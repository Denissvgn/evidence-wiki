"""Closed drafts, exact originals and unaccepted canonical publication."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import stat
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from evidence_wiki import Workspace, draft_contracts
from evidence_wiki import mutation_draft_recovery as recovery
from evidence_wiki import mutation_operations as operations
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import make_intent
from evidence_wiki.mutation_store import MutationStore, capture_revision
from evidence_wiki.onboarding_contract import decode_document
from evidence_wiki.pack_discovery import owner
from tests._execution_fixture import KEYS, authenticate, closure, identifier
from tests._execution_fixture import canonical as authenticated_bytes
from tests._usage_fixture import UsageFixture
from tests.test_host_capture import deposit, profile
from tests.test_mutation_store import state

GRANTS = {"workspace.open", "strict_evidence.run_admit_document", "strict_evidence.run_operation.check"} | {
    "mutation_operations." + name for name in ("revision", "prepare_request", "submit", "inspect", "reconcile")}


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def request(root, original="  What observation was retained?  ", *, derived=None):
    return {"schema_version": "1.0", "kind": "research_request", "request_id": "research",
        "payload": {"goal": "Retain exactly scoped observations.", "questions": [{"id": "q", "text": original}],
            "derived_questions": derived or [], "target": {"writable_root": str(root.parent), "relative_path": root.name},
            "outputs": ["json"], "scope": [{"name": "subject", "value": "Synthetic study"}],
            "domain": {"mode": "none", "pack": None, "rationale": "Local declared study."}, "sources": [], "host_tools": [],
            "authority": {"role": "caller", "reference": "host", "allowed_actions": ["local_research"],
                "source_scope": [str(root / "raw")], "writable_roots": [str(root)], "credential_references": []},
            "budgets": {"questions": 100, "source_requests": 0, "downloads": 0, "bytes": 1000000, "seconds": 60},
            "assumptions": [], "open_decisions": []}}


def update_policy(root, edit=None):
    path = root / "research.yml"
    config = yaml.safe_load(path.read_bytes())
    if edit:
        edit(config["strict_evidence"])
    config["strict_evidence"]["instructions"] = {"AGENTS.md": sha((root / "AGENTS.md").read_bytes()),
        "docs/research-requirements.json": sha((root / "docs/research-requirements.json").read_bytes())}
    path.write_text(yaml.safe_dump(config, sort_keys=False))


@pytest.fixture
def draft_root(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    root, control = tmp_path / "workspace", tmp_path / "control"
    for name in ("wiki/questions", "sources", "raw/web", "docs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    control.mkdir(mode=0o700)
    (root / "workspace-system.yml").write_text("workspace_system:\n  starter_version: local\n")
    (root / "AGENTS.md").write_text("Retain exact source observations.\n")
    declaration = request(root)
    decode_document("onboarding/research_request/v1", canonical(declaration))
    (root / "docs/research-requirements.json").write_bytes(canonical({"schema_version": "evidence-research-requirements/v1",
        "request": declaration, "decisions": {}}))
    policy = {"schema_version": "evidence-strict-policy/v1", "policy_id": "local-study", "revision": "1",
        "assurance": "artifact_checked", "claims_path": "docs/research-claims.json", "instructions": {"AGENTS.md": sha((root / "AGENTS.md").read_bytes())},
        "rubric": {"id": "study", "revision": "1", "criteria": {key: "Independent current review required." for key in
            ("support", "source_suitability", "scope", "time", "units", "counterevidence")}},
        "human_review": False, "max_source_age_seconds": 31536000, "max_review_age_seconds": 86400}
    config = {"wiki": {"root": "wiki", "required_dirs": ["questions"]}, "raw": {"source_roots": ["raw/web"]},
        "sources": {"manifest_path": "sources/manifest.jsonl", "normalized_dir": "sources/normalized", "coverage_dir": "sources/coverage"},
        "strict_evidence": policy}
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    text = declaration["payload"]["questions"][0]["text"]
    batch = {"schema_version": "1.0", "questions": [{"id": "q", "question": text, "origin": "caller",
        "metadata": {"research_id": "q", "original_ids_json": '["q"]', "original_text": text}}]}
    owner("intake_questions").run_intake_document(root, batch, dry_run=False, from_file_label="synthetic")
    data = b"Measured solar reflectance was 0.74.\n"
    capture = profile(data, retrieved_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    deposit(root, data, capture)
    owner("source_inventory").run_inventory_report(root, append_log=False)
    record = json.loads((root / "sources/manifest.jsonl").read_text().splitlines()[0])
    owner("normalize_sources").run_normalization_report(root, source_ids=[record["id"]], append_log=False)
    normalized = root / "sources/normalized" / (owner("_normalized_contract").safe_source_id(record["id"]) + ".md")
    update_policy(root)
    proposal = {"schema_version": draft_contracts.PROPOSAL,
        "originals": [{"original_id": "q", "text": text, "question_slugs": ["q"]}],
        "inventory": {"schema_version": "evidence-strict-claims/v1", "questions": [{"slug": "q", "original_id": "q", "question": text.strip()}],
            "claims": [{"id": "observation", "question_slug": "q", "qualification": "supported", "text": "Observed reflectance was 0.74.",
                "scope": "Synthetic study only.", "time": "Captured observation only.", "units": "Unitless reflectance.",
                "premises": [], "derivation": None, "limitations": ["Synthetic observation; independent review remains required."],
                "evidence": [{"source_id": record["id"], "record_sha256": sha(normalized.read_bytes()), "observed_at": capture["retrieved_at"],
                    "capture": "primary", "quote": "Measured solar reflectance was 0.74.", "location_hint": None, "anchor": None}]}]}}
    return root.resolve(), control.resolve(), proposal, normalized


def host(fixture, *, actor="caller", grants=GRANTS):
    return HostContext([fixture[0]], grants, actor, [fixture[0]], mutation_control_directory=fixture[1])


def intent(root, proposal, request_id="admit"):
    value = operations.prepare_request(root, request_id=request_id, operation="strict.admit", arguments={"proposal": proposal})
    return make_intent(value, capture_revision(root, excluded=(".locks", "raw/.locks"))["workspace_binding"])


def seed(store, value):
    store.prepare(value)
    store.write(state(value, "prepared"))


def observations(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_codec_roundtrip_detaches_unicode_and_exact_originals(draft_root):
    proposal = copy.deepcopy(draft_root[2])
    proposal["originals"][0]["text"] = "  ¿Qué observación se conservó? 日本語の観測  "
    raw = draft_contracts.encode_document(draft_contracts.PROPOSAL, proposal)
    value = draft_contracts.decode_document(draft_contracts.PROPOSAL, raw)
    assert value == proposal
    value["originals"][0]["text"] = "caller mutation"
    assert draft_contracts.decode_document(draft_contracts.PROPOSAL, raw) == proposal
    schema = draft_contracts.schema_document(draft_contracts.PROPOSAL)
    schema["properties"].clear()
    assert draft_contracts.schema_document(draft_contracts.PROPOSAL)["properties"]


@pytest.mark.parametrize("name", [None, [], {}, True, "unknown"])
def test_codec_invalid_schema_refuses_before_owner_lookup(monkeypatch, name):
    monkeypatch.setattr(draft_contracts, "owner", lambda *a: pytest.fail("owner lookup"))
    with pytest.raises(EvidenceWikiError):
        draft_contracts.schema_document(name)


@pytest.mark.parametrize("payload", [0, None, "opaque"])
def test_codec_contains_loader_process_exit_without_converting_payload(monkeypatch, payload):
    def exited(*args):
        raise SystemExit(payload)
    monkeypatch.setattr(draft_contracts, "owner", exited)
    with pytest.raises(EvidenceWikiError) as error:
        draft_contracts.schema_document(draft_contracts.PROPOSAL)
    assert error.value.error_code == "LIBRARY_PROCESS_EXIT"


@pytest.mark.parametrize("kind", ["unknown", "approval", "cycle", "forward", "duplicate", "nan", "oversized", "original_mismatch"])
def test_closed_malformed_proposals_refuse(draft_root, kind):
    value = copy.deepcopy(draft_root[2])
    claim = value["inventory"]["claims"][0]
    if kind == "unknown":
        value["source_body"] = "invented source"
    elif kind == "approval":
        claim["accepted"] = True
    elif kind == "cycle":
        claim.update(qualification="inference", premises=[claim["id"]], derivation="Self proof.")
    elif kind == "forward":
        claim.update(qualification="inference", premises=["later"], derivation="Forward proof.")
    elif kind == "duplicate":
        value["inventory"]["claims"].append(copy.deepcopy(claim))
    elif kind == "nan":
        claim["units"] = float("nan")
    elif kind == "oversized":
        claim["text"] = "x" * 4097
    else:
        value["originals"][0]["original_id"] = "other"
    with pytest.raises(EvidenceWikiError):
        draft_contracts.validate_document(draft_contracts.PROPOSAL, value)


def test_public_admission_and_same_id_old_revision_replay_have_no_status_approval_effect(draft_root):
    root, _control, proposal, _normalized = draft_root
    with host(draft_root).activate(), Workspace.open(root) as workspace:
        before = observations(root)
        revision = workspace.mutations.revision()["revision_id"]
        receipt = workspace.strict.admit(proposal, request_id="admit", expected_revision=revision)
        assert receipt["status"] == "committed" and receipt["result"]["accepted"] is False
        assert receipt["result"]["mechanical_check_count"] == 1
        assert receipt["result"]["review_registered"] is False and receipt["result"]["question_status_changed"] is False
        target = "docs/research-claims.json"
        assert all(observations(root)[path] == raw for path, raw in before.items())
        assert json.loads((root / target).read_bytes()) == proposal["inventory"]
        assert workspace.strict.admit(proposal, request_id="admit", expected_revision=revision) == receipt
        current = workspace.strict.check()
        assert all(row["accepted"] is False for row in current["claims"])
        changed = copy.deepcopy(proposal)
        changed["inventory"]["claims"][0]["text"] = "Changed draft."
        with pytest.raises(EvidenceWikiError):
            workspace.strict.admit(changed, request_id="admit", expected_revision=revision)


def test_lost_return_recovers_exact_inventory_and_preserves_unknown_receipt(draft_root, monkeypatch):
    root, _control, proposal, _normalized = draft_root
    with host(draft_root).activate():
        wanted = operations.prepare_request(root, request_id="admit", operation="strict.admit", arguments={"proposal": proposal})
        original = operations._invoke
        def lost_return(*args):
            original(*args)
            raise RuntimeError("synthetic loss after canonical claims publication")
        monkeypatch.setattr(operations, "_invoke", lost_return)
        receipt = operations.submit(root, wanted)
        assert receipt["status"] == "commit_unknown"
        actual = (root / "docs/research-claims.json").read_bytes()
        proof = operations.reconcile(root, "admit")
        assert proof["desired_state_proved"] and not proof["completion_proved"] and not proof["evidence_accepted"]
        assert (root / "docs/research-claims.json").read_bytes() == actual
        assert operations.inspect(root, "admit")["record"]["receipt"] == receipt


def test_active_host_selection_mismatch_refuses_historical_replay(draft_root):
    root, _control, proposal, _normalized = draft_root
    selected = host(draft_root)
    with selected.activate(), Workspace.open(root) as workspace:
        revision = workspace.mutations.revision()["revision_id"]
        receipt = workspace.strict.admit(proposal, request_id="admit", expected_revision=revision)
    actual = (root / "docs/research-claims.json").read_bytes()
    with replace(selected, actor_ref="other-caller").activate(), Workspace.open(root) as workspace:
        with pytest.raises(EvidenceWikiError):
            workspace.strict.admit(proposal, request_id="admit", expected_revision=revision)
    assert (root / "docs/research-claims.json").read_bytes() == actual and receipt["historical"]


@pytest.mark.parametrize("kind", ["missing_question", "extra_question", "question_text", "original_text", "original_ids", "request_schema", "request_kind", "primary_origin", "derived_origin", "unmapped"])
def test_exact_frozen_request_and_question_accounting_refuse_without_publication(draft_root, kind):
    root, control, proposal, _normalized = draft_root
    page = root / "wiki/questions/q.md"
    requirements = root / "docs/research-requirements.json"
    request_value = json.loads(requirements.read_bytes())
    if kind == "missing_question":
        page.unlink()
    elif kind == "extra_question":
        (page.parent / "extra.md").write_bytes(page.read_bytes())
    elif kind in {"question_text", "original_text", "original_ids", "unmapped"}:
        frontmatter, body = owner("verify_quotes").split_page(page.read_text())
        if kind == "question_text":
            frontmatter["question"] = "Changed text."
        elif kind == "original_text":
            frontmatter["metadata"]["original_text"] = "Changed original."
        elif kind == "original_ids":
            frontmatter["metadata"]["original_ids_json"] = '["other"]'
        else:
            del frontmatter["metadata"]["research_id"]
        page.write_text("---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n" + body)
    else:
        if kind == "request_schema":
            request_value["request"]["schema_version"] = "unsupported"
        elif kind == "request_kind":
            request_value["request"]["kind"] = "setup_plan"
        elif kind == "primary_origin":
            request_value["request"]["payload"]["questions"][0]["original_ids"] = ["other"]
        else:
            request_value["request"]["payload"]["derived_questions"] = [{"id": "detail", "text": "Derived question."}]
        requirements.write_bytes(canonical(request_value))
        update_policy(root)
    with host(draft_root).activate():
        value = intent(root, proposal)
        with MutationStore(root, control, value).locked() as store:
            seed(store, value)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, value, store)
            assert type(getattr(error.value, "error_code", None)) is str
    assert not (root / "docs/research-claims.json").exists()


@pytest.mark.parametrize("kind", ["record_hash", "unmapped_source", "observed_future", "source_future", "stale", "raw", "capture_kind", "quote"])
def test_source_bindings_and_age_refuse_without_changing_inventory(draft_root, kind):
    root, control, proposal, normalized = draft_root
    value = copy.deepcopy(proposal)
    evidence = value["inventory"]["claims"][0]["evidence"][0]
    if kind == "record_hash":
        evidence["record_sha256"] = "sha256:" + "1" * 64
    elif kind == "unmapped_source":
        evidence["source_id"] = "not-inventoried"
    elif kind == "observed_future":
        evidence["observed_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    elif kind in {"source_future", "stale"}:
        at = datetime.now(timezone.utc) + (timedelta(days=1) if kind == "source_future" else -timedelta(days=2))
        record = json.loads((root / "sources/manifest.jsonl").read_text().splitlines()[0])
        record["provenance"]["retrieved_at"] = at.isoformat()
        (root / "sources/manifest.jsonl").write_bytes(canonical(record) + b"\n")
        metadata, body = owner("verify_quotes").split_page(normalized.read_text())
        metadata["provenance"]["retrieved_at"] = at.isoformat()
        normalized.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + body)
        evidence["record_sha256"] = sha(normalized.read_bytes())
        evidence["observed_at"] = at.isoformat()
        if kind == "stale":
            update_policy(root, lambda policy: policy.update(max_source_age_seconds=86400))
    elif kind == "raw":
        (root / "raw/web/capture.md").write_text("Changed original bytes.\n")
    elif kind == "capture_kind":
        evidence["capture"] = "excerpt"
    else:
        evidence["quote"] = "This quote is not retained."
    with host(draft_root).activate():
        wanted = intent(root, value)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, wanted, store)
            assert type(getattr(error.value, "error_code", None)) is str
    assert not (root / "docs/research-claims.json").exists()


@pytest.mark.parametrize("path", ["research.yml", "docs/research-requirements.json", "sources/source-requests.jsonl", "sources/coverage/claims.json", "wiki/questions/claims.json", "raw/web/claims.json", "runs/control.json"])
def test_configured_claims_target_cannot_overwrite_input_or_lifecycle_paths(draft_root, path):
    root, control, proposal, _normalized = draft_root
    update_policy(root, lambda policy: policy.update(claims_path=path))
    before = observations(root)
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, wanted, store)
            assert type(getattr(error.value, "error_code", None)) is str
    assert all(observations(root)[name] == raw for name, raw in before.items())


def test_foreign_existing_target_is_preserved(draft_root):
    root, control, proposal, _normalized = draft_root
    target = root / "docs/research-claims.json"
    target.write_bytes(b'{"foreign_lifecycle":"preserve"}\n')
    before = target.read_bytes()
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, wanted, store)
            assert getattr(error.value, "details", {}).get("reason") == "claims_target_foreign_container"
    assert target.read_bytes() == before


def test_claims_gate_rechecks_target_after_acquiring_inner_lock(draft_root, monkeypatch):
    root, control, proposal, _normalized = draft_root
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            basis = recovery.prepare(root, wanted, store)
            invoked = owner("strict_evidence").sibling("_draft_admission")
            original = invoked.claims_gate
            target = root / "docs/research-claims.json"
            @contextmanager
            def changed(root):
                with original(root):
                    target.write_bytes(b'{"foreign":"concurrent"}\n')
                    yield
            monkeypatch.setattr(invoked, "claims_gate", changed)
            with pytest.raises(EvidenceWikiError):
                recovery.invoke(root, wanted, basis, store)
            assert target.read_bytes() == b'{"foreign":"concurrent"}\n'


@pytest.mark.parametrize("form", [b"", b"\0", b"unowned"])
def test_exact_native_claims_control_forms_are_bounded(draft_root, form):
    root, control, proposal, _normalized = draft_root
    (root / ".locks").mkdir(exist_ok=True)
    lock = root / ".locks/draft-admission.lock"
    lock.write_bytes(form)
    lock.chmod(0o600)
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            if form == b"unowned":
                with pytest.raises(Exception) as error:
                    recovery.prepare(root, wanted, store)
                assert type(getattr(error.value, "error_code", None)) is str
            else:
                basis = recovery.prepare(root, wanted, store)
                result = recovery.invoke(root, wanted, basis, store)
                assert result["accepted"] is False


@pytest.mark.parametrize("offset", [-1, 86401])
def test_unchanged_valid_source_is_refused_at_future_or_stale_frozen_clock(draft_root, offset):
    root, control, proposal, _normalized = draft_root
    update_policy(root, lambda policy: policy.update(max_source_age_seconds=86400))
    original_time = owner("_evidence_authority").timestamp(proposal["inventory"]["claims"][0]["evidence"][0]["observed_at"])
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                owner("_draft_admission").prepare_admission(root, proposal, observed_at=original_time + timedelta(seconds=offset))
            assert getattr(error.value, "details", {}).get("reason") == "source_age_invalid"
    assert not (root / "docs/research-claims.json").exists()


@pytest.mark.parametrize("path", ["normalized", "raw_sidecar", "manifest", "original", "question", "instruction"])
def test_changed_captured_dependency_refuses_before_publication_and_invalidates_proof(draft_root, path):
    root, control, proposal, normalized = draft_root
    changed = {"normalized": normalized, "raw_sidecar": root / "raw/web/capture.md.provenance.yml",
        "manifest": root / "sources/manifest.jsonl", "original": root / "docs/research-requirements.json",
        "question": root / "wiki/questions/q.md", "instruction": root / "AGENTS.md"}[path]
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            basis = recovery.prepare(root, wanted, store)
            prior = changed.read_bytes()
            changed.write_bytes(prior + b"\n")
            with pytest.raises(EvidenceWikiError):
                recovery.invoke(root, wanted, basis, store)
            assert not (root / "docs/research-claims.json").exists()
            changed.write_bytes(prior)
            recovery.invoke(root, wanted, basis, store)
            changed.write_bytes(prior + b"\n")
            proof = recovery.prove(root, wanted, basis, store)
            assert proof["present"] and not proof["desired"] and not proof["completion"]
            assert not proof["observation"]["dependencies_unchanged"]


@pytest.mark.parametrize("phase", ["before_rename", "directory_fsync"])
def test_native_publication_interruptions_preserve_or_prove_exact_unaccepted_inventory(draft_root, monkeypatch, phase):
    root, control, proposal, _normalized = draft_root
    prior = canonical(proposal["inventory"])
    target = root / "docs/research-claims.json"
    target.write_bytes(prior)
    changed = copy.deepcopy(proposal)
    changed["inventory"]["claims"][0]["text"] = "Retained revised draft observation."
    with host(draft_root).activate():
        wanted = intent(root, changed)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            basis = recovery.prepare(root, wanted, store)
            invoked = owner("strict_evidence").sibling("_draft_admission")
            publisher = invoked.sibling("_usage_materialization")
            actual_docs = (root / "docs").stat()
            if phase == "before_rename":
                original = publisher.os.rename
                def interrupted(source, destination, *args, **kwargs):
                    if destination == "research-claims.json":
                        raise OSError("synthetic interruption before claims publication")
                    return original(source, destination, *args, **kwargs)
                monkeypatch.setattr(publisher.os, "rename", interrupted)
                monkeypatch.setattr(publisher.os, "supports_dir_fd", publisher.os.supports_dir_fd | {interrupted})
            else:
                original = publisher.os.fsync
                def interrupted(descriptor):
                    info = publisher.os.fstat(descriptor)
                    if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (actual_docs.st_dev, actual_docs.st_ino):
                        raise OSError("synthetic directory durability interruption")
                    return original(descriptor)
                monkeypatch.setattr(publisher.os, "fsync", interrupted)
            with pytest.raises(EvidenceWikiError):
                recovery.invoke(root, wanted, basis, store)
            monkeypatch.undo()
            expected = prior if phase == "before_rename" else owner("_evidence_revision").canonical_bytes(changed["inventory"])
            assert target.read_bytes() == expected
            proof = recovery.prove(root, wanted, basis, store)
            assert proof["desired"] is (phase == "directory_fsync")
            assert not proof["completion"] and proof["observation"]["current_acceptance"] == "not_evaluated"


def test_staging_interruption_reuses_durable_clock_without_canonical_effect(draft_root, monkeypatch):
    root, control, proposal, _normalized = draft_root
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            original = store.stage_blob
            def interrupted(name, raw):
                if name == "draft-inventory":
                    raise OSError("synthetic interruption after retained clock")
                return original(name, raw)
            monkeypatch.setattr(store, "stage_blob", interrupted)
            with pytest.raises(OSError):
                recovery.prepare(root, wanted, store)
            observed = store.read()["owner_basis"]["observed_at"]
            retained = store.staged("draft-clock")
            assert json.loads(store.read_stage(retained))["observed_at"] == observed
            assert not (root / "docs/research-claims.json").exists()
            monkeypatch.setattr(store, "stage_blob", original)
            basis = recovery.prepare(root, wanted, store)
            result = recovery.invoke(root, wanted, basis, store)
            assert result["admitted_at"] == observed


def test_projected_result_quota_refuses_before_canonical_effect(draft_root, monkeypatch):
    root, control, proposal, _normalized = draft_root
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            monkeypatch.setattr(recovery, "MAX_RECORD_BYTES", 512)
            with pytest.raises(EvidenceWikiError) as error:
                recovery.prepare(root, wanted, store)
            assert error.value.error_code == "MUTATION_LIMIT"
    assert not (root / "docs/research-claims.json").exists()


def test_custom_jurisdictions_leaf_is_protected_from_claims_publication(draft_root):
    root, control, proposal, _normalized = draft_root
    configuration = yaml.safe_load((root / "research.yml").read_bytes())
    configuration["sources"]["jurisdictions_path"] = "docs/jurisdictions.json"
    configuration["strict_evidence"]["claims_path"] = "docs/jurisdictions.json"
    (root / "research.yml").write_text(yaml.safe_dump(configuration, sort_keys=False))
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, wanted, store)
            assert getattr(error.value, "details", {}).get("reason") == "claims_target_overlaps_original_or_control"
    assert not (root / "docs/jurisdictions.json").exists()


def test_direct_canonical_admission_cannot_skip_serialized_host_gate(draft_root):
    root, _control, proposal, _normalized = draft_root
    with pytest.raises(Exception) as error:
        owner("strict_evidence").run_admit_document(root, proposal)
    assert getattr(error.value, "details", {}).get("reason") == "serialized_admission_required"
    assert not (root / "docs/research-claims.json").exists()


@pytest.mark.parametrize("kind,reason", [
    ("target", "original_target_changed"), ("write_scope", "original_writable_scope_changed"),
    ("source_injection", "source_outside_original_scope"), ("file_companion", "source_outside_original_scope"),
    ("uri_scope", "original_source_scope_unsupported"), ("relative_scope", "original_source_scope_unsupported"),
])
def test_frozen_original_scope_constrains_broader_current_host_rights(draft_root, kind, reason):
    root, control, proposal, _normalized = draft_root
    path = root / "docs/research-requirements.json"
    frozen = json.loads(path.read_bytes())
    payload = frozen["request"]["payload"]
    if kind == "target":
        (root.parent / "other-workspace").mkdir()
        payload["target"]["relative_path"] = "other-workspace"
    elif kind == "write_scope":
        payload["authority"]["writable_roots"] = [str(root / "docs")]
    elif kind == "source_injection":
        payload["authority"]["source_scope"] = [str(root / "docs")]
    elif kind == "file_companion":
        payload["authority"]["source_scope"] = [str(root / "raw/web/capture.md")]
    elif kind == "uri_scope":
        payload["authority"]["source_scope"] = ["https://example.org/study"]
    else:
        payload["authority"]["source_scope"] = ["raw/web"]
    path.write_bytes(canonical(frozen))
    update_policy(root)
    original_inputs = observations(root)
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, wanted, store)
            assert getattr(error.value, "details", {}).get("reason") == reason
    assert not (root / "docs/research-claims.json").exists()
    assert all(observations(root)[name] == raw for name, raw in original_inputs.items())


def test_supported_derived_question_retains_exact_original_mapping(draft_root):
    root, control, proposal, _normalized = draft_root
    path = root / "docs/research-requirements.json"
    frozen = json.loads(path.read_bytes())
    derived = {"id": "detail", "text": "  ¿Cuál fue la observación? 日本語  ", "original_ids": ["q"]}
    frozen["request"]["payload"]["derived_questions"] = [derived]
    path.write_bytes(canonical(frozen))
    owner("intake_questions").run_intake_document(root, {"schema_version": "1.0", "questions": [
        {"id": "detail", "question": derived["text"], "origin": "caller", "metadata": {"research_id": "detail",
            "original_ids_json": '["q"]', "original_text": derived["text"]}}]}, dry_run=False, from_file_label="synthetic")
    update_policy(root)
    changed = copy.deepcopy(proposal)
    changed["inventory"]["questions"].append({"slug": "detail", "original_id": "q", "question": derived["text"].strip()})
    changed["originals"][0]["question_slugs"].append("detail")
    with host(draft_root).activate():
        wanted = intent(root, changed)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            basis = recovery.prepare(root, wanted, store)
            result = recovery.invoke(root, wanted, basis, store)
            assert result["question_slugs"] == ["detail", "q"] and result["original_ids"] == ["q"]


@pytest.mark.parametrize("kind", ["multi_origin", "computation"])
def test_unsupported_accounting_and_computation_profiles_refuse_explicitly(draft_root, kind):
    root, control, proposal, _normalized = draft_root
    if kind == "multi_origin":
        path = root / "docs/research-requirements.json"
        frozen = json.loads(path.read_bytes())
        frozen["request"]["payload"]["questions"].append({"id": "other", "text": "Other original."})
        frozen["request"]["payload"]["derived_questions"] = [{"id": "detail", "text": "Combined question.", "original_ids": ["q", "other"]}]
        path.write_bytes(canonical(frozen))
        update_policy(root)
    else:
        configuration = yaml.safe_load((root / "research.yml").read_bytes())
        configuration["computation"] = {"mode": "declared"}
        (root / "research.yml").write_text(yaml.safe_dump(configuration, sort_keys=False))
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, wanted, store)
            assert error.value.error_code == "DRAFT_ADMISSION_UNSUPPORTED"
    assert not (root / "docs/research-claims.json").exists()


def test_implicit_capture_sidecar_cannot_escape_exact_original_file_scope(draft_root):
    root, control, proposal, normalized = draft_root
    manifest = root / "sources/manifest.jsonl"
    record = json.loads(manifest.read_text().splitlines()[0])
    record["provenance"].pop("sidecar_path", None)
    manifest.write_bytes(canonical(record) + b"\n")
    metadata, body = owner("verify_quotes").split_page(normalized.read_text())
    metadata["provenance"].pop("sidecar_path", None)
    normalized.write_text("---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n" + body)
    proposal = copy.deepcopy(proposal)
    proposal["inventory"]["claims"][0]["evidence"][0]["record_sha256"] = sha(normalized.read_bytes())
    path = root / "docs/research-requirements.json"
    frozen = json.loads(path.read_bytes())
    frozen["request"]["payload"]["authority"]["source_scope"] = [str(root / "raw/web/capture.md")]
    path.write_bytes(canonical(frozen))
    update_policy(root)
    with host(draft_root).activate():
        wanted = intent(root, proposal)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            with pytest.raises(Exception) as error:
                recovery.prepare(root, wanted, store)
            assert getattr(error.value, "details", {}).get("reason") == "source_outside_original_scope"
    assert not (root / "docs/research-claims.json").exists()


def test_delayed_new_publication_rechecks_source_age_after_slow_validation(draft_root, monkeypatch):
    root, control, proposal, _normalized = draft_root
    update_policy(root, lambda policy: policy.update(max_source_age_seconds=86400))
    target = root / "docs/research-claims.json"
    prior = owner("_evidence_revision").canonical_bytes(proposal["inventory"])
    target.write_bytes(prior)
    changed = copy.deepcopy(proposal)
    changed["inventory"]["claims"][0]["text"] = "New delayed draft."
    with host(draft_root).activate():
        wanted = intent(root, changed)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            basis = recovery.prepare(root, wanted, store)
            invoked = owner("strict_evidence").sibling("_draft_admission")
            retrieved = owner("_evidence_authority").timestamp(proposal["inventory"]["claims"][0]["evidence"][0]["observed_at"])
            calls = 0
            def local_clock():
                nonlocal calls
                calls += 1
                return datetime.now(timezone.utc) if calls < 4 else retrieved + timedelta(seconds=86401)
            monkeypatch.setattr(invoked, "current_time", local_clock)
            with pytest.raises(EvidenceWikiError) as error:
                recovery.invoke(root, wanted, basis, store)
            assert error.value.details["reason"] == "source_age_invalid" and calls == 4
            assert target.read_bytes() == prior
            assert not list(target.parent.glob(".normalized-*"))


@pytest.mark.parametrize("expires", ["grant", "attestation"])
def test_live_authenticated_permission_expiry_refuses_replacement_with_unchanged_checkpoint(draft_root, tmp_path, monkeypatch, expires):
    root, control, proposal, normalized = draft_root
    directory = tmp_path / "permissions"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    configuration = yaml.safe_load((root / "research.yml").read_bytes())
    configuration.update(authority.config)
    authority.config = configuration
    (root / "research.yml").write_text(yaml.safe_dump(configuration, sort_keys=False))
    source_id = proposal["inventory"]["claims"][0]["evidence"][0]["source_id"]
    body, _files = authority.source(source_id, normalized=normalized.read_bytes())
    descriptor = {"schema_version": "evidence-source-revision/v1", "source_id": source_id, "parents": [],
        "normalized_path": normalized.relative_to(root).as_posix(), "evidence_root": None, "temporal": {}}
    files = {"source-record.json": authenticated_bytes(descriptor), descriptor["normalized_path"]: normalized.read_bytes()}
    revision = closure(files)
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    grant = body["grant"]["payload"]
    grant["source_revision"] = revision
    if expires == "grant":
        grant["expires_at"] = deadline.isoformat()
    body["grant"] = authenticate(grant, "owner", "usage")
    body["source_revision"] = revision
    scrub = body["scrub"]["payload"]
    scrub["sanitized_revision"] = revision
    body["scrub"] = authenticate(scrub, "runner", "scrubber")
    if expires == "attestation":
        authentication = body["scrub"]["authentication"]
        authentication["expires_at"] = deadline.isoformat()
        del authentication["signature"]
        message = b"evidence-attestation/v1\0" + authenticated_bytes({"payload": scrub, "authentication": authentication})
        authentication["signature"] = hmac.new(bytes.fromhex(KEYS["runner"]), message, hashlib.sha256).hexdigest()
    usage = owner("_evidence_usage")
    authority.transact(usage, "initialize")
    authority.transact(usage, "deposit", body, files)
    target = root / "docs/research-claims.json"
    prior = owner("_evidence_revision").canonical_bytes(proposal["inventory"])
    target.write_bytes(prior)
    changed = copy.deepcopy(proposal)
    changed["inventory"]["claims"][0]["text"] = "New permission-sensitive draft."
    selected = replace(host(draft_root), authority_file=authority.policy_path, state_directory=authority.host)
    with selected.activate():
        wanted = intent(root, changed)
        with MutationStore(root, control, wanted).locked() as store:
            seed(store, wanted)
            basis = recovery.prepare(root, wanted, store)
            checkpoint = (authority.host / "evidence-state.json").read_bytes()
            trust = authority.policy_path.read_bytes()
            invoked = owner("strict_evidence").sibling("_draft_admission")
            calls = 0
            def local_clock():
                nonlocal calls
                calls += 1
                return datetime.now(timezone.utc) if calls < 4 else deadline
            monkeypatch.setattr(invoked, "current_time", local_clock)
            with pytest.raises(EvidenceWikiError) as error:
                recovery.invoke(root, wanted, basis, store)
            assert error.value.details["reason"] == "source_use_not_permitted" and calls == 4
            assert target.read_bytes() == prior
            assert (authority.host / "evidence-state.json").read_bytes() == checkpoint
            assert authority.policy_path.read_bytes() == trust
            assert not list(target.parent.glob(".normalized-*"))
