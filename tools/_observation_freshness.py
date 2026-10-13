"""Closed observer recency policies; native calibration authority is separate."""

from datetime import datetime, timedelta

from tools._review_dependency_workload import digest


def observation_policy(profile):
    if type(profile) is not str or profile not in {"interactive", "batch"}:
        raise ValueError("observation_policy_invalid")
    return {"schema_version": "observer-recency-policy/v1", "profile": profile,
        "max_age_seconds": 3600 if profile == "batch" else 30,
        "witness_semantics": "observer_recency_bound_not_native_calibration_witness",
        "closing_validation": "fresh_native_owner_read" if profile == "batch" else "original_native_owner_reads",
        "original_window_renewal": "forbidden"}


def validate_observation_policy(policy):
    if (type(policy) is not dict or type(policy.get("max_age_seconds")) is not int
            or policy != observation_policy(policy.get("profile"))):
        raise ValueError("observation_policy_invalid")
    return policy


def observer_deadline(witnesses, policy):
    policy = validate_observation_policy(policy)
    clocks = [datetime.fromisoformat(witness["observed_utc"]) for witness in witnesses]
    if not clocks or any(clock.tzinfo is None for clock in clocks):
        raise ValueError("observer_clock_invalid")
    return (min(clocks) + timedelta(seconds=policy["max_age_seconds"])).isoformat()


def validate_observer_window(observed, closing, policy):
    policy = validate_observation_policy(policy)
    closing = datetime.fromisoformat(closing) if isinstance(closing, str) else closing
    if (closing.tzinfo is None or observed.get("observation_policy") != policy
            or observed.get("observation_policy_sha256") != digest(policy)
            or observed.get("witness_semantics") != policy["witness_semantics"]):
        raise ValueError("observer_policy_binding_invalid")
    witnesses = observed["native_host_witnesses"]
    deadline = observer_deadline(witnesses, policy)
    if (observed.get("observer_recency_not_after") != deadline
            or any(datetime.fromisoformat(witness["observed_utc"]) > closing for witness in witnesses)):
        raise ValueError("observer_clock_invalid")
    if closing >= datetime.fromisoformat(deadline):
        raise ValueError("workload_original_witness_expired")
    return deadline
