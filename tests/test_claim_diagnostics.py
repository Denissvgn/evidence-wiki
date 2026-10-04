"""Actual native read-only mechanics and explicit semantic uncertainty."""

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from evidence_wiki import claim_diagnostics
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.onboarding_contract import _matches
from evidence_wiki.pack_discovery import owner
from tests import test_draft_admission as admission
from tests._execution_fixture import authenticate, canonical, closure, identifier
from tests._usage_fixture import UsageFixture
from tests.test_draft_admission import host, observations


@pytest.fixture
def draft_root(tmp_path, monkeypatch):
    return admission.draft_root.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("text", ["Reflectance was not 0.74.", "The quoted words prove every material is safe.",
    "Reflectance was 0.74 and the treatment cured every condition."])
def test_mechanical_match_cannot_approve_negation_or_irrelevant_claim(draft_root, text):
    root, _control, proposal, _record = draft_root
    proposal = copy.deepcopy(proposal)
    proposal["inventory"]["claims"][0]["text"] = text
    before = observations(root)
    with host(draft_root).activate():
        report = claim_diagnostics.current(root, proposal)
    assert observations(root) == before
    assert report["structural_result"] == "valid"
    assert report["accepted"] is False and report["published"] is False
    row = report["claims"][0]
    assert row["mechanical_result"] == "verified"
    assert row["accepted"] is False and row["semantic_approval"] == "not_evaluated"
    evidence = row["evidence"][0]
    assert evidence["mechanical"]["occurrence_count"] == 1
    assert evidence["mechanical"]["match_type"] == "exact"
    assert "quote" not in evidence and "quote" not in evidence["mechanical"]
    assert "Measured solar" not in json.dumps(report)
    assert row["original_id"] == "q" and report["original_ids"] == ["q"]
    _matches(report, claim_diagnostics.schema_document())


@pytest.mark.parametrize("failure,reason", [("revision", "source_record_revision_changed"),
    ("missing", "source_not_inventoried"), ("quote", "mechanical_grounding_unmatched")])
def test_local_failed_evidence_keeps_other_mechanical_result(draft_root, failure, reason):
    root, _control, proposal, _record = draft_root
    proposal = copy.deepcopy(proposal)
    other = copy.deepcopy(proposal["inventory"]["claims"][0])
    other["id"] = "other"
    if failure == "revision":
        other["evidence"][0]["record_sha256"] = "sha256:" + "0" * 64
    elif failure == "missing":
        other["evidence"][0]["source_id"] = "missing-source"
    else:
        other["evidence"][0]["quote"] = "Retained secret-shaped material absent from this record."
    proposal["inventory"]["claims"].append(other)
    before = observations(root)
    with host(draft_root).activate():
        report = claim_diagnostics.current(root, proposal)
    assert observations(root) == before
    assert report["claims"][0]["mechanical_result"] == "verified"
    row = report["claims"][1]
    assert row["mechanical_result"] == "not_established"
    assert row["evidence"][0]["reason"] == reason
    assert row["accepted"] is False
    rendered = json.dumps(report)
    assert "secret-shaped" not in rendered and "Measured solar" not in rendered
    assert str(root) not in rendered


@pytest.mark.parametrize("failure", ["cycle", "duplicate", "authority_field"])
def test_whole_invalid_structure_refuses_before_observation(draft_root, failure):
    root, _control, proposal, _record = draft_root
    proposal = copy.deepcopy(proposal)
    if failure == "cycle":
        proposal["inventory"]["claims"][0]["premises"] = ["observation"]
    elif failure == "duplicate":
        proposal["inventory"]["claims"].append(copy.deepcopy(proposal["inventory"]["claims"][0]))
    else:
        proposal["authorized"] = True
    before = observations(root)
    with host(draft_root).activate(), pytest.raises(EvidenceWikiError):
        claim_diagnostics.current(root, proposal)
    assert observations(root) == before


def test_read_host_grant_is_required_without_any_writer_grant(draft_root):
    root, _control, proposal, _record = draft_root
    before = observations(root)
    with host(draft_root, grants=set()).activate(), pytest.raises(EvidenceWikiError):
        claim_diagnostics.current(root, proposal)
    with host(draft_root, grants={"strict_evidence.run_operation.check"}).activate():
        report = claim_diagnostics.current(root, proposal)
    assert report["claims"][0]["mechanical_result"] == "verified"
    assert observations(root) == before


def test_missing_anchor_retains_native_match_failure_without_quote(draft_root):
    root, _control, proposal, _record = draft_root
    proposal = copy.deepcopy(proposal)
    proposal["inventory"]["claims"][0]["evidence"][0]["location_hint"] = "page 999"
    with host(draft_root).activate():
        report = claim_diagnostics.current(root, proposal)
    evidence = report["claims"][0]["evidence"][0]
    assert evidence["reason"] == "mechanical_grounding_unmatched"
    assert evidence["mechanical"]["result"] == "anchor_not_found"
    assert evidence["mechanical"]["global_occurrence_count"] == 1
    assert evidence["mechanical"]["occurrence_count"] == 0
    assert "Measured solar" not in json.dumps(report)


def test_malformed_source_manifest_is_whole_input_refusal(draft_root):
    root, _control, proposal, _record = draft_root
    (root / "sources/manifest.jsonl").write_bytes(b'{"id":"a","id":"b"}\n')
    before = observations(root)
    with host(draft_root).activate(), pytest.raises(EvidenceWikiError):
        claim_diagnostics.current(root, proposal)
    assert observations(root) == before


def test_source_age_expiring_at_final_boundary_refuses_entire_observation(draft_root, monkeypatch):
    root, _control, proposal, _record = draft_root
    native = owner("_claim_diagnostics").sibling("_draft_admission")
    observed = native.current_time()
    policy = native.sibling("_strict_evidence").resolve_policy(root, native.sibling("_strict_evidence").configuration(root))
    calls = iter([observed, observed + timedelta(seconds=policy["max_source_age_seconds"] + 1)])
    monkeypatch.setattr(native, "current_time", lambda: next(calls))
    before = observations(root)
    with host(draft_root).activate(), pytest.raises(EvidenceWikiError) as error:
        claim_diagnostics.current(root, proposal)
    assert error.value.details["reason"] == "source_age_invalid"
    assert observations(root) == before


def _protected_source(draft_root, tmp_path, monkeypatch, *, retrieval):
    root, _control, proposal, normalized = draft_root
    directory = tmp_path / "permissions"
    directory.mkdir()
    authority = UsageFixture(directory, monkeypatch)
    authority.root = root
    authority.binding = identifier("evidence-host-workspace/v1", {"root":str(root)})
    import yaml
    config = yaml.safe_load((root / "research.yml").read_bytes())
    config.update(authority.config)
    authority.config = config
    (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    source_id = proposal["inventory"]["claims"][0]["evidence"][0]["source_id"]
    body, _files = authority.source(source_id, normalized=normalized.read_bytes(), retrieval=retrieval)
    descriptor = {"schema_version":"evidence-source-revision/v1", "source_id":source_id, "parents":[],
        "normalized_path":normalized.relative_to(root).as_posix(), "evidence_root":None, "temporal":{}}
    files = {"source-record.json":canonical(descriptor), descriptor["normalized_path"]:normalized.read_bytes()}
    revision = closure(files)
    grant = body["grant"]["payload"]
    grant["source_revision"] = revision
    body["grant"] = authenticate(grant, "owner", "usage")
    body["source_revision"] = revision
    scrub = body["scrub"]["payload"]
    scrub["sanitized_revision"] = revision
    body["scrub"] = authenticate(scrub, "runner", "scrubber")
    usage = owner("_evidence_usage")
    authority.transact(usage, "initialize")
    authority.transact(usage, "deposit", body, files)
    selected = replace(host(draft_root), authority_file=authority.policy_path, state_directory=authority.host)
    return selected, authority, body


@pytest.mark.parametrize("retrieval", [True, False])
def test_real_signed_permission_is_mechanical_input_not_semantic_approval(draft_root, tmp_path, monkeypatch, retrieval):
    selected, authority, body = _protected_source(draft_root, tmp_path, monkeypatch, retrieval=retrieval)
    root, _control, proposal, _record = draft_root
    before = observations(root)
    checkpoint = (authority.host / "evidence-state.json").read_bytes()
    with selected.activate():
        report = claim_diagnostics.current(root, proposal)
    row = report["claims"][0]["evidence"][0]
    if retrieval:
        assert row["result"] == "verified"
        assert row["current_usage"] == {"eligible":True, "source_revision":body["source_revision"], "complete":True}
    else:
        assert row["result"] == "failed" and row["reason"] == "source_record_contract_invalid"
        assert row["mechanical"] is None and row["current_usage"] is None
        assert "Measured solar" not in json.dumps(report)
    assert report["accepted"] is False
    assert observations(root) == before
    assert (authority.host / "evidence-state.json").read_bytes() == checkpoint


def test_actual_host_path_denial_precedes_native_file_reads(draft_root, monkeypatch):
    root, _control, proposal, normalized = draft_root
    native = owner("_claim_diagnostics")
    revision = native.sibling("_evidence_revision")
    checked = HostContext.require_path
    reads = []
    read = revision.read_observed_file

    def restrict(context, value, *, write=False):
        if value == normalized:
            raise EvidenceWikiError("HOST_OPERATION_REFUSED", "Scoped path refused.", details={"reason":"host_path_outside_scope"})
        return checked(context, value, write=write)

    def observed_read(*args):
        reads.append(args[1])
        return read(*args)

    monkeypatch.setattr(HostContext, "require_path", restrict)
    monkeypatch.setattr(revision, "read_observed_file", observed_read)
    with host(draft_root).activate(), pytest.raises(EvidenceWikiError):
        claim_diagnostics.current(root, proposal)
    assert reads == []


def test_changed_record_after_mechanical_check_cannot_publish_stale_diagnostics(draft_root, monkeypatch):
    root, _control, proposal, normalized = draft_root
    draft = owner("_claim_diagnostics").sibling("_draft_admission")
    actual = draft._check_evidence

    def changed(*args):
        result = actual(*args)
        normalized.write_bytes(normalized.read_bytes() + b"\nChanged after observation.\n")
        return result

    monkeypatch.setattr(draft, "_check_evidence", changed)
    with host(draft_root).activate(), pytest.raises(EvidenceWikiError) as error:
        claim_diagnostics.current(root, proposal)
    assert error.value.details["reason"] == "diagnostic_inputs_changed"
    assert not (root / "docs/research-claims.json").exists()


def test_closed_report_schema_rejects_authority_or_text_injection(draft_root):
    root, _control, proposal, _record = draft_root
    with host(draft_root).activate():
        report = claim_diagnostics.current(root, proposal)
    report["claims"][0]["evidence"][0]["mechanical"]["quote"] = "untrusted expansion"
    with pytest.raises(EvidenceWikiError):
        _matches(report, claim_diagnostics.schema_document())


def test_actual_signed_permission_expiry_after_native_check_refuses_return(draft_root, tmp_path, monkeypatch):
    selected, authority, body = _protected_source(draft_root, tmp_path, monkeypatch, retrieval=True)
    root, _control, proposal, _record = draft_root
    native = owner("_claim_diagnostics")
    draft = native.sibling("_draft_admission")
    usage = native.sibling("_evidence_usage")
    actual = draft._check_evidence
    deadline = datetime.fromisoformat(body["grant"]["payload"]["expires_at"].replace("Z", "+00:00"))

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline if tz is not None else deadline.replace(tzinfo=None)

    def expired(*args):
        checked = actual(*args)
        monkeypatch.setattr(usage, "datetime", ExpiredClock)
        return checked

    monkeypatch.setattr(draft, "_check_evidence", expired)
    before = observations(root)
    checkpoint = (authority.host / "evidence-state.json").read_bytes()
    with selected.activate(), pytest.raises(EvidenceWikiError):
        claim_diagnostics.current(root, proposal)
    assert observations(root) == before
    assert (authority.host / "evidence-state.json").read_bytes() == checkpoint


def test_public_commitment_excludes_actual_denied_record_bytes(draft_root, tmp_path, monkeypatch):
    selected, _authority, _body = _protected_source(draft_root, tmp_path, monkeypatch, retrieval=False)
    root, _control, proposal, normalized = draft_root
    with selected.activate():
        first = claim_diagnostics.current(root, proposal)
    normalized.write_bytes(normalized.read_bytes() + b"\nWithheld changed bytes are not a public commitment.\n")
    with selected.activate():
        second = claim_diagnostics.current(root, proposal)
    assert first["basis_id"] == second["basis_id"]
    assert first["proposal_sha256"] == second["proposal_sha256"]
    assert first["claims"][0]["evidence"][0]["current_usage"] is None
    assert second["claims"][0]["evidence"][0]["current_usage"] is None
    assert second["claims"][0]["evidence"][0]["record_sha256"] == proposal["inventory"]["claims"][0]["evidence"][0]["record_sha256"]
    assert "Withheld changed" not in json.dumps(second)
