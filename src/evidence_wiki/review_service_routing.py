"""Native route decisions preserve actual service identity and uncertain reservations."""

from datetime import datetime, timedelta

from .agent_execution import ExecutionError
from .mutation_contracts import digest
from .review_service_policy import approved_profiles, service_action


def required_profile(session, config_ref, slot_ref=None):
    rows = approved_profiles(session.policy.to_dict(), session.execution.profile)
    selected = [row for row in rows if row["profile_ref"] == config_ref]
    if len(selected) != 1:
        raise ExecutionError("service_configuration_unapproved")
    plan = session.execution.profile.review_services
    if plan is not None:
        actual_slot = next((row.slot_ref for row in plan.routes if config_ref in row.config_refs), None)
        if slot_ref is not None and slot_ref != actual_slot:
            raise ExecutionError("service_slot_changed")
    elif slot_ref is not None and slot_ref != config_ref:
        raise ExecutionError("service_slot_changed")
    return selected[0]


def selected(session, *, slot_ref, claim_id, strict_basis_id, view, records):
    from . import execution_session as native
    from .service_authentication import correlated, status

    plan = session.execution.profile.review_services
    if plan is None:
        return {"action": "select", "config_ref": slot_ref, "reason": "legacy_exact_configuration"}
    route = plan.route(slot_ref)
    report = status(session)
    available = [row["config_ref"] for row in report["services"] if row["slot_ref"] == slot_ref
        and row["credential_state"] == "verified" and row["qualification_state"] == "qualified"]
    attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"
        and row["data"].get("service_slot_ref") == slot_ref and row["data"].get("claim_id") == claim_id
        and row["data"]["attempt"]["input"].get("packet", {}).get("basis_id") == strict_basis_id]
    history = []
    accounting = session.execution.ledger(session, records)
    occupied = set() if accounting is None else set(native.accounting.projection(accounting, limits=native._limits(session))["incomplete_attempt_ids"])
    for retained in attempts:
        attempt = session.execution.attempt(retained["attempt"])
        observed = native._latest_observation(records, attempt.attempt_id)
        if observed is None:
            return {"action": "blocked", "config_ref": None, "reason": "external_work_unknown"}
        observed = correlated(attempt, observed, session.execution.profile)
        if any(row["kind"] == "execution_service_refusal" and row["data"]["attempt_id"] == attempt.attempt_id for row in records):
            return {"action": "blocked", "config_ref": None, "reason": "service_result_refused"}
        history.append({"config_ref": attempt.config_ref, "state": observed["state"],
            "reservation_occupied": attempt.attempt_id in occupied, "authenticated": True,
            "final_accounting": observed["final_accounting"], "reason": observed["reason"]})
    result = service_action(plan, slot_ref, history, available)
    if result["action"] == "completed":
        return {"action": "blocked", "config_ref": None, "reason": "owned_result_without_current_application"}
    if attempts and result["action"] == "select":
        frozen = session.policy.to_dict()
        if ("retry" not in frozen["allowed_effects"] or frozen["host_capability_refs"]["retry"] is None
                or view["usage"]["retry_attempts"] >= frozen["continuation_limits"]["retry_attempts"]):
            return {"action": "blocked", "config_ref": None, "reason": "service_retry_budget_exhausted"}
        predecessor = attempts[-1]["attempt"]["attempt_id"]
        final = native._latest_observation(records, predecessor)
        schedule = session.execution.profile.schedule
        delay = 0 if schedule is None else schedule.backoff_seconds
        ready = datetime.fromisoformat(final["observed_utc"].replace("Z", "+00:00")) + timedelta(seconds=delay)
        result.update(predecessor_attempt_id=predecessor, predecessor_observation_ref=digest(final),
            retry_after_utc=ready.isoformat())
    return {**result, "service_slot_ref": route.slot_ref}


def review_current(session, review, view, wanted):
    """The actual vote target must retain its own current qualified identity."""
    config = review.get("profile_ref")
    try:
        actual = required_profile(session, config, review.get("service_slot_ref"))
        if actual["role"] != wanted["role"]:
            return False
        role = session.execution.profile.role(config)
    except (ExecutionError, TypeError, KeyError):
        return False
    rows = [row for row in view["qualification"]["profiles"] if row["profile_ref"] == config]
    if (len(rows) != 1 or rows[0]["qualified"] is not True or rows[0]["independent"] is not True
            or rows[0]["controller_ref"] != role.controller_ref or rows[0]["role"] != wanted["role"]):
        return False
    identity = review.get("service_identity")
    expected = {"controller_ref": role.controller_ref, "model_ref": role.model_ref, "prompt_version": role.prompt_version,
        "execution_profile_sha256": session.execution.profile.sha256, "policy_id": session.policy.policy_id,
        "qualification_ref": rows[0]["qualification_ref"]}
    if type(identity) is not dict or any(identity.get(key) != value for key, value in expected.items()):
        return False
    from .service_authentication import status
    identities = [row for row in status(session)["services"] if row["config_ref"] == config]
    return bool(len(identities) == 1 and identities[0]["credential_state"] == "verified"
        and identities[0]["qualification_state"] == "qualified")
