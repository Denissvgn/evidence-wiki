"""Checks that compare an execution stamp with "now" read the ordered clock that produced the stamp.

Each driver stubs only what precedes one check, so the held case proves that the stubs reach it. The beyond and future
cases prove that the check still refuses a real regression and a stamp this clock never produced.
"""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import (
    _ordered_clock,
    acquisition_budget,
    artifact_delivery_status,
    counter_authority,
    execution_continuations,
    execution_session,
    repair_slot_projection,
    resolution_evidence,
)
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.errors import UsageError
from tests.test_ordered_clock import SHIPPED_ALLOWANCE

BASIS = "sha256:" + "b" * 64


class Passed(Exception):
    """Raised by a stub placed right after the clock check: the check accepted the stamp."""


def passed(*_args, **_kwargs):
    raise Passed


def continuation_gate(monkeypatch, stamp):
    review = {"claim_id": "claim", "basis_id": BASIS, "reviewed_at": stamp.isoformat()}
    envelope = {"payload": {"body": {"review": review}}, "authentication": {"issued_at": stamp.isoformat()}}
    records = [{"kind": "execution_attempt", "data": {"attempt": {"attempt_id": "judge", "config_ref": "judge"}}},
        {"kind": "execution_application", "data": {"operation": "evaluate", "attempt_id": "judge", "envelope": envelope}}]
    session = SimpleNamespace(execution=SimpleNamespace(records=lambda *_: records,
        attempt=lambda document: SimpleNamespace(config_ref=document["config_ref"])))
    checked = []
    monkeypatch.setattr(execution_session, "_verify_review_controller", lambda _s, attempt, _e: checked.append(attempt.config_ref))
    gates = execution_continuations.authenticated_gates(session, {"reviews": [{"review_ref": execution_continuations.digest(review)}]},
        {"authentication": "pass"}, prepared={"snapshot": {"policy": {"max_review_age_seconds": 60}}})
    assert checked == ["judge"]
    return {"pass": True, "fail": False}[gates["authentication"]]


def resolution_vote(monkeypatch, stamp):
    payload = {"claim_id": "claim", "basis_id": BASIS, "reviewed_at": stamp.isoformat()}
    application = {"attempt_id": "judge-1", "operation": "evaluate", "passed": True, "claim_id": "claim",
        "strict_basis_id": BASIS, "envelope": {"payload": {"body": {"review": payload}}}}
    applied = {"operation": "evaluate", "attempt_id": "judge-1", "claim_id": "claim", "profile_ref": "judge",
        "strict_basis_id": BASIS, "passed": True}
    records = [{"kind": "execution_application", "data": application}, {"kind": "execution_applied", "data": applied}]
    attempt = SimpleNamespace(attempt_id="judge-1", config_ref="judge")
    profile = SimpleNamespace(disagreement=None, review_services=None,
        role=lambda _ref: SimpleNamespace(role="evaluator", controller_ref="reviewer"),
        role_scopes=SimpleNamespace(scope=lambda _ref: SimpleNamespace(task_role="evaluator")))
    session = SimpleNamespace(execution=SimpleNamespace(profile=profile, attempt=lambda _document: attempt),
        policy=SimpleNamespace(to_dict=lambda: {"required_evaluators": [{"profile_ref": "judge", "role": "evaluator"}]}))
    observed = {"observed_usage": {"model_calls": 1}, "state": "succeeded", "final_accounting": True,
        "started_utc": stamp.isoformat(), "reason": "observed"}
    monkeypatch.setattr(execution_session, "_attempt_rows",
        lambda _records: {"judge-1": {"attempt": {}, "original_id": "original", "claim_id": "claim"}})
    monkeypatch.setattr(execution_session, "_counter_stop", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(execution_session, "_latest_observation", lambda _records, _attempt_id: observed)
    monkeypatch.setattr(execution_session, "_model_review_observed", lambda *_: True)
    monkeypatch.setattr(execution_session, "_verify_review_controller", lambda *_: None)
    monkeypatch.setattr(resolution_evidence, "correlated", lambda _attempt, value, _profile: value)
    monkeypatch.setattr(counter_authority, "require_current_producer", lambda *_: None)
    monkeypatch.setattr(resolution_evidence, "owner", lambda _name: SimpleNamespace(timestamp=datetime.fromisoformat))
    monkeypatch.setattr(resolution_evidence, "_retained_pass", passed)
    current = {"qualification": {"profiles": []}, "canonical_release": {"publication": {
        "schema_version": "evidence-strict-publication/v3", "basis_id": BASIS, "questions": [{"slug": "question",
            "accepted": True, "original_id": "original", "claims": [{"id": "claim", "claim_basis_id": BASIS}]}]}}}
    facts = SimpleNamespace(prepared=lambda _claim: {"snapshot": {"claim": {}}}, config={},
        policy={"max_review_age_seconds": 60})
    try:
        result = resolution_evidence.questions(session, current, records, usage_view=object(), facts=facts)
    except Passed:
        return True
    assert result["question"]["reasons"] == ["mandatory_current_review_binding_or_age_invalid"], result
    return False


def delivery_reconcile(monkeypatch, stamp):
    intent = {"reservation": {"issued_at": stamp.isoformat()}}
    monkeypatch.setattr(artifact_delivery_status, "_selected", lambda _s, request_id, _operation: (request_id, [], intent))
    monkeypatch.setattr(artifact_delivery_status, "_receipt", lambda *_: {"input_sha256": "sha256:" + "c" * 64})
    monkeypatch.setattr(execution_session, "gate", lambda _session: nullcontext())
    monkeypatch.setattr(execution_session, "retained", lambda _session: [])
    elapsed = []

    def observation(_session, _request_id, *, seconds, succeeded):
        elapsed.append(seconds)
        raise Passed

    session = SimpleNamespace(execution=SimpleNamespace(ledger=lambda *_: {"closure": None}, native_observation=observation))
    try:
        artifact_delivery_status.reconcile(session, request_id="delivery-1")
    except Passed:
        assert elapsed[0] >= 0
        return True
    except UsageError as caught:
        assert caught.details == {"reason": "delivery_clock_regressed"}
        return False
    raise AssertionError("reconcile returned before its clock check")


def acquisition_reservation(monkeypatch, stamp):
    frozen = {"authority": {"allowed_actions": ["acquisition"], "source_scope": ["https://example.org/"]},
        "budgets": {"downloads": 2, "bytes": 100, "source_requests": 2, "seconds": 600}}
    appended = []
    monkeypatch.setattr(execution_session, "gate", lambda _session: nullcontext())
    monkeypatch.setattr(execution_session, "current", lambda _session: {"execution": {"source_bindings": []},
        "accounting": {"original_outcomes": [{"original_id": "original", "questions": [{"slug": "question"}]}]}})
    monkeypatch.setattr(execution_session, "child_id", lambda request_id, suffix: request_id + "." + suffix)
    monkeypatch.setattr(execution_session, "append", lambda _s, kind, _id, data: appended.append((kind, data)))
    monkeypatch.setattr(acquisition_budget, "original_request", lambda _session: ({"original": "question"}, frozen))
    monkeypatch.setattr(acquisition_budget, "scope_allows", lambda *_: True)
    monkeypatch.setattr(acquisition_budget, "history", lambda *_: [])
    monkeypatch.setattr(acquisition_budget, "baseline_time", lambda _session: stamp.isoformat())
    session = SimpleNamespace(run_id="run", policy=SimpleNamespace(to_dict=lambda: {"scope": {"question_ids": ["original"]},
        "continuation_limits": {"acquisition_attempts": 2}, "budgets": {"sources": 4}}))
    record = {"request_id": "source-request", "question_slugs": ["question"], "query_or_identifier": "https://example.org/a"}
    try:
        binding = acquisition_budget.reserve(session, "acquire-1", record, {"max_downloads_per_run": 2}, 10)
    except UsageError as caught:
        assert caught.details == {"reason": "original_acquisition_clock_regressed"} and not appended
        return False
    assert appended == [("execution_acquisition_reservation", binding)]
    assert binding["original_budget_started_utc"] == stamp.isoformat()
    return True


def service_slot(monkeypatch, stamp):
    review = {"claim_id": "claim", "basis_id": BASIS, "reviewed_at": stamp.isoformat(), "verdicts": {"criterion": "pass"}}
    identity = {"qualification_ref": "qualified"}
    application = {"attempt_id": "judge-1", "service_slot_ref": "slot", "profile_ref": "judge", "strict_basis_id": BASIS,
        "service_identity": identity,
        "envelope": {"payload": {"body": {"review": review}}, "authentication": {"issued_at": stamp.isoformat()}}}
    effect = {"operation": "evaluate", "attempt_id": "judge-1", "claim_id": "claim", "strict_basis_id": BASIS,
        "service_slot_ref": "slot", "profile_ref": "judge", "service_identity": identity}
    records = [{"kind": "execution_application", "data": application}, {"kind": "execution_applied", "data": effect}]
    attempt = SimpleNamespace(attempt_id="judge-1", config_ref="judge")
    monkeypatch.setattr(repair_slot_projection, "owned", lambda _s, _attempt_id: (
        {"service_slot_ref": "slot", "claim_id": "claim"}, attempt, {"state": "succeeded", "final_accounting": True}))
    monkeypatch.setattr(execution_session, "_verify_review_controller", lambda *_: None)
    monkeypatch.setattr(repair_slot_projection, "review_current", lambda *_: True)
    profile = SimpleNamespace(review_services=SimpleNamespace(check=lambda *_: None),
        role=lambda _ref: SimpleNamespace(controller_ref="reviewer"))
    session = SimpleNamespace(execution=SimpleNamespace(profile=profile),
        policy=SimpleNamespace(to_dict=lambda: {"required_evaluators": [{"profile_ref": "slot"}]}))
    prepared = {"result": {"basis_id": BASIS}, "snapshot": {"policy": {"max_review_age_seconds": 60}}}
    try:
        result = repair_slot_projection.current_first_passes(session, claim_id="claim", prepared=prepared, view={},
            records=records)
    except ExecutionError as caught:
        assert caught.args == ("repair_progress_service_slot_review_time_invalid",)
        return False
    assert result["complete"] and [row["service_slot_ref"] for row in result["first_passes"]] == ["slot"]
    return True


CHECKS = {"continuation_gate": continuation_gate, "resolution_vote": resolution_vote,
    "delivery_reconcile": delivery_reconcile, "acquisition_reservation": acquisition_reservation,
    "service_slot": service_slot}


@pytest.mark.parametrize("case", ["held", "beyond", "future"])
@pytest.mark.parametrize("check", sorted(CHECKS))
def test_stamp_check_reads_the_clock_that_produced_the_stamp(monkeypatch, check, case):
    monkeypatch.setattr(_ordered_clock, "_last", None)
    if case == "future":
        # No reading of this clock ever reached the stamp.
        stamp = datetime.now(timezone.utc) + timedelta(milliseconds=500)
    else:
        # This clock last read the stamp, then the host stepped back 500 ms, or 250 ms past the allowance.
        ahead = timedelta(milliseconds=500) if case == "held" else SHIPPED_ALLOWANCE + timedelta(milliseconds=250)
        stamp = datetime.now(timezone.utc) + ahead
        monkeypatch.setattr(_ordered_clock, "_last", stamp)
    assert stamp > datetime.now(timezone.utc)
    assert CHECKS[check](monkeypatch, stamp) is (case == "held")
