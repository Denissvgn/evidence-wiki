"""A bounded Release job proposes references; native publication remains separate."""

import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import execution_accounting as accounting
from . import execution_session as native
from .agent_execution import ExecutionAttempt
from .artifact_delivery_contracts import CAPABILITIES, INPUT, PROPOSAL, checked, refuse
from .artifact_delivery_session import prepare
from .artifact_delivery_status import completion
from .mutation_contracts import digest
from .workflow_contracts import EXECUTION_OBSERVATION, JOB_V3, validate_job


def reserve(session, request_id, action, view):
    profile, policy = session.execution.profile, session.policy.to_dict()
    plan = profile.artifact_delivery
    if plan is None or action.get("config_ref") != plan.config_ref:
        refuse("release_frozen_selection_required")
    plan.check_profile(profile)
    role, scope = profile.role(plan.config_ref), profile.role_scopes.scope(plan.config_ref)
    if session.context.controller_for(role.role, role.config_ref) != role.controller_ref:
        refuse("release_controller_unavailable")
    session.context.require_grant("research_session.host_job.generate")
    if "model" not in policy["allowed_effects"] or policy["host_capability_refs"].get("model") is None:
        refuse("release_model_capability_missing")
    records = native.retained(session)
    if any(row["kind"] == "execution_attempt" and row["data"].get("purpose") == "release" for row in records):
        refuse("release_attempt_exhausted")
    selected = checked(INPUT, prepare(session))
    if selected["artifact"]["basis_id"] != view["basis_id"]:
        refuse("release_current_basis_changed")
    ledger = session.execution.ledger(session, records)
    projections = [accounting.projection(ledger, limits=native._limits(session))]
    if profile.schedule is not None:
        projections.append(native.role_projection(profile, ledger, records, plan.config_ref))
    if any(row["usage_unknown"] or row["overrun"] for row in projections):
        refuse("release_accounting_unknown")
    job_caps = profile.document()["job_budgets"]
    desired = {"model_calls": plan.model_calls, "tool_calls": plan.tool_calls, "tokens": plan.tokens, "cost": plan.cost_limit}
    cap = {key: min(desired[key], job_caps[key], *(row["remaining"][key] for row in projections))
        for key in ("model_calls", "tool_calls", "tokens")}
    cap["cost"] = str(min(Decimal(desired["cost"]), Decimal(job_caps["cost"]),
        *(Decimal(row["remaining"]["cost"]) for row in projections)))
    now = datetime.now(timezone.utc)
    timeout = min(plan.timeout_seconds, int((native.deadline(session) - now).total_seconds()),
        *(int(Decimal(row["remaining"]["seconds"])) for row in projections))
    if cap["model_calls"] < 1 or cap["tokens"] < 1 or timeout < 1:
        refuse("release_budget_exhausted")
    document = {"purpose": "release", "packet": selected,
        "instructions": "Return only an exact declared artifact identity and one approved opaque destination. Never supply paths, bytes, permissions or approvals."}
    job = validate_job({"schema_version": JOB_V3, "id": request_id, "operation": "generate", "role": role.role,
        "controller_ref": role.controller_ref, "grant_ref": policy["host_capability_refs"]["model"],
        "input_id": session.binding["request_binding"], "basis_id": view["basis_id"], "policy_id": session.policy.policy_id,
        "policy_version": policy["schema_version"].rsplit("v", 1)[-1], "prompt_version": role.prompt_version,
        "revision_precondition": view["basis_id"], "original_text": "Select exact current artifact references only.",
        "scope": [session.binding["original_ids"][0]], "required_capabilities": list(CAPABILITIES),
        "payload": {"question": "Select exact current artifact references only."}, "issued_utc": now.isoformat(), "deadline_utc": (now + timedelta(seconds=timeout)).isoformat(),
        "timeout_seconds": timeout, "max_attempts": 1, "expected_result_schema": EXECUTION_OBSERVATION, "budgets": cap,
        "qualifications": {"assurance": policy["assurance"], "status": "unknown", "evaluator_ref": None,
            "qualification_ref": None, "context_basis": None}, "task_role": "release", "role_scope_sha256": scope.sha256,
        "context_id": "release-" + secrets.token_hex(16), "packet_sha256": digest(document)[7:]}, now_utc=now.isoformat())
    attempt = ExecutionAttempt.create(attempt_id=request_id, nonce=secrets.token_hex(16), run_id=session.run_id,
        binding_id=digest(session.binding), profile=profile, config_ref=plan.config_ref, job=job, input_document=document)
    reservation = {"attempt_id": request_id, "job_id": request_id, "controller_ref": "session-observer",
        "issued_at": job["issued_utc"], "reservation": {**cap, "seconds": str(timeout)},
        "cost_unit": {"currency": profile.currency, "pricing_ref": profile.pricing_ref}, "required_slots": ["dispatch", "result", "usage"]}
    accounting.reserve_attempt(ledger, reservation)
    native.append(session, "execution_attempt", native.child_id(request_id, "intent"),
        {"attempt": attempt.document(), "reservation": reservation, "purpose": "release",
            "original_id": session.binding["original_ids"][0], "claim_id": None,
            "delivery_parent": {"artifact": selected["artifact"], "plan_sha256": plan.sha256,
                "approved_destination_refs": selected["approved_destination_refs"]}})
    return attempt


def apply(session, attempt_id):
    records = native.retained(session)
    row = native._attempt_rows(records)[attempt_id]
    attempt = session.execution.attempt(row["attempt"])
    observation = native._latest_observation(records, attempt_id)
    if observation is None:
        refuse("release_owned_observation_missing")
    from .service_authentication import correlated
    observation = correlated(attempt, observation, session.execution.profile)
    usage = observation["observed_usage"]
    completed = completion(records, native.child_id(attempt_id, "delivery"))
    if (row.get("purpose") != "release" or observation["state"] != "succeeded" or not observation["final_accounting"]
            or observation["started_utc"] is None or observation["reason"] == "offline_fixture"
            or type(usage.get("model_calls")) is not int or usage["model_calls"] < 1
            or completed is None and datetime.now(timezone.utc) >= datetime.fromisoformat(attempt.job["deadline_utc"])):
        refuse("release_execution_not_observed")
    proposed = checked(PROPOSAL, observation["output"])
    expected = row["delivery_parent"]
    artifact = expected["artifact"]
    if (proposed["artifact_id"] != artifact["artifact_id"] or proposed["artifact_sha256"] != artifact["artifact_sha256"]
            or proposed["artifact_bytes"] != artifact["bytes"] or proposed["plan_sha256"] != expected["plan_sha256"]
            or proposed["destination_ref"] not in expected["approved_destination_refs"]):
        refuse("release_proposal_substitution")
    if completed is not None:
        # Historical application replay cannot start a new write or renew its
        # lease. The terminal owner still evaluates current scientific evidence.
        return proposed
    with native.gate(session):
        records = native.retained(session)
        if not any(item["kind"] == "execution_applied" and item["data"]["attempt_id"] == attempt_id for item in records):
            native.append(session, "execution_application", native.child_id(attempt_id, "application"),
                {"attempt_id": attempt_id, "operation": "release", "proposal": proposed, "accepted": False})
            native.append(session, "execution_applied", native.child_id(attempt_id, "applied"),
                {"attempt_id": attempt_id, "operation": "release", "original_id": row["original_id"], "claim_id": None,
                    "profile_ref": attempt.config_ref, "accepted": False})
    from .artifact_delivery_session import deliver
    delivered = deliver(session, request_id=native.child_id(attempt_id, "delivery"), proposal=proposed,
        effect_deadline=attempt.job["deadline_utc"])
    if delivered["status"] not in {"committed", "reconciled"}:
        refuse("release_publication_unsettled")
    return proposed


def before_terminal(session, view, records, action):
    """Automatic release occurs before terminal closure under the frozen plan."""
    plan = session.execution.profile.artifact_delivery
    if (plan is None or action["operation"] not in {"terminal", "release"}
            or action["operation"] == "terminal" and action["reason"] not in {
                "current_outcomes", "unsupported_claim", "insufficient_evidence"}):
        return action
    attempts = [row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"].get("purpose") == "release"]
    if attempts:
        attempt_id = attempts[0]["attempt"]["attempt_id"]
        delivery_id = native.child_id(attempt_id, "delivery")
        if completion(records, delivery_id) is not None:
            return action
        if any(row["kind"] == "execution_applied" and row["data"]["attempt_id"] == attempt_id for row in records):
            return {"operation": "admit", "attempt_id": attempt_id, "reason": "eligible_result_intake"}
        return action
    from .errors import EvidenceWikiError
    try:
        prepare(session)
    except EvidenceWikiError as error:
        if error.error_code == "ARTIFACT_DELIVERY_REFUSED" and error.details.get("reason") == "delivery_current_brief_ineligible":
            return action
        return native._terminal_action(session, view, "blocked_external", "executor_unavailable")
    return {"operation": "generate", "purpose": "release", "config_ref": plan.config_ref,
        "original_id": session.binding["original_ids"][0], "reason": "approved_artifact_delivery"}
