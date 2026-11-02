"""Small host clock corrections keep execution stamps ordered; real regressions still refuse."""

from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import _ordered_clock, agent_execution
from evidence_wiki.agent_execution import ExecutionError
from tests.test_agent_execution import attempt, executor

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
