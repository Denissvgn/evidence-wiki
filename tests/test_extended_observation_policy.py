"""Extended observer windows preserve exact policy and execution bindings."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_selective_review_workload import cohort as cohort
from tests.test_selective_review_workload import pin as pin
from tools import _review_dependency_accounting as accounting
from tools._execution_budget import (
    credential_expiries,
    execution_policy,
    validate_execution_binding,
    validate_runtime_allocation,
)
from tools._observation_freshness import (
    is_batch_policy,
    observation_policy,
    observer_deadline,
    validate_observation_policy,
    validate_observer_window,
)
from tools._review_dependency_workload import digest


def test_extended_window_expires_at_original_two_hour_boundary():
    policy = observation_policy("batch-extended")
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    witnesses = [{"observed_utc": start.isoformat()},
                 {"observed_utc": (start + timedelta(seconds=60)).isoformat()}]
    observed = {"observation_policy": policy, "observation_policy_sha256": digest(policy),
        "native_host_witnesses": witnesses, "witness_semantics": policy["witness_semantics"],
        "observer_recency_not_after": observer_deadline(witnesses, policy)}
    deadline = start + timedelta(seconds=7200)
    assert observed["observer_recency_not_after"] == deadline.isoformat()
    assert validate_observer_window(observed, deadline - timedelta(microseconds=1), policy)
    with pytest.raises(ValueError, match="workload_original_witness_expired"):
        validate_observer_window(observed, deadline, policy)
    assert policy["closing_validation"] == "fresh_native_owner_read"
    assert policy["original_window_renewal"] == "forbidden"


@pytest.mark.parametrize("change", [
    {"schema_version": "observer-recency-policy/v1"}, {"profile": "batch"},
    {"max_age_seconds": 7200.0}, {"max_age_seconds": True},
    {"max_age_seconds": 7201}, {"extra": None}, {"profile": []},
    {"closing_validation": "original_native_owner_reads"},
])
def test_extended_policy_and_batch_helper_refuse_mixed_shapes(change):
    policy = dict(observation_policy("batch-extended"), **change)
    for validate in (validate_observation_policy, is_batch_policy):
        with pytest.raises(ValueError, match="observation_policy_invalid"):
            validate(policy)
    with pytest.raises(ValueError):
        accounting.validate("ObservationPolicy", policy)


def test_old_profiles_remain_exact_and_dispatch_accepts_both_batch_profiles():
    for profile, seconds in (("interactive", 30), ("batch", 3600)):
        policy = observation_policy(profile)
        assert policy == {"schema_version": "observer-recency-policy/v1", "profile": profile,
            "max_age_seconds": seconds,
            "witness_semantics": "observer_recency_bound_not_native_calibration_witness",
            "closing_validation": "fresh_native_owner_read" if profile == "batch" else "original_native_owner_reads",
            "original_window_renewal": "forbidden"}
        assert is_batch_policy(policy) is (profile == "batch")
    for profile in ("batch", "batch-extended"):
        accounting.validate("ObservationPolicy", observation_policy(profile))
    with pytest.raises(ValueError):
        accounting.validate("ObservationPolicy", observation_policy("interactive"))
    with pytest.raises(ValueError):
        validate_observation_policy(dict(observation_policy("batch"), schema_version="observer-recency-policy/v2"))


def _runtime_documents(profile):
    execution = execution_policy("two-hour")
    observer = observation_policy(profile)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = start + timedelta(seconds=execution["authority_seconds"])
    plan = {"candidate_sha256": "candidate", "phase_caps": {}, "hard_limits": {},
        "source_protocol_limits": {}, "run_ids": ["run"], "elapsed_cap_seconds": 100,
        "execution_policy": execution, "execution_policy_sha256": digest(execution),
        "observation_policy": observer, "observation_policy_sha256": digest(observer),
        "frozen_at": start.isoformat(), "expires_at": end.isoformat()}
    binding = {key: plan[key] for key in ("candidate_sha256", "phase_caps", "hard_limits",
        "source_protocol_limits", "execution_policy", "execution_policy_sha256",
        "observation_policy", "observation_policy_sha256")}
    binding.update(plan_sha256=digest(plan), authority_frozen_at=plan["frozen_at"],
        authority_expires_at=plan["expires_at"], study="closure_feasibility_diagnostic",
        run_ids=plan["run_ids"], review_jobs=0)
    allocation = {"schema_version": "review-workload-runtime-allocation/v2", "allocation_binding": binding,
        "execution_policy": execution, "execution_policy_sha256": digest(execution),
        "credential_expiries": credential_expiries(plan["expires_at"]),
        "authority_start": start.isoformat(), "issued_before_worker_outputs": start.isoformat(),
        "authority_end": end.isoformat(), "run_policy": {"budgets": {"seconds": 100}}}
    return plan, allocation


@pytest.mark.parametrize("profile", ["batch", "batch-extended"])
def test_runtime_allocation_requires_exact_selected_observer_profile(profile):
    plan, allocation = _runtime_documents(profile)
    assert validate_execution_binding(plan["execution_policy"], plan) == plan["execution_policy"]
    assert validate_runtime_allocation(allocation, plan) == allocation
    altered = copy.deepcopy(allocation)
    observer = observation_policy("batch" if profile == "batch-extended" else "batch-extended")
    altered["allocation_binding"].update(observation_policy=observer, observation_policy_sha256=digest(observer))
    with pytest.raises(ValueError, match="execution_runtime_binding_invalid"):
        validate_runtime_allocation(altered, plan)
    altered = copy.deepcopy(allocation)
    altered["allocation_binding"]["observation_policy_sha256"] = "forged"
    with pytest.raises(ValueError, match="execution_observation_profile_invalid"):
        validate_runtime_allocation(altered, plan)


def test_schema_exports_freeze_legacy_policy_and_extend_current_owner_contract():
    exported = accounting.schema_documents()
    legacy = observation_policy("batch")
    extended = observation_policy("batch-extended")
    for name in accounting._PRE_OWNER_SCHEMA_DEFS:
        schema = exported[name]["$defs"]["ObservationPolicy"]
        accounting._shape(legacy, schema)
        with pytest.raises(ValueError):
            accounting._shape(extended, schema)
    for name in ("OwnerPlan", "OwnerReport", "OwnerControlPhaseCeilingPolicy"):
        schema = exported[name]["$defs"]["ObservationPolicy"]
        accounting._shape(legacy, schema)
        accounting._shape(extended, schema)
        with pytest.raises(ValueError):
            accounting._shape(dict(extended, max_age_seconds=7200.0), schema)


@pytest.mark.parametrize("name", ["Plan", "PairPlan", "ExtendedPlan", "CollectionPlan", "LongPlan"])
def test_historical_formal_shape_and_decode_refuse_extended_policy(cohort, name):
    plan, _ = cohort
    assert accounting.validate("Plan", plan) == plan
    extended = observation_policy("batch-extended")
    changed = dict(plan, observation_policy=extended, observation_policy_sha256=digest(extended))
    # Direct names and generic dispatch must enforce the exported old policy.
    versions = {"Plan": accounting.PLAN, "PairPlan": accounting.PAIR_PLAN,
        "ExtendedPlan": accounting.EXTENDED_PLAN, "CollectionPlan": accounting.COLLECTION_PLAN,
        "LongPlan": accounting.LONG_PLAN}
    changed["schema_version"] = versions[name]
    for selected in {name, "Plan"}:
        with pytest.raises(ValueError, match="accounting_constant_invalid"):
            accounting.validate(selected, changed)
        with pytest.raises(ValueError, match="accounting_constant_invalid"):
            accounting.decode(selected, accounting.canonical(changed))


def test_current_owner_formal_shape_and_decode_accept_extended_policy(cohort):
    from tests.test_prospective_control_ceiling import owner_long_formal

    plan, pair = owner_long_formal(cohort)
    extended = observation_policy("batch-extended")
    plan.update(observation_policy=extended, observation_policy_sha256=digest(extended))
    for selected in ("Plan", "OwnerPlan"):
        assert accounting.validate(selected, plan) == plan
        assert accounting.decode(selected, accounting.canonical(plan)) == plan
    assert accounting.validate_plan(plan, pair) == plan
