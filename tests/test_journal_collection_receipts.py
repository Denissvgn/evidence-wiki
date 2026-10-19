"""Explicit receipt selection propagates through both read-only evidence joins."""

from types import SimpleNamespace

import pytest

from evidence_wiki import _retained_review_evidence as evidence
from evidence_wiki import execution_session
from evidence_wiki._review_dependency_json import digest
from evidence_wiki.errors import EvidenceWikiError, UsageError
from evidence_wiki.research_journal_collections import RECEIPT
from evidence_wiki.research_journal_contracts import JOURNAL

HASH = "sha256:" + "1" * 64


def _record(index, kind, data):
    return {"schema_version": JOURNAL, "kind": kind, "binding_id": HASH, "request_id": "request",
        "sequence": index, "previous_record_ref": None, "data": data, "authentication": None}


def _inert_session(monkeypatch):
    """Substitute owner boundaries only; create no native workspace or authority."""
    binding = {"fixture": "inert"}
    review = {"schema_version": "evidence-strict-review/v2", "claim_id": "claim", "basis_id": HASH,
        "generator": {"payload": {}}, "verdicts": {"support": "pass"}}
    envelope = {"payload": {"body": {"review": review}}}
    observation = {"attempt_id": "attempt", "state": "succeeded", "final_accounting": True,
        "config_ref": "critic", "controller_ref": "critic-controller", "output": {"verdicts": review["verdicts"]}}
    applied = {"operation": "evaluate", "claim_id": "claim", "attempt_id": "attempt", "profile_ref": "critic",
        "original_id": "original", "passed": True, "strict_basis_id": HASH}
    records = [
        _record(0, "execution_attempt", {"attempt": {"attempt_id": "attempt"}, "claim_id": "claim",
            "original_id": "original", "review_context": {}}),
        _record(1, "execution_observation", {"observation": observation}),
        _record(2, "execution_application", {**applied, "envelope": envelope}),
        _record(3, "execution_native", {"operation": "strict.register_review", "arguments": {"envelope": envelope},
            "reservation": {"attempt_id": "native-attempt"}}),
        _record(4, "execution_native_observation", {"observation": {"attempt_id": "native-attempt"},
            "error_code": None, "result": {"operation": "strict.register_review", "status": "committed"}}),
        _record(5, "execution_applied", applied),
    ]
    records.extend(_record(index + 6, "fixture_padding", {"chunk": "x" * 60000}) for index in range(20))
    attempt = SimpleNamespace(config_ref="critic", run_id="run", binding_id=digest(binding),
        job={"task_role": "critic", "operation": "evaluate"}, input_document={
            "complete_review_delivery": {"packet": {}, "packet_sha256": digest({})}}, document=lambda: {})
    profile = SimpleNamespace(role=lambda ref: SimpleNamespace(role="evaluator", controller_ref="critic-controller"),
        role_scopes=SimpleNamespace(scope=lambda ref: SimpleNamespace(task_role="critic")))
    session = SimpleNamespace(root=None, run_id="run", binding=binding, _check=lambda operation: None,
        execution=SimpleNamespace(attempt=lambda value: attempt, profile=profile, authenticate=lambda value, supplied: value,
            review_signer=SimpleNamespace(generator=SimpleNamespace(controller_ref="generator-controller"))))
    revisions = SimpleNamespace(capture_workspace=lambda root: SimpleNamespace(revision_id=HASH), content_id=lambda *args: HASH)
    native = {"state": "ineligible", "reason": "unsupported_contract", "current": {}, "strict": None,
        "publication": None, "usage_authority": None, "custody": None,
        "original_controller_ref": "critic-controller", "original_generator_controller_ref": "generator-controller"}
    owners = {"_strict_contract": SimpleNamespace(review_document=lambda value: value, artifact_id=lambda value: HASH),
        "_evidence_revision": revisions,
        "_strict_evidence": SimpleNamespace(retained_review_eligibility=lambda *args, **kwargs: dict(native))}
    monkeypatch.setattr(evidence, "owner", owners.__getitem__)
    monkeypatch.setattr(execution_session, "retained", lambda supplied: records)
    monkeypatch.setattr(execution_session, "_model_review_observed", lambda *args: True)
    monkeypatch.setattr(execution_session, "_observe_current", lambda supplied: (
        {"accounting": {"original_outcomes": [{"original_id": "original"}]}}, SimpleNamespace(finish=lambda: None), [], records))
    monkeypatch.setattr(execution_session, "_qualifications", lambda *args, **kwargs: {
        "profiles": [{"profile_ref": "critic", "qualified": True, "independent": True}]})
    selector = {"run_id": "run", "record_sha256": evidence._reference(records[5])}
    coordinate = {"host_id": "host", "case_id": "case", "cycle": "change", "claim_id": "claim", "config_ref": "critic"}
    return session, records, selector, coordinate


def test_explicit_collection_receipts_reach_original_and_final_sdk_facts(monkeypatch):
    session, records, selector, coordinate = _inert_session(monkeypatch)
    result = execution_session.retained_review_evidence(session, selector, coordinate, journal_collection=True)
    original = result["original"]["journal_receipt"]
    final = result["journal_receipt"]
    assert original == final
    assert final["schema_version"] == RECEIPT and final["records_bytes"] > 1_048_576
    assert final["record_count"] == len(records) and final["head_ref"] == evidence._reference(records[-1])
    assert final["run_id"] == session.run_id and final["binding_id"] == digest(session.binding)
    assert result["state"] == "ineligible"


def test_legacy_default_retains_the_original_one_mib_receipt_limit(monkeypatch):
    session, records, selector, coordinate = _inert_session(monkeypatch)
    with pytest.raises(UsageError) as refused:
        execution_session.retained_review_evidence(session, selector, coordinate)
    assert refused.value.error_code == "MUTATION_LIMIT"
    small = evidence._journal_receipt(session, records[:1])
    assert "schema_version" not in small and small["records_bytes"] < 1_048_576


@pytest.mark.parametrize("selected", [None, 1, "yes", {}, []])
def test_collection_receipt_selection_is_an_explicit_boolean(monkeypatch, selected):
    session, records, selector, coordinate = _inert_session(monkeypatch)
    with pytest.raises(EvidenceWikiError):
        execution_session.retained_review_evidence(session, selector, coordinate, journal_collection=selected)
