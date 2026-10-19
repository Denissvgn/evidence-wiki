"""Selective pilot documents remain distinct from historical admission."""

import pytest

from tools import _review_dependency_pilot as pilot
from tools import run_review_dependency_workload as legacy


def test_old_reader_rejects_selective_measurement():
    with pytest.raises(ValueError, match="workload_pilot_measurement_invalid"):
        legacy.completed_pilot_measurements({"schema_version": pilot.RECIPE}, "candidate")


def test_measurement_admission_rejects_other_candidate():
    with pytest.raises(ValueError, match="selective_pilot_measurement_invalid"):
        pilot.completed_pilot_measurements({"schema_version": pilot.RECIPE, "candidate_sha256": "old"}, "new")


def test_measurement_admission_rechecks_exact_original_recipe(monkeypatch):
    expected = {"schema_version": pilot.RECIPE, "candidate_sha256": "candidate",
                "pilot_plan_pin": {}, "pilot_report_pin": {}, "observed_worst_seconds": {"job": 300},
                "observed_group_overhead_seconds": 12, "phase_caps": {"job": 375},
                "group_overhead_cap_seconds": 15}
    monkeypatch.setattr(pilot, "measurement_recipe", lambda *_, **kwargs: expected)
    assert pilot.completed_pilot_measurements(expected, "candidate") == ({"job": 300}, 12)
    with pytest.raises(ValueError, match="selective_pilot_measurement_invalid"):
        pilot.completed_pilot_measurements({**expected, "phase_caps": {"job": 1}}, "candidate")


def test_pilot_current_binding_rejects_modified_envelope():
    with pytest.raises(ValueError, match="selective_pilot_plan_binding_invalid"):
        pilot.validate_plan({"schema_version": pilot.PLAN}, "wrong", current=False)


@pytest.fixture
def recipe_originals(monkeypatch):
    native = {"observation_policy": {"profile": "batch"}, "execution_policy": {"profile": "six-hour"},
              "frozen_at": "2026-10-16T00:00:00+00:00", "expires_at": "2026-10-16T06:00:00+00:00"}
    plan = {"candidate_sha256": "candidate", "native_plan": native}
    worst = {"setup": 10, "mutation": 10, "job": 100, "owner_read": 100, "cleanup": 10, "global_close": 100}
    ceilings = {"setup": 120, "mutation": 120, "job": 600, "owner_read": 1200, "cleanup": 120, "global_close": 1500}
    monkeypatch.setattr(legacy, "read_pinned_artifact", lambda pin: (plan, b"original"))
    monkeypatch.setattr(pilot, "_originals", lambda *args: (plan, {}, worst, 12))
    monkeypatch.setattr(legacy, "phase_ceilings", lambda *args: ceilings)
    return worst, ceilings


def test_recipe_accepts_explicit_bounded_closing_reservation(recipe_originals):
    worst, ceilings = recipe_originals
    recipe = pilot.measurement_recipe({}, {}, phase_caps=ceilings, group_overhead_cap_seconds=30)
    assert recipe["phase_caps"]["global_close"] == 1500
    assert pilot.completed_pilot_measurements(recipe, "candidate") == (worst, 12)


@pytest.mark.parametrize("field,value", [("global_close", 1501), ("job", 124), ("job", True)])
def test_recipe_refuses_caps_outside_observed_policy_bounds(recipe_originals, field, value):
    _, ceilings = recipe_originals
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.measurement_recipe({}, {}, phase_caps={**ceilings, field: value})


@pytest.mark.parametrize("overhead", [14, 21601, True])
def test_recipe_refuses_unbounded_group_overhead(recipe_originals, overhead):
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.measurement_recipe({}, {}, group_overhead_cap_seconds=overhead)


def test_recipe_refuses_changed_observed_cost_and_unknown_fields(recipe_originals):
    recipe = pilot.measurement_recipe({}, {})
    for changed in ({**recipe, "observed_group_overhead_seconds": 11}, {**recipe, "extra": True}):
        with pytest.raises(ValueError, match="selective_pilot_measurement_invalid"):
            pilot.completed_pilot_measurements(changed, "candidate")


def test_observational_measurement_records_minima_without_formal_admission(recipe_originals, monkeypatch):
    worst, ceilings = recipe_originals
    worst["job"] = 854
    from tools._execution_budget import execution_policy
    from tools._observation_freshness import observation_policy
    native = {"service_policy": legacy.service_policy("observational"),
        "observation_policy": observation_policy("batch"), "execution_policy": execution_policy("six-hour"), "frozen_at": "2026-10-16T00:00:00+00:00", "expires_at": "2026-10-16T06:00:00+00:00"}
    observed = {"schema_version": pilot.OBSERVATIONAL_PLAN, "candidate_sha256": "candidate", "native_plan": native}
    monkeypatch.setattr(legacy, "read_pinned_artifact", lambda pin: (observed, b"original"))
    monkeypatch.setattr(pilot, "_originals", lambda *args: (observed, {}, worst, 12))
    measurement = pilot.observational_measurements({}, {})
    assert measurement["minimum_phase_caps"]["job"] == 1068
    assert measurement["formal_phase_ceilings"]["job"] == 600
    assert measurement["formal_recipe_status"] == "refused_phase_ceilings"
    assert measurement["exceeded_formal_phases"] == ["job"]
    assert measurement["pair_qualified"] is False
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.measurement_recipe({}, {})
    with pytest.raises(ValueError, match="selective_pilot_measurement_invalid"):
        pilot.completed_pilot_measurements(measurement, "candidate")
