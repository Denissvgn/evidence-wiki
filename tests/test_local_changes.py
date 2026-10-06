"""Native current rights and retained originals bound local comparisons."""

import copy
import hashlib
import json
from dataclasses import replace

import pytest
import yaml

from evidence_wiki import Onboarding, Workspace
from evidence_wiki import change_operations as changes
from evidence_wiki import source_capture_operations as capture
from evidence_wiki.errors import EvidenceWikiError, UsageError
from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_contracts import canonical
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_capture_contracts import from_bytes
from tests._execution_fixture import authenticate, identifier
from tests._usage_fixture import UsageFixture
from tests.test_native_capture_revision import metadata
from tests.test_research_planning import request


def sha(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("EVIDENCE_WIKI_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("EVIDENCE_WIKI_STATE_DIR", raising=False)
    root = tmp_path / "workspace"
    declared = request(tmp_path)
    declared["request"]["payload"]["authority"]["source_scope"] = [str(root / "raw")]
    with Onboarding.open(allowed_roots=[tmp_path], allow=["apply"]) as onboarding:
        assert onboarding.apply(onboarding.plan(declared))["setup_ready"]
    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    authority = UsageFixture(authority_dir, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root": str(root)})
    config = yaml.safe_load((root / "research.yml").read_bytes())
    strict = copy.deepcopy(config["strict_evidence"])
    strict["instructions"][changes.REQUIREMENTS] = sha((root / changes.REQUIREMENTS).read_bytes())
    config.update(authority.config)
    config["strict_evidence"] = strict
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    authority.policy["strict_workspaces"] = {authority.binding: strict}
    authority.save_policy()
    authority.config = config
    authority.transact(owner("_evidence_usage"), "initialize")
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    context = HostContext([root], {"workspace.open", "change_operations.current"}, "host", [],
        mutation_control_directory=control, authority_file=authority.policy_path, state_directory=authority.host)
    return root, authority, context


def deposit(workspace, raw, *, title="Observation", parents=None, retrieval=True):
    root, authority, _ = workspace
    path = "raw/web/measured.txt"
    supplied = metadata(title=title, parents=parents or [],
        sanitization={"required": True, "approved_revision_id": None, "receipt_ref": None})
    files = capture.protected_artifacts(raw, supplied, path=path)
    revision = owner("_record_artifacts").closure_identity(files)
    source_id = owner("source_inventory").stable_id(path)
    body, _ = authority.source(source_id, retrieval=retrieval)
    body["source_revision"] = revision
    body["grant"]["payload"]["source_revision"] = revision
    body["grant"] = authenticate(body["grant"]["payload"], "owner", "usage")
    body["scrub"]["payload"]["sanitized_revision"] = revision
    body["scrub"] = authenticate(body["scrub"]["payload"], "runner", "scrubber")
    authority.transact(owner("_evidence_usage"), "deposit", body, files)
    supplied["sanitization"]["approved_revision_id"] = revision
    supplied["sanitization"]["receipt_ref"] = "host-deposit"
    profile = from_bytes(raw, supplied)
    provenance = capture.provenance(profile)
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_bytes(raw)
    (root / (path + ".provenance.yml")).write_bytes(canonical(provenance))
    manifest = {"id": source_id, "kind": "host_capture", "raw_paths": [path], "provenance": provenance}
    (root / "sources/manifest.jsonl").write_bytes(canonical(manifest) + b"\n")
    return source_id, revision, body


def pair(source_id, revision):
    return [{"before_source_id": source_id, "before_revision": revision, "after_source_id": source_id}]


@pytest.mark.parametrize("raw,title,dimension", [(b"Changed 0.81 ratio.", "Observation", "raw_content"),
    (b"Measured 0.74 ratio.", "Changed title", "capture_provenance")])
def test_retained_exact_bytes_and_provenance_comparison_is_read_only(workspace, raw, title, dimension):
    root, authority, context = workspace
    source_id, prior, _ = deposit(workspace, b"Measured 0.74 ratio.")
    _, current, _ = deposit(workspace, raw, title=title, parents=[prior])
    before = owner("_evidence_revision").capture_workspace(root)
    with context.activate(), Workspace.open(root) as handle:
        result = handle.research.changes(pairs=pair(source_id, prior))
    assert result["sources"][0]["dimensions"][dimension]["state"] == "changed"
    assert result["sources"][0]["identical_raw_bytes_different_provenance"] is (dimension == "capture_provenance")
    assert result["requirements"]["original_bytes"]["state"] == "identical"
    assert result["affected_source_ids"] == [source_id]
    assert result["remote_refresh"] == "not_observed" and not result["evidence_accepted"]
    assert dict(owner("_evidence_revision").capture_workspace(root).files) == dict(before.files)
    with owner("_evidence_usage").current_view(root, authority.config) as live:
        assert prior in live.state.revisions and current in live.state.revisions


def test_revoked_prior_and_unknown_capture_never_disclose_hashes(workspace):
    root, authority, context = workspace
    source_id, prior, old = deposit(workspace, b"Measured 0.74 ratio.")
    deposit(workspace, b"Measured 0.81 ratio.", parents=[prior])
    with context.activate(), Workspace.open(root) as handle:
        unknown = handle.research.changes(pairs=pair(source_id, "sha256:" + "f" * 64))
    authority.revoke(owner("_evidence_usage"), old)
    with context.activate(), Workspace.open(root) as handle:
        result = handle.research.changes(pairs=pair(source_id, prior))
    assert all(row["before"]["availability"] == "denied" and row["before"]["observed_basis"] is None
        and row["before"]["value_sha256"] is None and not row["before"]["evidence"]
        for row in result["sources"][0]["dimensions"].values())
    assert all(row["state"] == "unavailable" for row in unknown["sources"][0]["dimensions"].values())


def test_missing_record_and_current_denial_are_not_negative_facts(workspace):
    root, _, context = workspace
    with context.activate(), Workspace.open(root) as handle:
        missing = handle.research.changes(pairs=pair("unknown", "sha256:" + "a" * 64))
    assert not missing["coverage"]["global_absence_inferred"]
    assert all(row["state"] == "unavailable" for row in missing["sources"][0]["dimensions"].values())
    source_id, revision, _ = deposit(workspace, b"Private observation.", retrieval=False)
    with context.activate(), Workspace.open(root) as handle:
        denied = handle.research.changes(pairs=pair(source_id, revision))
    assert all(row["state"] == "denied" and row["after"]["value_sha256"] is None
        for row in denied["sources"][0]["dimensions"].values())


def test_grant_lifetime_and_private_revision_bound(workspace):
    root, _, context = workspace
    with replace(context, grants=frozenset({"workspace.open"})).activate(), Workspace.open(root) as handle:
        with pytest.raises(EvidenceWikiError):
            handle.research.changes(pairs=pair("missing", "sha256:" + "a" * 64))
    with context.activate(), Workspace.open(root) as handle:
        with pytest.raises(EvidenceWikiError):
            handle.research.changes(pairs=pair("missing", "sha256:" + "a" * 64), before_requirements_revision_id="unknown")
    with pytest.raises(EvidenceWikiError):
        handle.research.changes(pairs=pair("missing", "sha256:" + "a" * 64))


@pytest.mark.parametrize("pairs", [[], pair("x", "invalid"), pair("x", "sha256:" + "a" * 64) * 2,
    [{**pair("x", "sha256:" + "a" * 64)[0], "approved": True}],
    [pair(str(i * 2), "sha256:" + "a" * 64)[0] | {"after_source_id": str(i * 2 + 1)} for i in range(17)]])
def test_invalid_selection_precedes_authority(pairs, monkeypatch):
    monkeypatch.setattr(changes, "current_context", lambda: pytest.fail("Invalid selection reached authority."))
    with pytest.raises(UsageError):
        changes.current(None, pairs=pairs)


@pytest.mark.parametrize("path", ["raw/web/measured.txt", "wiki/questions/q1.md"])
def test_closed_selection_refuses_workspace_change_during_projection(workspace, monkeypatch, path):
    root, _, context = workspace
    source_id, revision, _ = deposit(workspace, b"Measured 0.74 ratio.")
    original = changes._project
    def changed(observed):
        result = original(observed)
        (root / path).write_bytes(b"Changed after observation.")
        return result
    monkeypatch.setattr(changes, "_project", changed)
    with context.activate(), Workspace.open(root) as handle, pytest.raises(EvidenceWikiError):
        handle.research.changes(pairs=pair(source_id, revision))


def test_actual_append_reports_original_preservation_and_requirement_changes(workspace):
    from evidence_wiki.mutation_contracts import digest
    from evidence_wiki.requirements_projection import ENVELOPE, PROPOSAL, criteria_identity
    from tests.test_mutation_operations import GRANTS

    root, _, context = workspace
    base = json.loads((root / changes.REQUIREMENTS).read_bytes())
    addition = {"id": "derived1", "text": "What contrary primary evidence exists?", "original_ids": ["q1"],
        "facet_refs": [{"original_id": "q1", "facet_id": "primary"}]}
    envelope = {"schema_version": ENVELOPE, "policy_ref": "frozen-scope", "base_sha256": digest(base),
        "criteria_sha256": criteria_identity(base), "allowed_original_ids": ["q1"],
        "allowed_questions": [{key: addition[key] for key in ("text", "original_ids", "facet_refs")}],
        "max_additions": 1, "max_total_questions": 10}
    proposal = {"schema_version": PROPOSAL, "request_id": "append", "parent_sha256": digest(base), "additions": [addition]}
    selected = replace(context, writable_roots=(root,), grants=context.grants | GRANTS | {
        "requirements_operations.prepare_revision", "requirements_operations.revise",
        "requirements.envelope." + digest(envelope)[7:]})
    with selected.activate(), Workspace.open(root) as handle:
        receipt = handle.research.requirements.revise(handle.research.requirements.prepare_revision(proposal, envelope))
        assert receipt["status"] == "committed"
        result = handle.research.changes(pairs=pair("missing", "sha256:" + "a" * 64))
        selected_revision = json.loads((root / changes.REQUIREMENTS).read_bytes())["revisions"][-1]["revision_id"]
        latest = handle.research.changes(pairs=pair("missing", "sha256:" + "a" * 64),
            before_requirements_revision_id=selected_revision)
    assert result["requirements"]["original_bytes"]["state"] == "identical"
    assert result["requirements"]["effective_requirements"]["state"] == "changed"
    assert result["requirements"]["revision_receipts"]["state"] == "changed"
    assert all(row["state"] == "identical" for row in latest["requirements"].values())


def test_noncanonical_original_is_refused_without_rewriting_its_bytes(workspace):
    root, authority, context = workspace
    document = json.loads((root / changes.REQUIREMENTS).read_bytes())
    raw = json.dumps(document, indent=3).encode() + b"\n"
    (root / changes.REQUIREMENTS).write_bytes(raw)
    expected = sha(raw)
    authority.policy["strict_workspaces"][authority.binding]["instructions"][changes.REQUIREMENTS] = expected
    authority.save_policy()
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config["strict_evidence"]["instructions"][changes.REQUIREMENTS] = expected
    (root / "research.yml").write_text(yaml.safe_dump(config))
    with context.activate(), Workspace.open(root) as handle, pytest.raises(UsageError) as caught:
        handle.research.changes(pairs=pair("missing", "sha256:" + "a" * 64))
    assert caught.value.error_code == "SOURCE_CHANGE_REFUSED"
    assert caught.value.details["reason"] == "requirements_lineage_unavailable"
    assert (root / changes.REQUIREMENTS).read_bytes() == raw
