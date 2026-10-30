"""Exact staged source outputs, bounded custody and interruption recovery."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest
import yaml

from evidence_wiki import mutation_operations as operations
from evidence_wiki import mutation_source_recovery as recovery
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import MutationError
from evidence_wiki.mutation_store import MutationStore
from tests.test_mutation_operations import host, owner, prepared
from tests.test_mutation_operations import roots as root_fixture


@pytest.fixture
def roots(tmp_path, monkeypatch):
    return root_fixture.__wrapped__(tmp_path, monkeypatch)


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def test_complete_inventory_interruption_uses_exact_planned_manifest_and_activity(roots, monkeypatch):
    root, _ = roots
    (root / "log.md").write_bytes(b"Original activity.\r\n")
    invoke = operations._invoke
    def interrupted(*args):
        result = invoke(*args)
        assert result["total"] == 1
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, "sources.inventory", {"append_log": True})
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        before = (root / "sources/manifest.jsonl").read_bytes(), (root / "log.md").read_bytes()
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("source redispatch"))
        result = operations.reconcile(root, "request")
        assert result["desired_state_proved"] and not result["completion_proved"]
        assert result["owner_observation"]["effects_complete"]
        assert result["proof_kind"] == "source_postconditions"
        assert (root / "sources/manifest.jsonl").read_bytes() == before[0]
        assert (root / "log.md").read_bytes() == before[1]
        assert before[1].startswith(b"Original activity.\r\n\n## ")


def test_normalization_extracts_once_then_proves_exact_completed_effects(roots, monkeypatch):
    root, _ = roots
    normalize = owner("normalize_sources")
    extract = normalize.normalize_selected_record
    calls = []
    def once(*args, **kwargs):
        calls.append(args[2].record["id"])
        return extract(*args, **kwargs)
    monkeypatch.setattr(normalize, "normalize_selected_record", once)
    invoke = operations._invoke
    def interrupted(*args):
        value = invoke(*args)
        assert value["summary"]["created"] == 1
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True, "append_log": True})
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: pytest.fail("extraction repeated"))
        result = operations.reconcile(root, "request")
        assert result["status"] == "reconciled" and result["desired_state_proved"]
        assert not result["authorized"] and not result["evidence_accepted"]
        assert calls == ["sample-source"]


def test_missing_final_activity_remains_partial_without_repairing_it(roots, monkeypatch):
    root, _ = roots
    publisher = owner("_usage_materialization")
    publish = publisher.publish_file
    def interrupted(project, path, *args, **kwargs):
        if path == "log.md":
            raise KeyboardInterrupt
        return publish(project, path, *args, **kwargs)
    monkeypatch.setattr(publisher, "publish_file", interrupted)
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True, "append_log": True})
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        assert not (root / "log.md").exists()
        result = operations.reconcile(root, "request")
        assert result["status"] == "partial" and not result["desired_state_proved"]
        assert "log.md" in result["owner_observation"]["missing_or_changed_outputs"]
        assert not (root / "log.md").exists()


def test_failed_extraction_and_existing_output_preservation_are_not_forecast_success(roots, monkeypatch):
    root, _ = roots
    normalize = owner("normalize_sources")
    monkeypatch.setattr(normalize, "normalize_selected_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("extractor failure")))
    with host(roots).activate():
        receipt = operations.submit(root, prepared(root, "normalize.sources", {"all_sources": True, "append_log": True}))
        assert receipt["status"] == "partial" and receipt["result"]["summary"]["failed"] == 1
        result = operations.reconcile(root, "request")
        assert result["status"] == "partial" and result["operation_outcome"] == "owner_return_partial"
        assert not result["desired_state_proved"] and not result["evidence_accepted"]
        assert not list((root / "sources/normalized").glob("*.md")) if (root / "sources/normalized").exists() else True


def test_structured_sidecar_deletion_is_an_explicit_partial_obligation(roots, monkeypatch):
    root, _ = roots
    (root / "raw/web/table.csv").write_text("name,value\nalpha,1\nbeta,2\n")
    record = {"id": "table-source", "kind": "table", "raw_paths": ["raw/web/table.csv"], "status": "discovered"}
    (root / "sources/manifest.jsonl").write_text(json.dumps(record) + "\n")
    with host(roots).activate():
        operations.submit(root, prepared(root, "normalize.sources", {"all_sources": True}, request_id="table"))
        normalize = owner("normalize_sources")
        md = normalize.normalized_output_path_for_record(record, root / "sources/normalized")
        sidecar = normalize.structured_view_path_for_record(md)
        assert sidecar.exists()
        old = md.read_bytes()
        record.update(kind="html", raw_paths=["raw/web/source.html"])
        (root / "sources/manifest.jsonl").write_text(json.dumps(record) + "\n")
        delete = recovery._delete
        def interrupted(*args):
            delete(*args)
            raise KeyboardInterrupt
        monkeypatch.setattr(recovery, "_delete", interrupted)
        request = prepared(root, "normalize.sources", {"all_sources": True, "force": True}, request_id="html")
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        assert not sidecar.exists() and md.read_bytes() == old
        result = operations.reconcile(root, "html")
        assert result["status"] == "partial" and not result["desired_state_proved"]
        assert md.relative_to(root).as_posix() in result["owner_observation"]["missing_or_changed_outputs"]


def test_captured_creation_date_and_frozen_clock_survive_exact_staging(roots, monkeypatch):
    root, _ = roots
    with host(roots).activate():
        operations.submit(root, prepared(root, "normalize.sources", {"all_sources": True}, request_id="first"))
        md = next((root / "sources/normalized").glob("*.md"))
        value = md.read_text()
        start = value.index("created:")
        end = value.index("\n", start)
        md.write_text(value[:start] + "created: '2020-01-01'" + value[end:])
        invoke = operations._invoke
        def verify_plan(*args):
            store = args[2]["mutation_store"]
            basis = store.read()["owner_basis"]
            plan = recovery._load(store, basis)
            assert plan["clock_utc"] == basis["source_stage"]["clock_utc"]
            payload = next(effect for effect in plan["effects"] if effect["path"].endswith(".md"))
            raw = store.read_stage(payload["stage"])
            assert b"2020-01-01" in raw
            # A further live read of created metadata would be a discrepancy.
            monkeypatch.setattr(owner("normalize_sources"), "existing_created_date", lambda *a: pytest.fail("created date reopened"))
            return invoke(*args)
        monkeypatch.setattr(operations, "_invoke", verify_plan)
        receipt = operations.submit(root, prepared(root, "normalize.sources", {"all_sources": True, "force": True}, request_id="update"))
        assert receipt["status"] == "committed" and "2020-01-01" in md.read_text()


@pytest.mark.parametrize("kind", ["raw", "question", "config", "raw_overlap"])
def test_unsafe_or_overlapping_manifest_targets_refuse_before_canonical_effects(roots, kind):
    root, _ = roots
    config = yaml.safe_load((root / "research.yml").read_text())
    if kind == "raw_overlap":
        config["raw"]["source_roots"] = ["."]
    else:
        config["sources"]["manifest_path"] = {"raw": "raw/web/source.html", "question": "wiki/questions/sample.md",
            "config": "research.yml"}[kind]
    (root / "research.yml").write_text(yaml.safe_dump(config))
    watched = {path: (root / path).read_bytes() for path in ("research.yml", "raw/web/source.html", "wiki/questions/sample.md")}
    with host(roots).activate():
        request = prepared(root, "sources.inventory", {})
        with pytest.raises(EvidenceWikiError):
            operations.submit(root, request)
        assert operations.inspect(root, "request")["record"]["state"]["status"] == "prepared"
    assert all((root / path).read_bytes() == raw for path, raw in watched.items())


def test_output_and_report_quotas_refuse_before_publication(roots, monkeypatch):
    root, _ = roots
    module = owner("source_inventory")
    prepare = module.prepare_inventory_effects
    def oversized(*args, **kwargs):
        value = prepare(*args, **kwargs)
        value["effects"][0]["data"] = b"x" * (recovery.MAX_ARTIFACT_BYTES + 1)
        return value
    monkeypatch.setattr(module, "prepare_inventory_effects", oversized)
    before = (root / "sources/manifest.jsonl").read_bytes()
    with host(roots).activate(), pytest.raises(MutationError) as error:
        operations.submit(root, prepared(root, "sources.inventory", {}))
    assert error.value.error_code == "MUTATION_SOURCE_OUTPUT_LIMIT"
    assert (root / "sources/manifest.jsonl").read_bytes() == before and not (root / "log.md").exists()


def test_duplicate_planned_output_target_refuses_before_first_effect(roots, monkeypatch):
    root, _ = roots
    module = owner("source_inventory")
    prepare = module.prepare_inventory_effects
    def contradictory(*args, **kwargs):
        value = prepare(*args, **kwargs)
        value["effects"].append(copy.deepcopy(value["effects"][0]))
        value["effects"][-1]["data"] = b"Contradictory output."
        return value
    monkeypatch.setattr(module, "prepare_inventory_effects", contradictory)
    before = (root / "sources/manifest.jsonl").read_bytes()
    with host(roots).activate(), pytest.raises(MutationError):
        operations.submit(root, prepared(root, "sources.inventory", {}))
    assert (root / "sources/manifest.jsonl").read_bytes() == before


def test_staging_crash_retains_clock_and_never_reextracts_on_duplicate(roots, monkeypatch):
    root, _ = roots
    stage = MutationStore.stage_blob
    def interrupted(self, name, raw):
        stage(self, name, raw)
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True})
        monkeypatch.setattr(MutationStore, "stage_blob", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        stored = operations.inspect(root, "request")["record"]
        assert stored["state"]["status"] == "prepared"
        assert stored["state"]["owner_basis"]["source_stage"]["phase"] == "preparing"
        clock = stored["state"]["owner_basis"]["source_stage"]["clock_utc"]
        monkeypatch.setattr(owner("normalize_sources"), "normalize_selected_record", lambda *a: pytest.fail("staging retry extraction"))
        with pytest.raises(MutationError) as error:
            operations.submit(root, request)
        assert error.value.error_code == "MUTATION_OUTCOME_UNKNOWN"
        assert operations.inspect(root, "request")["record"]["state"]["owner_basis"]["source_stage"]["clock_utc"] == clock
        assert not (root / "sources/normalized").exists()


def test_new_raw_child_or_changed_source_input_prevents_completed_source_proof(roots, monkeypatch):
    root, _ = roots
    invoke = operations._invoke
    def interrupted(*args):
        invoke(*args)
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, "sources.inventory", {})
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        (root / "raw/web/late.html").write_text("New unrelated source.")
        result = operations.reconcile(root, "request")
        assert not result["desired_state_proved"] and not result["completion_proved"]
        assert "owner_error" in result["owner_observation"]


def test_repeated_strict_checksum_refusal_stays_partial_even_when_manifest_bytes_match(roots):
    root, _ = roots
    with host(roots).activate():
        for index in range(2):
            request = prepared(root, "sources.inventory", {"require_checksum": True}, request_id=f"strict-{index}")
            receipt = operations.submit(root, request)
            assert receipt["status"] == "partial" and receipt["error"]["error_code"] == "INVENTORY_CHECKSUM_REQUIRED"
            assert (root / "sources/manifest.jsonl").read_bytes() == b""
            proof = operations.reconcile(root, request["request_id"])
            assert proof["status"] == "partial" and not proof["desired_state_proved"]
            assert proof["owner_observation"]["effects_complete"]
            assert proof["owner_observation"]["planned_operation_outcome"] == "partial"
            assert operations.submit(root, request) == receipt


def test_near_envelope_owner_report_is_not_duplicated_in_retained_return_reconciliation(roots, monkeypatch):
    root, _ = roots
    large = {f"part-{index}": "x" * 60000 for index in range(12)}
    module = owner("source_inventory")
    prepare = module.prepare_inventory_effects
    def large_report(*args, **kwargs):
        plan = prepare(*args, **kwargs)
        plan["report"]["large_observation"] = large
        return plan
    monkeypatch.setattr(module, "prepare_inventory_effects", large_report)
    with host(roots).activate():
        request = prepared(root, "sources.inventory", {})
        # A post-return input drift gives a retained result and uncertainty.
        invoke = operations._invoke
        def changed_after_return(*args):
            report = invoke(*args)
            args[2]["source_closing_error"] = True
            return report
        capture = operations._capture
        changed = [False]
        def failed_closing(*args, **kwargs):
            if changed[0]:
                changed[0] = False
                raise MutationError("MUTATION_REVISION_CONFLICT", "Closing capture refused.", recoverable=False)
            return capture(*args, **kwargs)
        def uncertain_after_return(*args):
            report = changed_after_return(*args)
            changed[0] = True
            return report
        monkeypatch.setattr(operations, "_invoke", uncertain_after_return)
        monkeypatch.setattr(operations, "_capture", failed_closing)
        receipt = operations.submit(root, request)
        assert receipt["status"] == "commit_unknown" and receipt["result"]["large_observation"] == large
        result = operations.reconcile(root, "request")
        assert result["status"] == "partial" and result["completion_proved"]
        assert "retained_result" not in result["owner_observation"]
        assert result["owner_observation"]["retained_result_sha256"] == digest(operations.canonical(receipt["result"]))
        assert operations.submit(root, request) == receipt


def test_absent_raw_tree_appearance_after_preparation_blocks_stale_empty_inventory(roots, monkeypatch):
    root, _ = roots
    (root / "raw/web/source.html").unlink()
    (root / "raw/web").rmdir()
    before = (root / "sources/manifest.jsonl").read_bytes()
    invoke = operations._invoke
    def new_input(*args):
        (root / "raw/web").mkdir()
        (root / "raw/web/new.html").write_text("New input after the absence observation.")
        return invoke(*args)
    with host(roots).activate():
        request = prepared(root, "sources.inventory", {})
        monkeypatch.setattr(operations, "_invoke", new_input)
        receipt = operations.submit(root, request)
        assert receipt["status"] == "commit_unknown" and receipt["error"]["error_code"] == "MUTATION_SOURCE_INPUTS_CHANGED"
        assert (root / "sources/manifest.jsonl").read_bytes() == before
        result = operations.reconcile(root, "request")
        assert not result["desired_state_proved"] and not result["completion_proved"]


def test_staging_closing_revision_conflict_precedes_executing_checkpoint(roots, monkeypatch):
    root, _ = roots
    module = owner("source_inventory")
    prepare = module.prepare_inventory_effects
    def changed_after_prepare(*args, **kwargs):
        value = prepare(*args, **kwargs)
        (root / "late.txt").write_text("An unrelated legacy edit while extraction was running.")
        return value
    monkeypatch.setattr(module, "prepare_inventory_effects", changed_after_prepare)
    before = (root / "sources/manifest.jsonl").read_bytes()
    with host(roots).activate():
        request = prepared(root, "sources.inventory", {})
        with pytest.raises(MutationError) as error:
            operations.submit(root, request)
        assert error.value.error_code == "MUTATION_STAGING_INPUTS_CHANGED"
        record = operations.inspect(root, "request")["record"]
        assert record["state"]["status"] == "prepared" and record["state"]["owner_basis"]["source_stage"]["phase"] == "ready"
        assert (root / "sources/manifest.jsonl").read_bytes() == before
