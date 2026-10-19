"""Per-service observations cannot replace current selection or original custody."""

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import _retained_review_evidence as parent
from evidence_wiki import execution_session
from evidence_wiki.errors import EvidenceWikiError
from tests.test_strict_evidence import CORE

HASH = "sha256:" + "1" * 64
BASIS = "sha256:" + "2" * 64
PRODUCER = "sha256:" + "3" * 64
REVIEW = "sha256:" + "4" * 64
SELECTED = "sha256:" + "5" * 64


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    payload = {"schema_version": "evidence-strict-review/v3", "claim_id": "claim", "basis_id": BASIS,
        "generator": {"payload": {}}, "verdicts": dict.fromkeys(CORE.sibling("_strict_contract").CHECKS, "pass"),
        "observation": {"producer_id": PRODUCER}, "delivery": {"binding_sha256": HASH}}
    envelope = {"payload": {"body": {"review": payload}}}
    trust = {"content_hash": HASH, "policy": {"policy_id": "policy", "policy_revision": "1", "expires_at": "2027-01-01T00:00:00Z"}}
    view = SimpleNamespace(trust=trust, now=datetime.now(timezone.utc), state=SimpleNamespace(
        strict_reviews={REVIEW: {"envelope": deepcopy(envelope), "authority": {"content_hash": HASH}}},
        state_id="state", binding=HASH, checkpoint=HASH))
    row = {"claim": {"id": "claim", "question_slug": "question"}, "accepted": True,
        "review": {"review_id": SELECTED, "passed": True}}
    report = {"basis_id": HASH, "review_producer_id": PRODUCER, "claims": [row]}
    current = {"dependency_basis": {"basis_id": BASIS, "semantic_material_id": HASH}}
    calls = []
    proof = SimpleNamespace(assert_review_envelope=lambda supplied: calls.append(("envelope", supplied)),
        original_snapshot=lambda: {"original": True}, assert_current=lambda: calls.append(("closing", None)))
    monkeypatch.setattr(CORE, "configuration", lambda root: {})
    monkeypatch.setattr(CORE, "resolve_policy", lambda root, config: {"schema_version": "evidence-strict-policy/v2"})
    monkeypatch.setattr(CORE, "review_document", lambda supplied: supplied)
    monkeypatch.setattr(CORE, "artifact_id", lambda supplied: REVIEW)
    monkeypatch.setattr(CORE.sibling("_evidence_usage"), "current_view", lambda *args: nullcontext(view))
    monkeypatch.setattr(CORE, "evaluate", lambda *args, **kwargs: report)
    monkeypatch.setattr(CORE, "publication", lambda *args, **kwargs: {"verdict": "blocked_on_sources"})
    monkeypatch.setattr(CORE, "selection_inputs", lambda *args, **kwargs: (object(), {}, HASH))
    monkeypatch.setattr(CORE, "_review_producer", lambda: SimpleNamespace(producer_id=PRODUCER))
    monkeypatch.setattr(CORE, "dependency_current", lambda *args: current)
    monkeypatch.setattr(CORE, "_dependency_authenticated", lambda *args: {"controller": "reviewer-controller"})
    monkeypatch.setattr(CORE, "verify_attestation", lambda *args: {"authenticated": True, "controller": "generator-controller"})
    monkeypatch.setattr(CORE, "_dependency_original", lambda *args, **kwargs: proof)
    monkeypatch.setattr(CORE, "_dependency_closing", lambda *args: calls.append(("current_closing", None)))
    readiness = {"verdict": "ship"}
    monkeypatch.setattr(CORE.sibling("_selected_publication"), "run_selected_publication", lambda *args, **kwargs: readiness)
    return SimpleNamespace(root=tmp_path, envelope=envelope, payload=payload, row=row, view=view,
        current=current, readiness=readiness, calls=calls)


def observe(case):
    return CORE.retained_review_eligibility(case.root, case.envelope, claim_id="claim", expected_review_id=REVIEW)


def test_explicit_nonselected_service_can_pass_while_other_claims_block_publication(native_case):
    result = observe(native_case)
    assert result["state"] == "eligible"
    assert result["reason"] == "validated_retained_review"
    assert result["current"]["selected_review_id"] == SELECTED != REVIEW
    assert result["publication"]["verdict"] == "blocked_on_sources"
    assert result["original_authenticated"] is True
    assert native_case.calls[-1] == ("closing", None)


@pytest.mark.parametrize("selected", ["fail", "unknown", "missing", "premise", "mechanical"])
def test_original_pass_never_overrides_current_selected_or_live_failure(native_case, selected):
    native_case.row["accepted"] = False
    native_case.row["review"].update(passed=False, reason="current_" + selected)
    result = observe(native_case)
    assert result["state"] == "ineligible" and result["reason"] == "claim_not_accepted"


def test_current_human_or_lifecycle_blocker_prevents_per_service_credit(native_case):
    native_case.readiness["verdict"] = "blocked_on_sources"
    result = observe(native_case)
    assert result["state"] == "ineligible" and result["reason"] == "claim_not_accepted"


def test_explicit_original_negative_is_not_rehabilitated_by_newer_pass(native_case):
    native_case.payload["verdicts"]["support"] = "unknown"
    native_case.view.state.strict_reviews[REVIEW]["envelope"] = deepcopy(native_case.envelope)
    assert observe(native_case)["state"] == "ineligible"


def test_exact_registered_envelope_is_required_even_with_same_review_payload(native_case):
    native_case.envelope["caller_wrapper"] = "changed"
    result = observe(native_case)
    assert result["state"] == "ineligible" and result["reason"] == "original_authentication_failed"
    assert result["original_authenticated"] is False


def test_current_changed_basis_refuses_before_original_reuse_credit(native_case):
    native_case.current["dependency_basis"]["basis_id"] = HASH
    result = observe(native_case)
    assert result["state"] == "ineligible" and result["reason"] == "basis_changed"
    assert result["original_authenticated"] is False


def test_expired_original_service_stays_ineligible(native_case, monkeypatch):
    def expired(*args):
        raise CORE.refusal("strict_review_expired")
    monkeypatch.setattr(CORE, "_dependency_authenticated", expired)
    result = observe(native_case)
    assert result["state"] == "ineligible" and result["reason"] == "review_expired"


@pytest.mark.parametrize("selector", [
    {"run_id": "other", "record_sha256": HASH}, {"run_id": "run", "record_sha256": "bad"},
    {"run_id": "run", "record_sha256": HASH, "authority": True}, HASH,
])
def test_parent_selector_never_grants_run_or_record_authority(selector):
    coordinate = {"host_id": "host", "case_id": "case", "cycle": "first-change", "claim_id": "claim", "config_ref": "critic"}
    with pytest.raises(EvidenceWikiError):
        parent._selectors(SimpleNamespace(run_id="run"), selector, coordinate)


def test_ambiguous_authenticated_application_selection_refuses():
    with pytest.raises(EvidenceWikiError):
        parent._one([{"kind": "execution_applied"}, {"kind": "execution_applied"}], lambda row: True)


def test_parent_current_qualification_cannot_be_replaced_by_accepted_boolean(monkeypatch):
    records = [{"kind": "authenticated_original"}]
    original = {"config_ref": "critic", "controller_ref": "critic-controller", "envelope": {}, "review_id": REVIEW}
    monkeypatch.setattr(parent, "_selectors", lambda *args: None)
    monkeypatch.setattr(parent, "_original", lambda *args: (original, {"schema_version": "evidence-strict-review/v3"}, {"original_id": "original"}))
    monkeypatch.setattr(parent, "_journal_receipt", lambda *args: {})
    monkeypatch.setattr(execution_session, "retained", lambda session: records)
    monkeypatch.setattr(execution_session, "_observe_current", lambda session: (
        {"accounting": {"original_outcomes": [{"original_id": "original"}]}},
        SimpleNamespace(finish=lambda: None), [], records))
    monkeypatch.setattr(execution_session, "_qualifications", lambda *args, **kwargs: {"profiles": [
        {"profile_ref": "critic", "qualified": False, "independent": True}]})
    native = {"state": "eligible", "reason": "validated_retained_review", "current": {}, "strict": None,
        "publication": None, "usage_authority": None, "custody": None, "original_controller_ref": "critic-controller",
        "original_generator_controller_ref": "generator-controller"}
    monkeypatch.setattr(parent, "owner", lambda name: SimpleNamespace(capture_workspace=lambda root: SimpleNamespace(revision_id=HASH))
        if name == "_evidence_revision" else SimpleNamespace(retained_review_eligibility=lambda *args, **kwargs: native))
    session = SimpleNamespace(_check=lambda operation: None, root=None,
        execution=SimpleNamespace(review_signer=SimpleNamespace(generator=SimpleNamespace(controller_ref="generator-controller"))))
    result = parent.observe(session, {"record_sha256": HASH}, {"claim_id": "claim"})
    assert result["state"] == "unknown" and result["reason"] == "qualification_unavailable"
    assert "original" in result and "qualification" in result
