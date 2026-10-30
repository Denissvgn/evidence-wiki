"""Actual owners, durable interruption checkpoints and conservative replay."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
import yaml

from evidence_wiki import Workspace, _script_host
from evidence_wiki import mutation_operations as operations
from evidence_wiki._filesystem import os as native_os
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import INSPECTION, RECEIPT, RECONCILIATION, MutationError
from evidence_wiki.mutation_store import MutationStore, MutationStoreError
from tests.test_host_capture import profile

OUTER = {"mutation_operations." + name for name in ("revision", "prepare_request", "submit", "inspect", "reconcile")}
GRANTS = OUTER | {"workspace.open", "coverage_manifest.run_init_inline", "coverage_manifest.run_link",
    "coverage_manifest.run_evaluate", "coverage_manifest.run_revision", "intake_questions.run_intake",
    "source_requests.run_add_request", "source_requests.run_fulfill_request", "source_requests.run_list_requests",
    "source_inventory.run_inventory_report", "normalize_sources.run_normalization_report", "normalize_verify.run_verify",
    "onboarding.capture", "strict_evidence.run_operation.review", "evidence_usage.run_status"}


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    root, control = tmp_path / "workspace", tmp_path / "control"
    (root / "wiki/questions").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "raw/web").mkdir(parents=True)
    native_os.mkdir(control, mode=0o700)
    config = {"wiki": {"root": "wiki", "required_dirs": ["questions"]},
        "raw": {"source_roots": ["raw/web"]}, "sources": {"manifest_path": "sources/manifest.jsonl",
            "coverage_dir": "sources/coverage", "normalized_dir": "sources/normalized"}}
    (root / "research.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (root / "workspace-system.yml").write_text("workspace_system:\n  starter_version: local\n")
    (root / "wiki/questions/sample.md").write_text("---\ntype: question\nstatus: open\nquestion: What is observed?\n---\n")
    (root / "sources/manifest.jsonl").write_text(json.dumps({"id": "sample-source", "kind": "html",
        "raw_paths": ["raw/web/source.html"], "status": "discovered", "provenance": {"scope": {"facet_id": "primary"}}}) + "\n")
    (root / "raw/web/source.html").write_text("<html><title>Local study</title><body><h1>Study</h1><p>" +
        "Measured content from the original study. " * 30 + "</p></body></html>")
    (root / "index.md").write_text("# Workspace\n\n## Questions\n\n| Page | Summary | Updated | |\n| --- | --- | --- | --- |\n")
    return root, control


def host(roots, *, grants=GRANTS, actor="caller", writable=True, **options):
    root, control = roots
    return HostContext([root], grants, actor, [root] if writable else [], mutation_control_directory=control, **options)


def template():
    return {"coverage_profile": "declared", "required_facets": [{"facet_id": "primary", "description": "Primary observation",
        "evidence_path": "official_guidance", "source_policy": "official_primary", "freshness_policy": "no_staleness_check",
        "identity_policy": "official_domain_match", "min_sources": 1}], "optional_facets": []}


def prepared(root, operation="coverage.init", arguments=None, request_id="request"):
    return operations.prepare_request(root, request_id=request_id, operation=operation,
        arguments=arguments if arguments is not None else {"slug": "sample", "template": template()})


def capture_arguments(content=b"Measured result 0.74.\r\n"):
    return {"path": "raw/web/capture.md", "delivery": {
        "schema_version": "evidence-host-delivery/v1", "capture": profile(content),
        "content_base64": base64.b64encode(content).decode("ascii")}}


def owner(stem):
    return _script_host.load_packaged_script(_script_host.shared_assets_root(), stem)


def test_real_coverage_commit_duplicate_historical_replay_and_detachment(roots, monkeypatch, capsys):
    root, _ = roots
    with host(roots).activate():
        request = prepared(root)
        original = copy.deepcopy(request)
        receipt = operations.submit(root, request)
        assert receipt["schema_version"] == RECEIPT and receipt["status"] == "committed"
        assert receipt["result"]["manifest"]["question_slug"] == "sample"
        assert request == original and not receipt["authorized"] and not receipt["evidence_accepted"]
        manifest = root / "sources/coverage/sample.yml"
        first = manifest.read_bytes()
        (root / "other.txt").write_text("Live basis changed after success.")
        monkeypatch.setattr(operations, "_generation", lambda: pytest.fail("historical replay tested current generation"))
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("duplicate dispatch"))
        assert operations.submit(root, request) == receipt
        assert manifest.read_bytes() == first
        inspected = operations.inspect(root, "request")
        assert inspected["schema_version"] == INSPECTION and inspected["record"]["receipt"] == receipt
        inspected["record"]["receipt"]["result"].clear()
        assert operations.inspect(root, "request")["record"]["receipt"] == receipt
    assert capsys.readouterr() == ("", "")


def test_conflicting_id_reuse_precedes_current_revision_checks(roots):
    root, _ = roots
    with host(roots).activate():
        original = prepared(root)
        operations.submit(root, original)
        changed = copy.deepcopy(original)
        changed["arguments"]["force"] = True
        with pytest.raises(MutationStoreError) as error:
            operations.submit(root, changed)
        assert error.value.error_code == "MUTATION_REQUEST_CONFLICT"
        assert operations.inspect(root, "request")["record"]["intent"]["arguments"]["force"] is False


@pytest.mark.parametrize("kind", ["revision", "input", "generation"])
def test_stale_preconditions_refuse_before_owner_checkpoint_and_effect(roots, kind, monkeypatch):
    root, _ = roots
    (root / "batch.yml").write_text("schema_version: '1.0'\nquestions: []\n")
    with host(roots).activate():
        request = prepared(root, "questions.add_batch", {"from_file": "batch.yml"})
        if kind == "revision":
            (root / "new.txt").write_text("Concurrent knowledge change.")
        elif kind == "input":
            request["input_files"][0]["sha256"] = "sha256:" + "2" * 64
        else:
            request["owner_generation"]["package_sha256"] = "sha256:" + "2" * 64
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("stale dispatch"))
        with pytest.raises(MutationError) as error:
            operations.submit(root, request)
        assert error.value.error_code == "MUTATION_" + {"revision": "REVISION_CONFLICT",
            "input": "INPUT_CONFLICT", "generation": "GENERATION_CONFLICT"}[kind]
        assert operations.inspect(root, "request")["record"]["state"] is None
        assert not (root / "log.md").exists()


@pytest.mark.parametrize("kind", ["actor", "grants", "controller", "control"])
def test_changed_host_selection_refuses_historical_replay_and_inspection(roots, kind, tmp_path):
    root, control = roots
    with host(roots).activate():
        request = prepared(root)
        operations.submit(root, request)
    options = {}
    selected_roots = roots
    if kind == "actor":
        options["actor"] = "different-caller"
    elif kind == "grants":
        options["grants"] = GRANTS | {"workspace.versions"}
    elif kind == "controller":
        options["controller_refs"] = {"evaluator": "independent-controller"}
    else:
        other = tmp_path / "other-control"
        native_os.mkdir(other, mode=0o700)
        selected_roots = root, other
    with host(selected_roots, **options).activate(), pytest.raises(MutationError) as error:
        operations.submit(root, request)
    assert error.value.error_code == "MUTATION_HOST_CONTEXT_CONFLICT"
    with host(roots).activate():
        assert operations.inspect(root, "request")["record"]["receipt"]["status"] == "committed"
    assert control.is_dir()


def test_authority_policy_digest_changes_and_links_refuse_before_replay(roots, tmp_path):
    root, _ = roots
    custody = tmp_path / "authority"
    native_os.mkdir(custody, mode=0o700)
    policy = custody / "policy.json"
    policy.write_text('{"policy":"original"}')
    selected = host(roots, authority_file=policy)
    with selected.activate():
        request = prepared(root)
        operations.submit(root, request)
        policy.write_text('{"policy":"changed"}')
        with pytest.raises(MutationError) as error:
            operations.submit(root, request)
        assert error.value.error_code == "MUTATION_HOST_CONTEXT_CONFLICT"
        policy.unlink()
        policy.symlink_to(root / "research.yml")
        with pytest.raises(EvidenceWikiError):
            operations.submit(root, request)


@pytest.mark.parametrize("operation", ["revision", "prepare_request", "submit", "inspect", "reconcile"])
def test_outer_authorization_required_before_control_store_creation(roots, operation):
    root, control = roots
    with host(roots, grants=()).activate(), pytest.raises(EvidenceWikiError):
        if operation == "prepare_request":
            prepared(root)
        elif operation == "submit":
            operations.submit(root, {})
        elif operation in {"inspect", "reconcile"}:
            getattr(operations, operation)(root, "request")
        else:
            operations.revision(root)
    assert list(control.iterdir()) == []


@pytest.mark.parametrize("operation", ["inspect", "reconcile"])
@pytest.mark.parametrize("identifier", ["../escape", "", "x" * 97, "x\n", ["request"]])
def test_malformed_transport_id_never_reserves_control_entries(roots, operation, identifier):
    root, control = roots
    with host(roots).activate(), pytest.raises(MutationError):
        getattr(operations, operation)(root, identifier)
    assert list(control.iterdir()) == []


def test_missing_owner_grant_prevents_checkpoint_and_owner_loading(roots, monkeypatch):
    root, _ = roots
    selected = host(roots, grants=GRANTS - {"coverage_manifest.run_init_inline"})
    with selected.activate():
        request = prepared(root)
        monkeypatch.setattr(operations, "_basis", lambda *a: pytest.fail("unauthorized owner loaded"))
        with pytest.raises(EvidenceWikiError):
            operations.submit(root, request)
        assert operations.inspect(root, "request")["record"]["state"] is None
    assert not (root / "sources/coverage").exists()


def test_read_only_scope_and_control_overlap_refuse(roots, tmp_path):
    root, control = roots
    with host(roots, writable=False).activate():
        request = prepared(root)
        with pytest.raises(EvidenceWikiError):
            operations.submit(root, request)
    with host(roots, state_directory=control).activate(), pytest.raises(MutationError) as error:
        operations.revision(root)
    assert error.value.error_code == "MUTATION_CONTROL_OVERLAP"
    (control / "policy.json").write_text("{}")
    with host(roots, authority_file=control / "policy.json").activate(), pytest.raises(MutationError):
        operations.revision(root)


@pytest.mark.parametrize("kind,code", [("missing_manifest", "COVERAGE_MANIFEST_INVALID"),
    ("unknown_facet", "COVERAGE_FACET_UNKNOWN")])
def test_ordinary_owner_preconditions_have_typed_refusals(roots, kind, code):
    root, _ = roots
    with host(roots).activate():
        if kind == "unknown_facet":
            operations.submit(root, prepared(root))
        request = prepared(root, "coverage.link", {"slug": "sample", "facet_id": "unknown"}, request_id="link")
        with pytest.raises(EvidenceWikiError) as error:
            operations.submit(root, request)
        assert error.value.error_code == code
        assert operations.inspect(root, "link")["record"]["state"] is None


def test_owner_completed_checkpoint_recovers_without_redispatch_or_live_generation(roots, monkeypatch):
    root, _ = roots
    write = MutationStore.write
    def interrupted(self, state, **options):
        if state["status"] == "committed":
            raise KeyboardInterrupt
        return write(self, state, **options)
    with host(roots).activate():
        request = prepared(root)
        monkeypatch.setattr(MutationStore, "write", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        inspected = operations.inspect(root, "request")["record"]
        assert inspected["state"]["status"] == "owner_completed" and inspected["receipt"] is None
        monkeypatch.setattr(MutationStore, "write", write)
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("completed redispatch"))
        monkeypatch.setattr(operations, "_generation", lambda: pytest.fail("historical outcome current generation"))
        outcome = operations.reconcile(root, "request")
        assert outcome["status"] == "committed" and outcome["result"] == inspected["state"]["result"]
        assert outcome["after_revision"] is None
        assert operations.submit(root, request) == outcome


def test_missing_receipt_after_terminal_checkpoint_republishes_saved_exact_result(roots, monkeypatch):
    root, _ = roots
    publish = MutationStore.commit_receipt
    with host(roots).activate():
        request = prepared(root)
        monkeypatch.setattr(MutationStore, "commit_receipt", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        state = operations.inspect(root, "request")["record"]["state"]
        assert state["status"] == "committed" and state["result"] is not None
        monkeypatch.setattr(MutationStore, "commit_receipt", publish)
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("receipt repair redispatch"))
        receipt = operations.submit(root, request)
        assert receipt["result"] == state["result"] and receipt["after_revision"] == state["after_revision"]


def test_two_clients_share_workspace_gate_and_other_workspace_progresses(roots, tmp_path, monkeypatch):
    root, control = roots
    original = operations._invoke
    observed = []
    other = tmp_path / "independent"
    other.mkdir()
    (other / "research.yml").write_text("{}")
    def during_owner(*args):
        def blocked():
            with host(roots).activate():
                return operations.revision(root)
        def independent():
            selected = HostContext([other], OUTER, "caller", mutation_control_directory=control)
            with selected.activate():
                return operations.revision(other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            attempted, separate = pool.submit(blocked), pool.submit(independent)
            with pytest.raises(MutationStoreError) as error:
                attempted.result(timeout=5)
            observed.append(error.value.error_code)
            assert separate.result(timeout=5)["revision_id"].startswith("sha256:")
        return original(*args)
    with host(roots).activate():
        request = prepared(root)
        monkeypatch.setattr(operations, "_invoke", during_owner)
        assert operations.submit(root, request)["status"] == "committed"
    assert observed == ["MUTATION_LOCK_BUSY"]


def test_captured_intake_uses_pinned_bytes_and_original_hash_without_live_reread(roots, monkeypatch):
    root, _ = roots
    original_text = "schema_version: '1.0'\r\nquestions:\r\n  - question: '  What is the second observation?  '\r\n    id: second\r\n"
    path = root / "batch.yml"
    path.write_bytes(original_text.encode())
    invoke = operations._invoke
    def changed_after_capture(*args):
        path.write_text("schema_version: '1.0'\nquestions: [Changed question]\n")
        return invoke(*args)
    with host(roots).activate():
        request = prepared(root, "questions.add_batch", {"from_file": "batch.yml"})
        assert request["input_files"][0]["sha256"] == "sha256:" + hashlib.sha256(original_text.encode()).hexdigest()
        monkeypatch.setattr(operations, "_invoke", changed_after_capture)
        receipt = operations.submit(root, request)
        assert receipt["status"] == "commit_unknown" and receipt["result"]["counts"]["created"] == 1
        assert "What is the second observation?" in (root / "wiki/questions/second.md").read_text()
        assert not any("Changed question" in page.read_text() for page in (root / "wiki/questions").glob("*.md"))
        recovery = operations.reconcile(root, "request")
        assert recovery["status"] == "partial" and recovery["completion_proved"]
        assert recovery["operation_outcome"] == "owner_return_with_inputs_changed"
        assert not recovery["desired_state_proved"] and not recovery["evidence_accepted"]
        assert operations.submit(root, request) == receipt


@pytest.mark.parametrize("raw", [b"schema_version: '1.0'\nquestions: &q [*q]\n",
    b"schema_version: '1.0'\nquestions: []\nunexpected: !!python/object:builtins.object {}\n"])
def test_hostile_intake_yaml_refuses_before_pages_or_checkpoint(roots, raw):
    root, _ = roots
    (root / "batch.yml").write_bytes(raw)
    with host(roots).activate():
        request = prepared(root, "questions.add_batch", {"from_file": "batch.yml"})
        with pytest.raises(MutationError):
            operations.submit(root, request)
        assert operations.inspect(root, "request")["record"]["state"] is None
        assert not (root / "log.md").exists()


@pytest.mark.parametrize("operation,values", [
    ("coverage.init", {"slug": "sample", "template": template()}),
    ("source_requests.add", {"kind": "other", "query_or_identifier": "Original observations", "rationale": "Declared gap", "question_slugs": ["sample"]}),
    ("sources.capture", capture_arguments()),
])
def test_interrupted_actual_owner_effects_reconcile_desired_state_without_execution(roots, operation, values, monkeypatch):
    root, _ = roots
    original = operations._invoke
    def interrupted(*args):
        original(*args)
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, operation, values)
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        assert operations.inspect(root, "request")["record"]["state"]["status"] == "executing"
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("reconciliation dispatched"))
        result = operations.reconcile(root, "request")
        assert result["schema_version"] == RECONCILIATION and result["status"] == "reconciled"
        assert result["desired_state_proved"] and not result["completion_proved"]
        assert result["recovery_supported"] and not result["evidence_accepted"] and not result["authorized"]
        receipt = operations.submit(root, request)
        assert receipt["status"] == "reconciled" and receipt["result"]["original_outcome_unknown"]


def test_coverage_backfill_failure_is_partial_and_original_unknown_receipt_is_immutable(roots, monkeypatch):
    root, _ = roots
    with host(roots).activate(), Workspace.open(root) as workspace:
        added = workspace.source_requests.add(kind="other", query_or_identifier="Observation", rationale="Gap", question_slugs=["sample"])
        operations.submit(root, prepared(root, request_id="initial-coverage"))
        module = owner("coverage_manifest")
        backfill = module.backfill_facet_scope
        monkeypatch.setattr(module, "backfill_facet_scope", lambda *a: (_ for _ in ()).throw(OSError("fault")))
        request = prepared(root, "coverage.link", {"slug": "sample", "facet_id": "primary",
            "blocking_request_ids": [added["request"]["request_id"]]})
        earlier = operations.submit(root, request)
        assert earlier["status"] == "commit_unknown"
        first = operations.reconcile(root, "request")
        assert first["status"] == "partial" and not first["desired_state_proved"]
        assert first["owner_observation"]["missing_request_scope_ids"] == [added["request"]["request_id"]]
        monkeypatch.setattr(module, "backfill_facet_scope", backfill)
        # A separately authorized canonical repair, not transport redispatch.
        operations.submit(root, prepared(root, "coverage.link", {"slug": "sample", "facet_id": "primary",
            "blocking_request_ids": [added["request"]["request_id"]]}, request_id="repair-link"))
        second = operations.reconcile(root, "request")
        assert second["status"] == "reconciled" and second["desired_state_proved"]
        inspected = operations.inspect(root, "request")["record"]
        assert inspected["receipt"] == earlier and inspected["state"]["status"] == "reconciled"
        assert operations.submit(root, request) == earlier


def test_intake_partial_pages_do_not_prove_missing_index_or_activity(roots, monkeypatch):
    root, _ = roots
    (root / "batch.yml").write_text("schema_version: '1.0'\nquestions:\n  - question: A second observation?\n    id: second\n")
    module = owner("intake_questions")
    init = module.load_sibling_module("init_research_workspace")
    append = init.append_log_entry
    monkeypatch.setattr(init, "append_log_entry", lambda *a: (_ for _ in ()).throw(OSError("activity fault")))
    with host(roots).activate():
        request = prepared(root, "questions.add_batch", {"from_file": "batch.yml"})
        receipt = operations.submit(root, request)
        assert receipt["status"] == "commit_unknown" and (root / "wiki/questions/second.md").exists()
        result = operations.reconcile(root, "request")
        assert result["status"] == "partial" and result["owner_observation"]["activity_complete"] is False
        assert result["owner_observation"]["index_complete"] is True
        assert not result["desired_state_proved"] and not result["completion_proved"]
        assert operations.submit(root, request) == receipt
    monkeypatch.setattr(init, "append_log_entry", append)


def test_inventory_and_normalization_real_reports_remain_inert(roots):
    root, _ = roots
    with host(roots).activate():
        inventoried = operations.submit(root, prepared(root, "sources.inventory", {}, request_id="inventory"))
        assert inventoried["status"] == "committed" and inventoried["result"]["total"] == 1
        normalized = operations.submit(root, prepared(root, "normalize.sources", {"all_sources": True}, request_id="normalize"))
        assert normalized["status"] == "committed" and normalized["result"]["summary"]["created"] == 1
        assert not normalized["authorized"] and not normalized["evidence_accepted"]


def test_capture_partial_pair_never_retries_missing_sidecar(roots, monkeypatch):
    root, _ = roots
    from evidence_wiki import source_delivery
    publish = source_delivery._publish
    def interrupted(directory, name, raw):
        if name.endswith(".provenance.yml"):
            raise KeyboardInterrupt
        return publish(directory, name, raw)
    with host(roots).activate():
        request = prepared(root, "sources.capture", capture_arguments())
        monkeypatch.setattr(source_delivery, "_publish", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        assert (root / "raw/web/capture.md").exists() and not (root / "raw/web/capture.md.provenance.yml").exists()
        result = operations.reconcile(root, "request")
        assert result["status"] == "partial" and not result["desired_state_proved"]
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("partial pair redispatch"))
        assert operations.submit(root, request)["status"] == "partial"


def test_exact_capture_pair_under_link_root_cannot_prove_invalid_owner_target(roots):
    root, _ = roots
    with host(roots).activate():
        valid = capture_arguments()
        operations.submit(root, prepared(root, "sources.capture", valid, request_id="valid"))
        (root / "raw/links").mkdir()
        for suffix in ("", ".provenance.yml"):
            (root / ("raw/links/capture.md" + suffix)).write_bytes((root / (valid["path"] + suffix)).read_bytes())
        config = yaml.safe_load((root / "research.yml").read_text())
        config["raw"]["source_roots"].append("raw/links")
        (root / "research.yml").write_text(yaml.safe_dump(config))
        forbidden = {**valid, "path": "raw/links/capture.md"}
        request = prepared(root, "sources.capture", forbidden, request_id="links")
        receipt = operations.submit(root, request)
        assert receipt["status"] == "commit_unknown"
        result = operations.reconcile(root, "links")
        assert not result["desired_state_proved"] and not result["completion_proved"]
        assert result["owner_observation"]["owner_error"]["error_code"] == "MUTATION_INPUT_UNBOUND"


def test_fulfillment_postconditions_and_activity_survive_unknown_owner_return(roots, monkeypatch):
    root, _ = roots
    with host(roots).activate(), Workspace.open(root) as workspace:
        added = workspace.source_requests.add(kind="other", query_or_identifier="Observation", rationale="Gap",
            question_slugs=["sample"], scope={"facet_id": "primary"})
        request = prepared(root, "source_requests.fulfill", {"request_id": added["request"]["request_id"],
            "source_id": "sample-source", "match_scope": {"facet_id": "primary"}, "require_scope": True})
        original = operations._invoke
        def no_transport_return(*args):
            original(*args)
            raise RuntimeError("transport disconnected after owner effects")
        monkeypatch.setattr(operations, "_invoke", no_transport_return)
        earlier = operations.submit(root, request)
        assert earlier["status"] == "commit_unknown"
        result = operations.reconcile(root, "request")
        assert result["status"] == "reconciled" and result["desired_state_proved"]
        assert result["owner_observation"]["activity_complete"] and result["owner_observation"]["fields_match"]
        assert operations.submit(root, request) == earlier
        assert workspace.source_requests.list()["requests"][0]["status"] == "fulfilled"


def test_request_row_without_required_activity_is_precise_partial(roots, monkeypatch):
    root, _ = roots
    module = owner("source_requests")
    monkeypatch.setattr(module, "append_log_entry", lambda *a: (_ for _ in ()).throw(OSError("activity fault")))
    with host(roots).activate():
        request = prepared(root, "source_requests.add", {"kind": "other", "query_or_identifier": "Observation",
            "rationale": "Gap", "question_slugs": ["sample"]})
        receipt = operations.submit(root, request)
        result = operations.reconcile(root, "request")
        assert receipt["status"] == "commit_unknown" and result["status"] == "partial"
        assert result["owner_observation"]["fields_match"] and not result["owner_observation"]["activity_complete"]
        assert operations.submit(root, request) == receipt


def test_normalization_failed_report_and_checksum_postwrite_error_remain_partial(roots, monkeypatch):
    root, _ = roots
    module = owner("normalize_sources")
    monkeypatch.setattr(module, "normalize_selected_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("extractor fault")))
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True})
        receipt = operations.submit(root, request)
        assert receipt["status"] == "partial" and receipt["result"]["summary"]["failed"] == 1
        assert not receipt["evidence_accepted"]
        recovered = operations.reconcile(root, "request")
        assert recovered["status"] == "partial" and recovered["completion_proved"]
        assert recovered["operation_outcome"] == "owner_return_partial" and not recovered["desired_state_proved"]
        assert operations.submit(root, request) == receipt
        raw = root / "raw/web/source.html"
        raw.with_name(raw.name + ".provenance.yml").write_text(
            "checksum: sha256:" + hashlib.sha256(raw.read_bytes()).hexdigest() + "\n")
        (root / "raw/web/missing.html").write_text("<html><body>Missing checksum.</body></html>")
        request = prepared(root, "sources.inventory", {"require_checksum": True}, request_id="inventory")
        partial = operations.submit(root, request)
        assert partial["status"] == "partial" and partial["error"]["error_code"] == "INVENTORY_CHECKSUM_REQUIRED"
        assert len((root / "sources/manifest.jsonl").read_text().splitlines()) == 1


@pytest.fixture
def strict_fixture(tmp_path, monkeypatch, request):
    from tests.test_strict_evidence import host as strict_host

    return strict_host.__wrapped__(tmp_path, monkeypatch, request)


def signed_review(fixture):
    from tests._execution_fixture import KEYS, authenticate, canonical
    from tests.test_strict_evidence import CONTRACT, CORE

    prepared = CORE.prepare_review(fixture.root, "product-source")
    result = prepared["result"]
    row = next(row for row in result["claims"] if row["claim"]["id"] == "product-source")
    review = {"schema_version": prepared["review_schema"], "basis_id": result["basis_id"], "claim_id": "product-source",
        "generator": authenticate({"schema_version": "evidence-strict-authorship/v1", "basis_id": result["basis_id"],
            "claim_id": "product-source"}, "runner", "generator"), "verdicts": dict.fromkeys(CONTRACT.CHECKS, "pass"),
        "rationale": "Judgment against the retained fixture rubric.", "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "observation": row["observation"], "snapshot": prepared["snapshot"]}
    command = prepared["registration"]
    command["request_id"] = "registered-review"
    command["body"]["review"] = review
    envelope = authenticate(command, "evaluator", "evaluator")
    envelope["authentication"]["issued_at"] = datetime.now(timezone.utc).isoformat()
    auth = {key: child for key, child in envelope["authentication"].items() if key != "signature"}
    signed = b"evidence-attestation/v1\0" + canonical({"payload": envelope["payload"], "authentication": auth})
    envelope["authentication"]["signature"] = hmac.new(bytes.fromhex(KEYS["evaluator"]), signed, hashlib.sha256).hexdigest()
    return envelope


def strict_context(fixture, control):
    return HostContext([fixture.root], GRANTS, "caller", [fixture.root], authority_file=fixture.policy_path,
        state_directory=fixture.host, mutation_control_directory=control)


def test_strict_review_reuses_authenticated_protocol_and_exact_event_recovery(strict_fixture, tmp_path, monkeypatch):
    fixture = strict_fixture
    control = tmp_path / "mutation-control"
    native_os.mkdir(control, mode=0o700)
    envelope = signed_review(fixture)
    original = operations._invoke
    def interrupted(*args):
        original(*args)
        raise KeyboardInterrupt
    with strict_context(fixture, control).activate():
        request = prepared(fixture.root, "strict.register_review", {"envelope": envelope})
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(fixture.root, request)
        state = (fixture.host / "evidence-state.json").read_bytes()
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("authenticated review redispatch"))
        result = operations.reconcile(fixture.root, "request")
        assert result["status"] == "reconciled" and result["completion_proved"]
        assert result["proof_kind"] == "strict_checkpoint" and not result["evidence_accepted"]
        assert result["owner_observation"]["exact_signed_command"]
        assert result["owner_observation"]["current_acceptance"] == "not_evaluated"
        assert (fixture.host / "evidence-state.json").read_bytes() == state


def test_forged_review_same_inner_id_never_matches_original_canonical_event(strict_fixture, tmp_path):
    fixture = strict_fixture
    from tests.test_strict_evidence import USAGE

    envelope = signed_review(fixture)
    USAGE.transact(fixture.root, fixture.config, envelope)
    forged = copy.deepcopy(envelope)
    forged["payload"]["body"]["review"]["verdicts"]["support"] = "fail"
    control = tmp_path / "mutation-control"
    native_os.mkdir(control, mode=0o700)
    before = (fixture.host / "evidence-state.json").read_bytes()
    with strict_context(fixture, control).activate():
        request = prepared(fixture.root, "strict.register_review", {"envelope": forged})
        receipt = operations.submit(fixture.root, request)
        assert receipt["status"] == "commit_unknown"
        result = operations.reconcile(fixture.root, "request")
        assert result["status"] == "partial" and not result["completion_proved"] and not result["evidence_accepted"]
        assert result["owner_observation"]["exact_signed_command"] is False
    assert (fixture.host / "evidence-state.json").read_bytes() == before


def test_text_only_intake_uses_canonical_normalized_question_for_index_basis(roots):
    root, _ = roots
    (root / "batch.yml").write_text("schema_version: '1.0'\nquestions:\n  - text: What is the textual observation?\n    id: textual\n")
    with host(roots).activate():
        receipt = operations.submit(root, prepared(root, "questions.add_batch", {"from_file": "batch.yml"}))
        assert receipt["status"] == "committed" and receipt["result"]["counts"]["created"] == 1
    assert (root / "wiki/questions/textual.md").exists()


def test_matching_intake_index_row_in_another_section_never_proves_completion(roots, monkeypatch):
    root, _ = roots
    (root / "batch.yml").write_text("schema_version: '1.0'\nquestions:\n  - question: A second observation?\n    id: second\n")
    invoke = operations._invoke
    def interrupted(*args):
        invoke(*args)
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, "questions.add_batch", {"from_file": "batch.yml"})
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        index = (root / "index.md").read_text().replace("## Questions", "## Other")
        (root / "index.md").write_text(index)
        result = operations.reconcile(root, "request")
        assert result["status"] == "partial" and not result["desired_state_proved"]
        assert result["owner_observation"]["index_complete"] is False


@pytest.mark.parametrize("change", ["source_membership", "request_membership", "criteria"])
def test_link_proof_checks_canonical_membership_and_unchanged_facet_criteria(roots, monkeypatch, change):
    root, _ = roots
    with host(roots).activate(), Workspace.open(root) as workspace:
        added = workspace.source_requests.add(kind="other", query_or_identifier="Observation", rationale="Gap", question_slugs=["sample"])
        operations.submit(root, prepared(root, request_id="initial-coverage"))
        request = prepared(root, "coverage.link", {"slug": "sample", "facet_id": "primary",
            "accepted_source_ids": ["sample-source"], "blocking_request_ids": [added["request"]["request_id"]]})
        invoke = operations._invoke
        def interrupted(*args):
            invoke(*args)
            raise KeyboardInterrupt
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        if change == "source_membership":
            (root / "sources/manifest.jsonl").write_bytes(b"")
        elif change == "request_membership":
            path = owner("source_requests").requests_path(root, yaml.safe_load((root / "research.yml").read_text()))
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["question_slugs"] = ["different-question"]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        else:
            path = root / "sources/coverage/sample.yml"
            document = yaml.safe_load(path.read_text())
            document["required_facets"][0]["description"] = "Changed criteria."
            path.write_text(yaml.safe_dump(document))
        result = operations.reconcile(root, "request")
        assert not result["desired_state_proved"] and not result["completion_proved"]
        assert not result["evidence_accepted"]


def test_add_proof_cannot_keep_missing_question_membership(roots, monkeypatch):
    root, _ = roots
    invoke = operations._invoke
    def interrupted(*args):
        invoke(*args)
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, "source_requests.add", {"kind": "other", "query_or_identifier": "Observation",
            "rationale": "Gap", "question_slugs": ["sample"]})
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        (root / "wiki/questions/sample.md").unlink()
        result = operations.reconcile(root, "request")
        assert not result["desired_state_proved"] and not result["completion_proved"]
        assert "owner_error" in result["owner_observation"]


@pytest.mark.parametrize("operation,values", [("sources.inventory", {}), ("normalize.sources", {"all_sources": True}),
    ("coverage.evaluate", {"slug": "sample"})])
def test_planned_owner_outcomes_have_exact_effect_proof_without_redispatch(roots, operation, values, monkeypatch):
    root, _ = roots
    invoke = operations._invoke
    def interrupted(*args):
        invoke(*args)
        raise KeyboardInterrupt
    with host(roots).activate(), Workspace.open(root) as workspace:
        if operation == "coverage.evaluate":
            operations.submit(root, prepared(root, request_id="initial-coverage"))
        request = prepared(root, operation, values)
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        monkeypatch.setattr(workspace.normalize.__class__, "verify", lambda *a: pytest.fail("unsupported live verify"))
        result = operations.reconcile(root, "request")
        assert result["operation_outcome"] == "desired_state_only" and not result["completion_proved"]
        assert result["desired_state_proved"] and result["recovery_supported"]
        assert result["status"] == "reconciled" and not result["evidence_accepted"]


def test_absolute_wiki_reader_cannot_use_transient_live_question_to_prove_missing_capture(roots, monkeypatch):
    root, _ = roots
    config = yaml.safe_load((root / "research.yml").read_text())
    config["wiki"]["root"] = str(root / "wiki")
    (root / "research.yml").write_text(yaml.safe_dump(config))
    values = {"kind": "other", "query_or_identifier": "Existing observations", "rationale": "Gap",
              "question_slugs": ["sample"]}
    with host(roots).activate(), Workspace.open(root) as workspace:
        workspace.source_requests.add(**values)
        request = prepared(root, "source_requests.add", values, request_id="absolute-reader")
        monkeypatch.setattr(operations, "_invoke", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        question = root / "wiki/questions/sample.md"
        original = question.read_bytes()
        question.unlink()
        module = owner("source_requests")
        validate = module.validate_question_slugs
        seen = []
        def transient_live_question(project, configuration, slugs):
            seen.append(module.questions_directory(project, configuration))
            question.write_bytes(original)
            try:
                return validate(project, configuration, slugs)
            finally:
                question.unlink()
        monkeypatch.setattr(module, "validate_question_slugs", transient_live_question)
        result = operations.reconcile(root, "absolute-reader")
        assert not result["desired_state_proved"] and not result["completion_proved"]
        assert result["status"] != "reconciled" and not question.exists()
        assert seen == []  # Missing captured inputs refuse before any validator.
        assert result["owner_observation"]["owner_error"]["error_code"] == "MUTATION_CAPTURED_INPUT_MISSING"


def test_absolute_wiki_config_is_detached_and_remapped_to_private_captured_reader(roots, monkeypatch):
    root, _ = roots
    config = yaml.safe_load((root / "research.yml").read_text())
    config["wiki"]["root"] = str(root / "wiki")
    (root / "research.yml").write_text(yaml.safe_dump(config))
    values = {"kind": "other", "query_or_identifier": "Original observations", "rationale": "Gap",
              "question_slugs": ["sample"]}
    module = owner("source_requests")
    validate = module.validate_question_slugs
    seen = []
    def private_question(project, configuration, slugs):
        effective = module.questions_directory(project, configuration)
        assert effective.is_relative_to(project) and not effective.is_relative_to(root)
        assert (effective / "sample.md").read_bytes() == (root / "wiki/questions/sample.md").read_bytes()
        seen.append((project, configuration["wiki"]["root"]))
        return validate(project, configuration, slugs)
    monkeypatch.setattr(module, "validate_question_slugs", private_question)
    with host(roots).activate():
        request = prepared(root, "source_requests.add", values)
        invoke = operations._invoke
        # Canonical actual owner invocation uses the live root appropriately;
        # patch only captured validation and execute the owner without this spy.
        def real_then_interrupt(*args):
            monkeypatch.setattr(module, "validate_question_slugs", validate)
            try:
                invoke(*args)
            finally:
                monkeypatch.setattr(module, "validate_question_slugs", private_question)
            raise KeyboardInterrupt
        monkeypatch.setattr(operations, "_invoke", real_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        result = operations.reconcile(root, "request")
        assert result["desired_state_proved"] and result["operation_outcome"] == "desired_state_only"
        assert len(seen) == 2 and all(value == "wiki" for _, value in seen)
    assert config["wiki"]["root"] == str(root / "wiki")
    assert yaml.safe_load((root / "research.yml").read_text()) == config


def test_snapshot_reader_refuses_absolute_generated_reader_config_instead_of_inventing_admissibility(roots):
    root, _ = roots
    config = {"wiki": {"root": str(root / "wiki")}, "sources": {
        "manifest_path": str(root / "sources/manifest.jsonl")}}
    with host(roots).activate(), pytest.raises(MutationError) as error, operations._validator_view({}, root, config):
        pytest.fail("absolute generated source path became admissible")
    assert error.value.error_code == "MUTATION_INPUT_UNBOUND"


@pytest.mark.parametrize("operation,values,change", [
    ("questions.add_batch", {"from_file": "batch.yml"}, "wiki_root"),
    ("source_requests.add", {"kind": "other", "query_or_identifier": "Observation", "rationale": "Gap",
        "question_slugs": ["sample"]}, "request_path"),
    ("coverage.init", {"slug": "sample", "template": template()}, "coverage_path"),
])
def test_configuration_drift_cannot_reuse_original_desired_state_obligations(roots, operation, values, change, monkeypatch):
    root, _ = roots
    (root / "batch.yml").write_text("schema_version: '1.0'\nquestions:\n  - question: A second observation?\n    id: second\n")
    invoke = operations._invoke
    def interrupted(*args):
        invoke(*args)
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root, operation, values)
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        original = operations.inspect(root, "request")["record"]
        assert original["state"]["owner_basis"]["configuration_sha256"].startswith("sha256:")
        config = yaml.safe_load((root / "research.yml").read_text())
        if change == "wiki_root":
            config["wiki"]["root"] = "other-wiki"
        else:
            config["sources"]["source_requests_path" if change == "request_path" else "coverage_dir"] = "sources/other"
        (root / "research.yml").write_text(yaml.safe_dump(config))
        with pytest.raises(MutationError) as error:
            operations.reconcile(root, "request")
        assert error.value.error_code == "MUTATION_CONFIGURATION_CONFLICT"
        after = operations.inspect(root, "request")["record"]
        assert after == original and after["receipt"] is None


def test_configuration_drift_does_not_reinterpret_retained_owner_return_as_current_desired_state(roots, monkeypatch):
    root, _ = roots
    write = MutationStore.write
    def interrupted(self, state, **options):
        if state["status"] == "committed":
            raise KeyboardInterrupt
        return write(self, state, **options)
    with host(roots).activate():
        request = prepared(root)
        monkeypatch.setattr(MutationStore, "write", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        original = operations.inspect(root, "request")["record"]["state"]["result"]
        config = yaml.safe_load((root / "research.yml").read_text())
        config["sources"]["coverage_dir"] = "sources/other"
        (root / "research.yml").write_text(yaml.safe_dump(config))
        monkeypatch.setattr(MutationStore, "write", write)
        monkeypatch.setattr(operations, "_invoke", lambda *a: pytest.fail("historical completion redispatch"))
        receipt = operations.reconcile(root, "request")
        assert receipt["schema_version"] == RECEIPT and receipt["result"] == original
        assert receipt["historical"] and not receipt["authorized"] and not receipt["evidence_accepted"]
        assert not (root / "sources/other").exists()


def test_interrupted_init_cannot_prove_desired_state_after_selected_question_is_removed(roots, monkeypatch):
    root, _ = roots
    invoke = operations._invoke
    def interrupted(*args):
        invoke(*args)
        raise KeyboardInterrupt
    with host(roots).activate():
        request = prepared(root)
        monkeypatch.setattr(operations, "_invoke", interrupted)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request)
        assert (root / "sources/coverage/sample.yml").exists()
        (root / "wiki/questions/sample.md").unlink()
        result = operations.reconcile(root, "request")
        assert not result["desired_state_proved"] and not result["completion_proved"]
        assert result["status"] != "reconciled" and not result["evidence_accepted"]
        assert result["owner_observation"]["owner_error"]["error_code"] == "MUTATION_CAPTURED_INPUT_MISSING"
        assert operations.inspect(root, "request")["record"]["receipt"] is None


def test_missing_init_question_uses_canonical_typed_precondition_without_writes(roots):
    root, _ = roots
    (root / "wiki/questions/sample.md").unlink()
    with host(roots).activate():
        request = prepared(root)
        with pytest.raises(EvidenceWikiError) as error:
            operations.submit(root, request)
        assert error.value.error_code == "SLUG_UNKNOWN"
        assert operations.inspect(root, "request")["record"]["state"] is None
    assert not (root / "sources/coverage").exists()
