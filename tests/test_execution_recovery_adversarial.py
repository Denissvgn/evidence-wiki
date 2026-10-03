"""Independent adversarial oracles for durable execution boundaries."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as driver
from evidence_wiki.workflow_contracts import WorkflowContractError


def test_completed_job_overrun_stops_before_result_application(monkeypatch):
    frozen = {"human_review": False, "required_evaluators": [], "budgets": {
        "seconds": 100, "model_calls": 1, "tool_calls": 1, "tokens": 10, "cost_limit": "1", "claims": 1, "sources": 1}}
    session = SimpleNamespace(policy=SimpleNamespace(to_dict=lambda: frozen),
        execution=SimpleNamespace(profile=SimpleNamespace(total_steps=10), ledger=lambda *_args: {}))
    view = {"policy_compatible": True, "authority": {"terminal_grants_available": True}, "usage": {"claims": 0, "sources": 0},
        "execution": {"steps_used": 1, "pending": {"attempt_id": "job", "applied": False,
            "state": "succeeded", "reservation_occupied": False}}}
    monkeypatch.setattr(driver, "deadline", lambda *_args: datetime.now(timezone.utc) + timedelta(seconds=100))
    monkeypatch.setattr(driver.accounting, "projection", lambda *_args, **_kwargs: {
        "usage_unknown": False, "overrun": ["cost"]})
    monkeypatch.setattr(driver, "_terminal_action", lambda _session, _view, outcome, reason: {
        "operation": "terminal", "outcome": outcome, "reason": reason})
    action = driver._action(session, view, [{"kind": "retained"}])
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
        {"kind": "execution_observation", "data": {"attempt_id": "job", "observation": {"state": "succeeded"}}},
        {"kind": "execution_application", "data": {"attempt_id": "job", "operation": "generate",
            "original_id": "original", "proposal": {}, "coverage_links": []}}]
    attempt = SimpleNamespace(job={"basis_id": "old", "deadline_utc":
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
    execution = SimpleNamespace(attempt=lambda _document: attempt, authenticate=lambda value, _attempt: value)
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
