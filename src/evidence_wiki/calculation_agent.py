"""One authenticated Calculation worker proposes only frozen native formulas."""

from datetime import datetime, timezone

from evidence_wiki import execution_session as native
from evidence_wiki.mutation_contracts import digest, validate_request_id
from evidence_wiki.research_session import ResearchSession

from .calculation_contracts import choose, refuse
from .calculation_session import current as calculation


def _settle_refusal(session, request_id, identity, attempt, observation, reason, result_id):
    """Settle only an authenticated known-final attempt, retaining all usage."""
    with native.gate(session):
        native.append(session, "execution_calculation_refused", native.child_id(request_id, "refused"),
            {"attempt_id":attempt.attempt_id, "attempt_ref":digest(attempt.document()),
                "observation_ref":digest(observation), "identity":identity, "reason":reason,
                "result_id":result_id, "accepted":False})
        native.append(session, "execution_applied", native.child_id(attempt.attempt_id, "applied"),
            {"attempt_id":attempt.attempt_id, "operation":"calculation_refused", "original_id":identity["original_id"]})
    refuse(reason)


def propose(session, *, request_id, original_id, config_ref, as_of=None):
    """Retain actual execution before reducing its output against current arithmetic."""
    if type(session) is not ResearchSession or session.execution is None:
        refuse("bound_execution_session_required")
    request_id = validate_request_id(request_id)
    session._check("calculate", write=True)
    session.context.require_grant("calculation_agent.propose")
    registry = session.execution.profile.role_scopes
    role = session.execution.profile.role(config_ref)
    if (registry is None or registry.scope(config_ref).task_role != "calculation" or role.role != "host_worker"
            or session.context.controller_for(role.role, config_ref) != role.controller_ref):
        refuse("scoped_calculation_controller_required")
    registry.scope(config_ref).require("model")
    registry.scope(config_ref).require("calculation.propose")
    identity = {"original_id":original_id, "config_ref":config_ref, "as_of":as_of}
    attempt_id = native.child_id(request_id, "calculation-agent")
    with native.gate(session):
        previous_refusal = next((row["data"] for row in native.retained(session)
            if row["kind"] == "execution_calculation_refused" and row["request_id"] == native.child_id(request_id, "refused")), None)
        if previous_refusal is not None:
            if previous_refusal["identity"] != identity:
                refuse("request_conflict")
            native.append(session, "execution_applied", native.child_id(attempt_id, "applied"),
                {"attempt_id":attempt_id, "operation":"calculation_refused", "original_id":original_id})
            refuse(previous_refusal["reason"])
    result = calculation(session, request_id=native.child_id(request_id, "owner"), as_of=as_of)
    if result["status"] != "passed" or any(not item["passed"] or not item["complete"] for item in result["invariants"]):
        refuse("invariants_not_passed")
    with native.gate(session):
        records = native.retained(session)
        previous = next((row for row in records if row["kind"] == "execution_calculation_instruction" and row["request_id"] == request_id), None)
        if previous is not None and previous["data"] != identity:
            refuse("request_conflict")
        row = next((item["data"] for item in records if item["kind"] == "execution_attempt"
            and item["data"]["attempt"]["attempt_id"] == attempt_id), None)
        saved = next((item["data"] for item in records if item["kind"] == "execution_calculation_selection"
            and item["request_id"] == native.child_id(request_id, "selection")), None)
        if saved is not None:
            if saved["selection"]["reference"]["result_id"] != result["result_id"]:
                refuse("current_computation_changed")
            native.append(session, "execution_applied", native.child_id(attempt_id, "applied"),
                {"attempt_id":attempt_id, "operation":"calculation", "original_id":original_id})
            return saved
        current = native.current(session)
        if current["qualification"]["state"] != "qualified" or original_id not in session.policy.to_dict()["scope"]["question_ids"]:
            refuse("qualified_quantitative_original_required")
        if previous is None:
            native.append(session, "execution_calculation_instruction", request_id, identity)
        if row is None:
            attempt = native.reserve(session, attempt_id, {"operation":"generate", "purpose":"calculation", "original_id":original_id,
                "config_ref":config_ref, "calculation_result":result}, current)
        else:
            if (row["purpose"] != "calculation" or row["original_id"] != original_id
                    or row["attempt"]["config_ref"] != config_ref or "calculation_result" not in row):
                refuse("request_conflict")
            attempt = session.execution.attempt(row["attempt"])
            if row["calculation_result"]["result_id"] != result["result_id"]:
                refuse("current_computation_changed")
    observed = native._latest_observation(native.retained(session), attempt_id)
    for _index in range(32):
        if observed is not None and observed["state"] != "running":
            break
        method = "invoke" if observed is None and row is None else "poll"
        observation, reason = native._external(session, method, attempt)
        if reason is not None:
            refuse("calculation_execution_unknown")
        observed = native.intake(session, observation, request_id=native.child_id(request_id, "observation." + str(observation["sequence"])))
    if observed is None or observed["state"] != "succeeded" or not observed["final_accounting"]:
        refuse("calculation_execution_incomplete")
    observed = session.execution.authenticate(observed, attempt)
    try:
        usage = observed.get("observed_usage") or {}
        if (type(usage.get("model_calls")) is not int or usage["model_calls"] <= 0 or observed.get("started_utc") is None
                or observed.get("reason") == "offline_fixture" or role.model_ref.startswith("offline/")):
            refuse("calculation_model_provenance_unknown")
        fresh = calculation(session, request_id=native.child_id(request_id, "owner"), as_of=as_of)
        if fresh["result_id"] != result["result_id"]:
            refuse("current_computation_changed")
        with native.gate(session):
            current_view = native.current(session)
            if (current_view["qualification"]["state"] != "qualified"
                    or current_view["basis_id"] != attempt.job["basis_id"]
                    or datetime.now(timezone.utc) >= datetime.fromisoformat(attempt.job["deadline_utc"])):
                refuse("calculation_application_stale")
            selection = choose(fresh, observed["output"])
            retained = {"attempt_ref":digest(attempt.document()), "selection":selection, "accepted":False,
                "verifier_status":"independent_current_v2_review_required"}
    except Exception as error:
        reason = getattr(error, "details", {}).get("reason")
        if type(reason) is not str or not reason or len(reason) > 128:
            reason = "calculation_application_refused"
        _settle_refusal(session, request_id, identity, attempt, observed, reason, result["result_id"])
    with native.gate(session):
        native.append(session, "execution_calculation_selection", native.child_id(request_id, "selection"), retained)
        native.append(session, "execution_applied", native.child_id(attempt_id, "applied"),
            {"attempt_id":attempt_id, "operation":"calculation", "original_id":original_id})
        return retained
