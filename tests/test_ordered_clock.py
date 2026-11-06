"""Small host clock corrections keep execution stamps ordered; real regressions still refuse."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import _ordered_clock, agent_execution
from evidence_wiki import execution_accounting as accounting
from evidence_wiki._review_dependency_json import digest
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.agent_qualification import QualificationAuthority, QualifiedEvaluators
from evidence_wiki.research_execution import SCOPE, ResearchExecution
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_agent_execution import attempt, executor
from tests.test_execution_isolation_adversarial import profile, signer

START = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(_ordered_clock, "_last", None)
    return _ordered_clock


def test_regression_within_allowance_holds_the_previous_reading(clock):
    assert clock.ordered(START) == START
    assert clock.ordered(START - timedelta(milliseconds=5)) == START
    assert clock.ordered(START - clock.ALLOWANCE) == START
    later = START + timedelta(milliseconds=1)
    assert clock.ordered(later) == later


def test_regression_beyond_allowance_passes_through_and_restarts_from_it(clock):
    clock.ordered(START)
    regressed = START - clock.ALLOWANCE - timedelta(microseconds=1)
    assert clock.ordered(regressed) == regressed
    assert clock.ordered(regressed - timedelta(milliseconds=5)) == regressed


def stepped_executor(tmp_path, monkeypatch, step):
    """The first wall reading is the start stamp; every later reading has stepped back by ``step``."""
    base = datetime.now(timezone.utc)
    readings = iter([base])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(readings, base - step).astimezone(tz)

    monkeypatch.setattr(agent_execution, "datetime", Clock)
    host = executor(tmp_path, "import time; time.sleep(0.2); print('{}')")
    issued = (base - timedelta(minutes=1)).isoformat()
    return host, attempt(host.profile, job_changes={"issued_utc": issued})


def test_millisecond_step_back_after_start_keeps_the_observation_valid(tmp_path, monkeypatch, clock):
    host, selected = stepped_executor(tmp_path, monkeypatch, timedelta(milliseconds=5))
    observed = host.invoke(selected)
    assert host._attempts[selected.attempt_id].finished.wait(2)
    assert observed["started_utc"] == observed["observed_utc"]
    assert host.authenticate(observed, selected) == observed


def test_step_back_beyond_allowance_still_refuses_as_clock_regression(tmp_path, monkeypatch, clock):
    host, selected = stepped_executor(tmp_path, monkeypatch, clock.ALLOWANCE + timedelta(seconds=1))
    with pytest.raises(ExecutionError, match="execution_clock_regressed"):
        host.invoke(selected)
    assert host._attempts[selected.attempt_id].finished.wait(2)



def research_execution():
    issuer = QualificationAuthority("issuer", b"q" * 32, "generator")
    return ResearchExecution(SimpleNamespace(profile=profile()), qualifications=QualifiedEvaluators([], issuer),
        review_signer=signer(), observer_key=b"o" * 32)


SESSION = SimpleNamespace(binding={"run_id": "ordered-clock-run"})
# Frozen here, not read from the module: widening the allowance must fail the boundary tests below.
SHIPPED_ALLOWANCE = timedelta(seconds=1)


def held(monkeypatch, ahead):
    """The last reading sits ``ahead`` of the raw clock: the host stepped back that far right after it."""
    last = datetime.now(timezone.utc) + ahead
    monkeypatch.setattr(_ordered_clock, "_last", last)
    return last


def stamped(execution, at):
    """A baseline this process signed at ``at``, as its own clock reported it."""
    baseline = execution.baseline(SESSION)
    baseline["observed_at"] = at.isoformat()
    return execution.sign(baseline)


def begin(execution, baseline):
    """Open coverage both directly and through journal replay, as a session does on every step."""
    plan = accounting.start_coverage(binding={"session_binding_id": digest(SESSION.binding)}, scope_id=SCOPE,
        baseline=baseline, verifier=execution.verifier())
    assert execution.ledger(SESSION, records=[{"kind": "execution_begin", "data": {"baseline": baseline}}]) == plan
    return plan


def refused(execution, baseline):
    with pytest.raises(WorkflowContractError) as caught:
        accounting.start_coverage(binding={"session_binding_id": digest(SESSION.binding)}, scope_id=SCOPE,
            baseline=baseline, verifier=execution.verifier())
    assert caught.value.error_code == "ACCOUNTING_OBSERVATION_UNAUTHENTICATED"
    with pytest.raises(WorkflowContractError, match="Execution accounting"):
        execution.ledger(SESSION, records=[{"kind": "execution_begin", "data": {"baseline": baseline}}])


@pytest.mark.parametrize("ahead", [timedelta(milliseconds=500), SHIPPED_ALLOWANCE], ids=["500ms", "allowance"])
def test_held_execution_stamp_authenticates_against_the_clock_that_produced_it(monkeypatch, clock, ahead):
    execution = research_execution()
    last = held(monkeypatch, ahead)
    baseline = execution.baseline(SESSION)
    # The premise the refusal needs: the stamp is the held reading, ahead of the raw clock.
    assert datetime.fromisoformat(baseline["observed_at"]) == last > datetime.now(timezone.utc)
    assert begin(execution, baseline)["opened_at"] == baseline["observed_at"]


def test_stamp_from_before_a_step_back_beyond_the_allowance_still_refuses(monkeypatch, clock):
    execution = research_execution()
    # This clock last read ``last``, then the host stepped back 250 ms past the allowance.
    last = held(monkeypatch, SHIPPED_ALLOWANCE + timedelta(milliseconds=250))
    refused(execution, stamped(execution, last))
    # The regressed reading passed through, so the refusal came from the real fault, not from a missing hold.
    assert clock._last < last - SHIPPED_ALLOWANCE


def test_future_stamp_the_clock_never_produced_still_refuses(clock):
    execution = research_execution()
    ahead = datetime.now(timezone.utc) + timedelta(milliseconds=500)
    refused(execution, stamped(execution, ahead))
    assert clock._last < ahead
