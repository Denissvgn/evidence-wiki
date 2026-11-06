"""Small host clock corrections keep execution stamps ordered; real regressions still refuse."""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import _ordered_clock, agent_execution
from evidence_wiki import execution_accounting as accounting
from evidence_wiki._review_dependency_json import digest
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.agent_qualification import QualificationAuthority, QualifiedEvaluators
from evidence_wiki.research_execution import SCOPE, ResearchExecution, now_utc
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_agent_execution import attempt, executor
from tests.test_agent_qualification import signer as review_signer
from tests.test_execution_isolation_adversarial import profile, signer
from tests.test_strict_evidence import CONTRACT, CORE, USAGE
from tests.test_strict_evidence import host as strict_host_fixture

START = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
# Frozen here, not read from the module: widening or narrowing the allowance must fail the boundary tests below.
SHIPPED_ALLOWANCE = timedelta(seconds=1)


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(_ordered_clock, "_last", None)
    return _ordered_clock


def test_regression_within_allowance_waits_and_returns_the_previous_reading(clock):
    assert clock.ordered(START) == START
    for step in (timedelta(milliseconds=5), SHIPPED_ALLOWANCE):
        began = time.monotonic()
        assert clock.ordered(START - step) == START
        # Waiting out the step brings the raw clock back to the result, so no raw check later sees it ahead.
        assert time.monotonic() - began >= step.total_seconds()
    later = START + timedelta(milliseconds=1)
    assert clock.ordered(later) == later


def test_regression_beyond_allowance_passes_through_and_restarts_from_it(clock):
    clock.ordered(START)
    regressed = START - SHIPPED_ALLOWANCE - timedelta(microseconds=1)
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
    # The stamp is the previous reading, and the clock waited until the raw clock was back at it.
    assert datetime.fromisoformat(baseline["observed_at"]) == last <= datetime.now(timezone.utc)
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


@pytest.fixture
def strict_host(tmp_path, monkeypatch, request):
    return strict_host_fixture.__wrapped__(tmp_path, monkeypatch, request)


def signed_review(prepared, at):
    return review_signer().registration(prepared, dict.fromkeys(CONTRACT.CHECKS, "pass"),
        "The exact retained product page supports its attributed statement.", "autonomous-review", at)


def script_refused(host, envelope):
    """The workspace scripts authenticate against their own raw clock, which ordered() cannot reach."""
    with pytest.raises(Exception) as caught:
        USAGE.transact(host.root, host.config, envelope)
    assert type(caught.value).__name__ == "EvidenceInvalid"
    assert str(caught.value) == "authentication_outside_validity"


def test_review_signed_right_after_a_step_back_registers_against_the_script_clock(strict_host, monkeypatch, clock):
    prepared = CORE.prepare_review(strict_host.root, "product-source")
    # Step back by the whole allowance immediately before signing, so the signer reads the clock inside the step.
    last = held(monkeypatch, SHIPPED_ALLOWANCE)
    stamp = now_utc()
    assert stamp == last.isoformat()
    # Registration checks the stamp against its own raw clock within milliseconds of signing.
    assert USAGE.transact(strict_host.root, strict_host.config, signed_review(prepared, stamp))["checkpoint"]


def test_review_stamped_ahead_of_the_script_clock_is_still_refused(strict_host, monkeypatch, clock):
    prepared = CORE.prepare_review(strict_host.root, "product-source")
    # A stamp this clock never produced, with no step back.
    script_refused(strict_host, signed_review(prepared, (datetime.now(timezone.utc) + timedelta(milliseconds=500)).isoformat()))
    # A stamp from before a step back beyond the allowance. The regressed reading passes through without a wait.
    last = held(monkeypatch, SHIPPED_ALLOWANCE + timedelta(milliseconds=250))
    assert datetime.fromisoformat(now_utc()) < last - SHIPPED_ALLOWANCE
    script_refused(strict_host, signed_review(prepared, last.isoformat()))


def host_executor(tmp_path, monkeypatch):
    """The reference executor stamping with its own raw clock, as a host-implemented AgentExecutor does."""
    monkeypatch.setattr(agent_execution, "_now", lambda: datetime.now(timezone.utc).isoformat())
    return executor(tmp_path, "print('{}')")


def issued_at(host, issued):
    return attempt(host.profile, job_changes={"issued_utc": issued.isoformat(),
        "deadline_utc": (issued + timedelta(seconds=10)).isoformat()})


@pytest.mark.parametrize("step", [timedelta(milliseconds=500), SHIPPED_ALLOWANCE], ids=["500ms", "allowance"])
def test_job_issued_right_after_a_step_back_runs_on_a_host_executor_clock(tmp_path, monkeypatch, clock, step):
    host = host_executor(tmp_path, monkeypatch)
    last = held(monkeypatch, step)
    # The session issues every job through the ordered clock.
    issued = clock.ordered(datetime.now(timezone.utc))
    assert issued == last
    selected = issued_at(host, issued)
    # The executor checks the job and stamps its observations with its own raw clock.
    observed = host.invoke(selected)
    assert host.authenticate(observed, selected) == observed
    assert host._attempts[selected.attempt_id].finished.wait(5)
    final = host.poll(selected)
    assert final["state"] != "running" and host.authenticate(final, selected) == final


def test_job_issued_ahead_of_a_host_executor_clock_is_still_refused(tmp_path, monkeypatch, clock):
    host = host_executor(tmp_path, monkeypatch)
    with pytest.raises(WorkflowContractError) as caught:
        host.invoke(issued_at(host, datetime.now(timezone.utc) + timedelta(milliseconds=500)))
    assert caught.value.error_code == "WORKFLOW_INVALID"
    last = held(monkeypatch, SHIPPED_ALLOWANCE + timedelta(milliseconds=250))
    assert clock.ordered(datetime.now(timezone.utc)) < last - SHIPPED_ALLOWANCE
    with pytest.raises(WorkflowContractError) as caught:
        host.invoke(issued_at(host, last))
    assert caught.value.error_code == "WORKFLOW_INVALID"
