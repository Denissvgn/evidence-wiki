"""Closed prospective execution lifetimes, separate from observer recency."""

from datetime import datetime, timedelta, timezone


def execution_policy(profile):
    if type(profile) is not str or profile not in {"two-hour", "four-hour"}:
        raise ValueError("execution_policy_invalid")
    seconds = 7200 if profile == "two-hour" else 14400
    return {"schema_version": "review-execution-budget-policy/v1", "profile": profile,
        "authority_seconds": seconds, "credential_lifetime_seconds": seconds,
        "deadline_basis": "frozen_plan_expiry", "renewal": "forbidden"}


def validate_execution_policy(policy):
    if (type(policy) is not dict or type(policy.get("authority_seconds")) is not int
            or type(policy.get("credential_lifetime_seconds")) is not int
            or policy != execution_policy(policy.get("profile"))):
        raise ValueError("execution_policy_invalid")
    return policy


def validate_execution_binding(policy, binding):
    from tools._observation_freshness import observation_policy, validate_observation_policy
    from tools._review_dependency_workload import digest

    policy = validate_execution_policy(policy)
    if (type(binding) is not dict or binding.get("execution_policy") != policy
            or binding.get("execution_policy_sha256") != digest(policy)):
        raise ValueError("execution_policy_binding_invalid")
    observer = validate_observation_policy(binding.get("observation_policy"))
    if (observer != observation_policy("batch")
            or binding.get("observation_policy_sha256") != digest(observer)):
        raise ValueError("execution_observation_profile_invalid")
    return policy


def validate_execution_window(policy, frozen_at, expires_at, *, instant=None):
    policy = validate_execution_policy(policy)
    try:
        frozen, expires = (datetime.fromisoformat(value) for value in (frozen_at, expires_at))
        if (frozen.tzinfo is None or expires.tzinfo is None
                or expires - frozen != timedelta(seconds=policy["authority_seconds"])
                or instant is not None and not frozen <= instant < expires):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("execution_authority_clock_invalid") from None
    return frozen, expires


def credential_expiries(expires_at):
    return dict.fromkeys(("trust", "usage", "signing", "service", "reference_qualification"), expires_at)


def validate_runtime_allocation(allocation, plan, *, closing=None):
    from tools._review_dependency_workload import digest

    policy = validate_execution_binding(plan.get("execution_policy"), plan)
    frozen, expires = validate_execution_window(policy, plan["frozen_at"], plan["expires_at"])
    if type(allocation) is not dict or allocation.get("schema_version") != "review-workload-runtime-allocation/v2":
        raise ValueError("execution_runtime_binding_invalid")
    binding = allocation.get("allocation_binding")
    validate_execution_binding(policy, binding)
    expected = {key: plan[key] for key in ("candidate_sha256", "phase_caps", "hard_limits", "source_protocol_limits",
        "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256")}
    expected.update(plan_sha256=digest(plan), authority_frozen_at=plan["frozen_at"], authority_expires_at=plan["expires_at"])
    if "assignments" in plan:
        expected.update((key, plan[key]) for key in ("assignments", "group_overhead_cap_seconds", "control_run_id"))
    else:
        expected.update(study="closure_feasibility_diagnostic", run_ids=plan["run_ids"], review_jobs=0)
    if (binding != expected or allocation.get("execution_policy") != policy
            or allocation.get("execution_policy_sha256") != digest(policy)
            or allocation.get("credential_expiries") != credential_expiries(plan["expires_at"])
            or allocation.get("authority_end") != plan["expires_at"]
            or allocation.get("run_policy", {}).get("budgets", {}).get("seconds") != plan["elapsed_cap_seconds"]):
        raise ValueError("execution_runtime_binding_invalid")
    try:
        begin = datetime.fromisoformat(allocation["authority_start"])
        issued = datetime.fromisoformat(allocation["issued_before_worker_outputs"])
        if (begin.tzinfo is None or issued.tzinfo is None or not frozen <= begin <= issued < expires
                or not 0 < (expires - begin).total_seconds() <= policy["credential_lifetime_seconds"]
                or closing is not None and not begin <= closing < expires):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("execution_authority_clock_invalid") from None
    return allocation


def provisioning_window(policy, binding, run_seconds):
    from tools import run_review_dependency_workload as workload

    policy = validate_execution_binding(policy, binding)
    if (binding.get("hard_limits") != workload.runtime_hard_limits(binding["observation_policy"], policy)
            or binding.get("source_protocol_limits") != workload.HARD_LIMITS):
        raise ValueError("execution_runtime_binding_invalid")
    if type(run_seconds) is not int or not 0 < run_seconds <= policy["authority_seconds"]:
        raise ValueError("execution_allocation_invalid")
    instant = datetime.now(timezone.utc)
    _, expires = validate_execution_window(policy, binding.get("authority_frozen_at"),
        binding.get("authority_expires_at"), instant=instant)
    return instant.isoformat(), binding["authority_expires_at"]
