"""Independent adversarial oracles for durable execution boundaries."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as driver
from evidence_wiki.workflow_contracts import JOB, WorkflowContractError


def test_completed_job_overrun_stops_before_result_application(monkeypatch):
    frozen = {"human_review": False, "required_evaluators": [], "budgets": {
        "seconds": 100, "model_calls": 1, "tool_calls": 1, "tokens": 10, "cost_limit": "1", "claims": 1, "sources": 1}}
    session = SimpleNamespace(policy=SimpleNamespace(to_dict=lambda: frozen),
        execution=SimpleNamespace(profile=SimpleNamespace(total_steps=10, readability=None, artifact_delivery=None,
            role_scopes=None, review_services=None), ledger=lambda *_args: {}))
    view = {"policy_compatible": True, "qualification": {"reason": "qualified_scoped_profiles"}, "authority": {"terminal_grants_available": True}, "usage": {"claims": 0, "sources": 0},
        "execution": {"steps_used": 1, "pending": {"attempt_id": "job", "applied": False,
            "state": "succeeded", "reservation_occupied": False}}}
    monkeypatch.setattr(driver, "deadline", lambda *_args: datetime.now(timezone.utc) + timedelta(seconds=100))
    monkeypatch.setattr(driver.accounting, "projection", lambda *_args, **_kwargs: {
        "usage_unknown": False, "overrun": ["cost"]})
    monkeypatch.setattr(driver, "_terminal_action", lambda _session, _view, outcome, reason: {
        "operation": "terminal", "outcome": outcome, "reason": reason})
    action = driver._action(session, view, [{"kind": "execution_attempt",
        "data": {"attempt": {"attempt_id": "job"}}}])
    assert action["operation"] == "terminal"
    assert action["outcome"] == "stop_budget"
    assert action["reason"] == "budget_exhausted"


def test_interrupted_native_intent_reaches_canonical_reconciliation(monkeypatch, tmp_path):
    records = [{"kind": "execution_native", "data": {"operation": "strict.admit",
        "arguments": {}, "reservation": {"attempt_id": "native", "issued_at": datetime.now(timezone.utc).isoformat()}}}]
    invoked = []
    session = SimpleNamespace(root=tmp_path, run_id="run", execution=SimpleNamespace(ledger=lambda *_args: {},
        profile=SimpleNamespace(cleanup_timeout_seconds=1), native_observation=lambda *_args, **_kwargs: {}))
    monkeypatch.setattr(driver, "gate", lambda *_args: nullcontext())
    monkeypatch.setattr(driver, "retained", lambda _session: records)
    monkeypatch.setattr(driver, "_limits", lambda _session: {})
    monkeypatch.setattr(driver.accounting, "projection", lambda *_args, **_kwargs: {
        "usage_unknown": False, "remaining": {"tool_calls": 1}, "overrun": []})
    monkeypatch.setattr(driver, "deadline", lambda _session: datetime.now(timezone.utc) + timedelta(seconds=100))
    monkeypatch.setattr(driver, "append", lambda *_args: None)
    monkeypatch.setattr(driver, "ResearchObservation", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(driver, "input_basis", lambda _observation: "owned-basis")

    def reconcile(_control):
        invoked.append("reconcile")
        return {"status": "reconciled"}

    result = driver._native_call(session, "native", "strict.admit", {},
        lambda _control: pytest.fail("Uncertain native effect was redispatched"), recover=reconcile)
    assert result == {"status": "reconciled"}
    assert invoked == ["reconcile"]


def test_retained_application_revalidates_before_native_effect(monkeypatch):
    records = [{"kind": "execution_attempt", "data": {"attempt": {"attempt_id": "job"}}},
        {"kind": "execution_observation", "data": {"attempt_id": "job", "observation": {"state": "succeeded", "sequence": 1}}},
        {"kind": "execution_application", "data": {"attempt_id": "job", "operation": "generate",
            "original_id": "original", "proposal": {}, "coverage_links": []}}]
    attempt = SimpleNamespace(attempt_id="job", input_document={}, job={"schema_version": JOB, "basis_id": "old", "deadline_utc":
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    execution = SimpleNamespace(attempt=lambda _document: attempt, authenticate=lambda value, _attempt: value,
        profile=SimpleNamespace(review_services=None, calibration=None))
    mutated = []
    monkeypatch.setattr(driver, "retained", lambda _session: records)
    monkeypatch.setattr(driver, "gate", lambda *_args: nullcontext())
    monkeypatch.setattr(driver, "current", lambda _session: {"basis_id": "changed", "qualification": {"state": "unknown"}})
    monkeypatch.setattr(driver, "append", lambda *_args: None)
    monkeypatch.setattr(driver, "_mutation", lambda *_args: mutated.append(True))
    with pytest.raises(WorkflowContractError, match="refused"):
        driver._apply(SimpleNamespace(execution=execution), "job")
    assert not mutated


def test_old_exact_observation_replay_is_idempotent(monkeypatch):
    first = {"attempt_id": "job", "sequence": 1, "observed_utc": "2026-10-01T00:00:01+00:00"}
    second = {"attempt_id": "job", "sequence": 2, "observed_utc": "2026-10-01T00:00:02+00:00"}
    records = [{"kind": "execution_attempt", "data": {"attempt": {"attempt_id": "job"}}},
        {"kind": "execution_observation", "data": {"attempt_id": "job", "observation": first}},
        {"kind": "execution_observation", "data": {"attempt_id": "job", "observation": second}}]
    execution = SimpleNamespace(attempt=lambda _document: SimpleNamespace(attempt_id="job", document=lambda: {"attempt_id": "job"}),
        authenticate=lambda value, _attempt: value)
    appended = []
    monkeypatch.setattr(driver, "gate", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(driver, "retained", lambda _session: records)
    monkeypatch.setattr(driver, "append", lambda *_args: appended.append(True))
    assert driver.intake(SimpleNamespace(execution=execution), first, request_id="replay") == first
    assert not appended


@pytest.fixture
def native_effect_boundary(monkeypatch, tmp_path):
    records, observations = [], []
    session = SimpleNamespace(root=tmp_path, run_id="run", execution=SimpleNamespace(
        ledger=lambda *_args: {},
        profile=SimpleNamespace(cleanup_timeout_seconds=2, currency="USD", pricing_ref="local"),
        native_observation=lambda *_args, **kwargs: observations.append(kwargs) or kwargs))
    monkeypatch.setattr(driver, "gate", lambda *_args: nullcontext())
    monkeypatch.setattr(driver, "retained", lambda _session: records)
    monkeypatch.setattr(driver, "_limits", lambda _session: {})
    monkeypatch.setattr(driver.accounting, "projection", lambda *_args, **_kwargs: {
        "usage_unknown": False, "remaining": {"tool_calls": 1}, "overrun": []})
    monkeypatch.setattr(driver, "append", lambda _session, kind, _identifier, data:
        records.append({"kind": kind, "data": data}))
    return session, records, observations


@pytest.mark.parametrize("run_seconds,effect_seconds,expected_seconds", [
    (172800, None, 86400), (172800, 120, 120), (120, None, 120)])
def test_native_effect_uses_bounded_original_control_window(
        monkeypatch, native_effect_boundary, run_seconds, effect_seconds, expected_seconds):
    from evidence_wiki.operation_control import OperationControl

    session, records, observations = native_effect_boundary
    started = datetime.now(timezone.utc)
    run_end = started + timedelta(seconds=run_seconds)
    effect_end = started + timedelta(seconds=effect_seconds) if effect_seconds is not None else None
    monkeypatch.setattr(driver, "deadline", lambda _session: run_end)
    controls = []

    def invoke(control):
        assert type(control) is OperationControl
        controls.append(control)
        assert expected_seconds - 2 < control.timeout_seconds <= expected_seconds
        actual_end = datetime.fromisoformat(control.deadline_utc.replace("Z", "+00:00"))
        if expected_seconds == 86400:
            assert started + timedelta(seconds=86400) <= actual_end <= datetime.now(timezone.utc) + timedelta(seconds=86400)
        else:
            assert actual_end == (effect_end or run_end)
        assert 0 < control.remaining_seconds() <= expected_seconds
        return {"status": "observed"}

    try:
        assert driver._native_call(session, "native", "strict.admit", {}, invoke,
            effect_deadline=effect_end.isoformat() if effect_end else None) == {"status": "observed"}
        original_seconds = float(records[0]["data"]["reservation"]["reservation"]["seconds"])
        assert min(run_seconds, effect_seconds or run_seconds) - 2 < original_seconds <= min(run_seconds, effect_seconds or run_seconds)
        assert observations[0]["succeeded"] is True
        assert observations[0]["tool_calls"] == 1
    finally:
        for control in controls:
            control.close()


@pytest.mark.parametrize("expired_effect", [False, True])
def test_native_effect_refuses_expired_original_deadline(monkeypatch, native_effect_boundary, expired_effect):
    session, records, observations = native_effect_boundary
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(driver, "deadline", lambda _session: now + timedelta(seconds=172800 if expired_effect else -1))
    with pytest.raises(WorkflowContractError) as error:
        driver._native_call(session, "native", "strict.admit", {},
            lambda _control: pytest.fail("Expired effect was invoked"),
            effect_deadline=(now - timedelta(seconds=1)).isoformat() if expired_effect else None)
    assert error.value.error_code == "RESEARCH_BUDGET_EXHAUSTED"
    assert records == []
    assert observations == []


def test_native_recovery_keeps_cleanup_window_after_original_expiry(monkeypatch, native_effect_boundary):
    from evidence_wiki.operation_control import OperationControl

    session, records, observations = native_effect_boundary
    issued = datetime.now(timezone.utc) - timedelta(seconds=120)
    reservation = {"attempt_id": "native", "issued_at": issued.isoformat()}
    records.append({"kind": "execution_native", "data": {
        "operation": "strict.admit", "arguments": {}, "reservation": reservation}})
    monkeypatch.setattr(driver, "deadline", lambda _session: issued + timedelta(seconds=1))
    controls = []

    def recover(control):
        assert type(control) is OperationControl
        controls.append(control)
        assert control.timeout_seconds == session.execution.profile.cleanup_timeout_seconds
        assert 0 < control.remaining_seconds() <= 2
        return {"status": "reconciled"}

    try:
        assert driver._native_call(session, "native", "strict.admit", {},
            lambda _control: pytest.fail("Recovery redispatched an effect"), recover=recover) == {"status": "reconciled"}
        assert records[0]["data"]["reservation"] is reservation
        assert observations[0]["seconds"] >= 120
        assert observations[0]["tool_calls"] == 1
        assert records[-1]["data"]["elapsed_scope"] == "utc_lifetime_including_recovery_wait"
    finally:
        for control in controls:
            control.close()


@pytest.mark.parametrize("usage_unknown,overrun,tool_calls", [
    (True, [], 1), (False, ["cost"], 1), (False, [], 0)])
def test_long_native_effect_preserves_accounting_refusals(
        monkeypatch, native_effect_boundary, usage_unknown, overrun, tool_calls):
    session, records, observations = native_effect_boundary
    monkeypatch.setattr(driver, "deadline", lambda _session: datetime.now(timezone.utc) + timedelta(seconds=172800))
    monkeypatch.setattr(driver.accounting, "projection", lambda *_args, **_kwargs: {
        "usage_unknown": usage_unknown, "overrun": overrun, "remaining": {"tool_calls": tool_calls}})
    with pytest.raises(WorkflowContractError) as error:
        driver._native_call(session, "native", "strict.admit", {},
            lambda _control: pytest.fail("Unfunded effect was invoked"))
    assert error.value.error_code == "RESEARCH_BUDGET_EXHAUSTED"
    assert records == []
    assert observations == []


def test_native_effect_window_does_not_restart_after_precondition(monkeypatch, native_effect_boundary):
    session, _records, _observations = native_effect_boundary
    started = datetime.now(timezone.utc)
    clock = [started]

    class HostClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    monkeypatch.setattr(driver, "datetime", HostClock)
    monkeypatch.setattr(driver, "deadline", lambda _session: started + timedelta(seconds=172800))
    controls = []

    def precondition():
        clock[0] += timedelta(seconds=10)

    def invoke(control):
        controls.append(control)
        assert datetime.fromisoformat(control.deadline_utc.replace("Z", "+00:00")) == started + timedelta(seconds=86400)
        assert control.timeout_seconds == 86400
        return {"status": "observed"}

    try:
        driver._native_call(session, "native", "strict.admit", {}, invoke, effect_precondition=precondition)
    finally:
        for control in controls:
            control.close()
