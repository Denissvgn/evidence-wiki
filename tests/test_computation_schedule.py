"""Fixed-clock cadence cases, including real timezone transition boundaries."""

import copy
from pathlib import Path

import pytest

from tests._computation_fixture import definition
from tests._script_loader import load_isolated_module

SCHEDULE = load_isolated_module("computation_schedule", Path(__file__).resolve().parents[1] / "workspace-template/scripts/_computation_schedule.py")
EXPR = load_isolated_module("computation_schedule_expression", Path(__file__).resolve().parents[1] / "workspace-template/scripts/_computation_expression.py")


def evaluate(trigger, *, clock=None, as_of=None, completed=frozenset()):
    data = definition()
    data["clock"].update(clock or {})
    data["cadence"]["check"] = {"description": "Scheduled status", "trigger": trigger,
        "lead_alerts": [{"amount": 2, "unit": "days"}], "action": {"kind": "status_flag", "target": "ready"}}
    evaluator = EXPR.Evaluator(data["arithmetic"])
    return SCHEDULE.evaluate_schedules(data, as_of, evaluator, lambda _: {}, "retained-input", completed=completed)


def test_fixed_date_and_lead_alerts_are_read_only_descriptors():
    result = evaluate({"type": "fixed_date", "at": "2026-09-22T12:00:00Z"})
    row = result["schedules"][0]
    assert row["state"] == "pending" and row["next_at"] == "2026-09-22T12:00:00+00:00"
    assert row["lead_alerts"] == [{"amount": 2, "unit": "days"}]
    assert row["occurrence_id"] is None


def test_repeated_polling_retains_the_occurrence_identity():
    trigger = {"type": "fixed_date", "at": "2026-09-20T12:00:00Z"}
    first = evaluate(trigger)["schedules"][0]
    later = evaluate(trigger, as_of="2026-09-22T12:00:00Z")["schedules"][0]
    assert first["state"] == "overdue" and later["occurrence_id"] == first["occurrence_id"]
    assert evaluate(trigger, completed={first["occurrence_id"]})["schedules"][0]["state"] == "completed"


@pytest.mark.parametrize("policy,expected", [("earlier", "2026-10-25T00:30:00+00:00"), ("later", "2026-10-25T01:30:00+00:00")])
def test_ambiguous_wall_clock_uses_the_declared_fold(policy, expected):
    result = evaluate({"type": "fixed_date", "at": "2026-10-25T02:30:00"},
        clock={"timezone": "Europe/Madrid", "ambiguous": policy}, as_of="2026-10-24T00:00:00Z")
    assert result["schedules"][0]["next_at"] == expected
    assert result["timezone"]["provider"].startswith("tzdata:")
    assert len(result["timezone"]["sha256"]) == 64


def test_ambiguous_or_nonexistent_wall_clock_is_not_guessed():
    for at in ("2026-10-25T02:30:00", "2026-03-29T02:30:00"):
        with pytest.raises(ValueError):
            evaluate({"type": "fixed_date", "at": at}, clock={"timezone": "Europe/Madrid"})
    result = evaluate({"type": "fixed_date", "at": "2026-03-29T02:30:00"},
                      clock={"timezone": "Europe/Madrid", "nonexistent": "skip"})
    assert result["schedules"][0]["state"] == "unknown"


def test_cron_wildcard_does_not_override_a_weekday_constraint():
    result = evaluate({"type": "cron", "expression": "0 9 * * 1", "day_match": "or"})
    row = result["schedules"][0]
    assert row["due_at"] == "2026-09-21T09:00:00+00:00"
    assert row["next_at"] == "2026-09-28T09:00:00+00:00"


def test_calendar_month_clamp_is_distinct_from_elapsed_days():
    trigger = {"type": "elapsed_units", "anchor": "2026-01-31T12:00:00Z", "amount": 1,
               "unit": "months", "calendar_policy": "clamp"}
    row = evaluate(trigger, as_of="2026-02-28T12:00:00Z")["schedules"][0]
    assert row["state"] == "due" and row["next_at"] == "2026-03-31T12:00:00+00:00"
    strict = copy.deepcopy(trigger)
    strict["calendar_policy"] = "refuse"
    with pytest.raises(ValueError):
        evaluate(strict, as_of="2026-02-28T12:00:00Z")


def test_condition_occurrence_is_bound_to_inputs_instead_of_poll_time():
    trigger = {"type": "condition", "expression": "2 > 1", "inputs": {}}
    first = evaluate(trigger)["schedules"][0]
    later = evaluate(trigger, as_of="2026-09-22T12:00:00Z")["schedules"][0]
    assert first["state"] == later["state"] == "due"
    assert first["occurrence_id"] == later["occurrence_id"]


@pytest.mark.parametrize("cron", ["@hourly", "* * * * * /bin/sh", "60 * * * *", "*/0 * * * *", "* * 0 * *", "* * * * 7"])
def test_unknown_or_unbounded_cron_syntax_refuses(cron):
    with pytest.raises(ValueError):
        SCHEDULE.cron_fields(cron)


def test_a_schedule_never_supplies_an_implicit_current_clock():
    with pytest.raises(ValueError, match="computation_clock_required"):
        evaluate({"type": "fixed_date", "at": "2026-09-22"}, clock={"as_of": None})
