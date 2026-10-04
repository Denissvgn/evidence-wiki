"""Acquisition suggestions require an actual scoped researcher model capability."""

from __future__ import annotations

from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import validate_request_id
from evidence_wiki.research_session import ResearchSession


def capability(session, config_ref):
    """Return the actual frozen proposer route; labels never grant model effects."""
    if type(session) is not ResearchSession or session.execution is None:
        return {"state": "unavailable", "reason": "bound_execution_session_required"}
    session._check("acquire", write=True)
    policy = session.policy.to_dict()
    if "model" not in policy["allowed_effects"] or policy["host_capability_refs"]["model"] is None:
        return {"state": "unavailable", "reason": "model_ungranted"}
    registry = session.execution.profile.role_scopes
    if registry is None:
        return {"state": "unavailable", "reason": "scoped_acquisition_proposer_missing"}
    scope = registry.scope(config_ref)
    role = session.execution.profile.role(config_ref)
    if (scope.task_role != "researcher" or "acquisition.propose" not in scope.capabilities
            or role.role != "host_worker" or session.context.controller_for(role.role, config_ref) != role.controller_ref):
        return {"state": "unavailable", "reason": "scoped_acquisition_proposer_ungranted"}
    session.context.require_grant("research_session.host_job.generate")
    return {"state": "available", "config_ref": config_ref, "controller_ref": role.controller_ref,
        "role_scope_sha256": scope.sha256, "model_grant_ref": policy["host_capability_refs"]["model"]}


def choose(session, *, request_id, instruction, config_ref):
    """Expose finite missing-capability outcomes before any model dispatch."""
    request_id = validate_request_id(request_id)
    from .acquisition_contracts import checked_instruction
    instruction=checked_instruction(instruction)
    route = capability(session, config_ref)
    if route["state"] != "available":
        return {"schema_version": "evidence-acquisition-agent-result/v1", "status": "blocked_external",
            "request_id": request_id, "reason": route["reason"], "proposal": None,
            "model_invoked": False, "source_accepted": False, "rights_inferred": False}
    return _run_scoped_choice(session, request_id, instruction, route)


def _run_scoped_choice(session, request_id, instruction, route):
    from datetime import datetime, timezone

    from evidence_wiki import execution_session as native
    from evidence_wiki.acquisition_contracts import choose as reduce_choice
    from evidence_wiki.mutation_contracts import detached, digest

    if type(instruction) is not dict or instruction.get("execution_authority")!="not_established":
        return _result(request_id,"blocked_external","instruction_invalid")
    with native.gate(session,"acquire"):
        records=native.retained(session)
        previous=next((row for row in records if row["kind"]=="execution_acquisition_choice"
            and row["request_id"]==request_id),None)
        identity={"instruction":detached(instruction),"config_ref":route["config_ref"]}
        if previous is not None and previous["data"]!=identity:
            return _result(request_id,"blocked_external","request_conflict")
        saved=next((row["data"] for row in records if row["kind"]=="execution_acquisition_choice_result"
            and row["request_id"]==native.child_id(request_id,"result")),None)
        if saved is not None:
            attempt_id=native.child_id(request_id,"choice")
            owned=next((item["data"] for item in records if item["kind"]=="execution_attempt"
                and item["data"]["attempt"]["attempt_id"]==attempt_id),None)
            if owned is not None and not any(item["kind"]=="execution_applied" and item["data"]["attempt_id"]==attempt_id for item in records):
                native.append(session,"execution_applied",native.child_id(attempt_id,"applied"),
                    {"attempt_id":attempt_id,"operation":"acquisition","original_id":owned["original_id"]})
            return saved
        native._begin(session)
        if previous is None:
            native.append(session,"execution_acquisition_choice",request_id,identity)
        view=native.current(session)
        if view["qualification"]["state"]!="qualified":
            return _result(request_id,"blocked_external","qualification_unknown")
        from evidence_wiki.acquisition_operations import _request
        _observed,source_view,record=_request(session.root,instruction["request_id"])
        source_view.finish()
        if digest(record)!=instruction.get("request_basis"):
            return _result(request_id,"blocked_external","request_basis_changed")
        originals=[original for original in view["accounting"]["original_outcomes"]
            if set(record["question_slugs"]) <= {question["slug"] for question in original["questions"]}]
        if len(originals)!=1:
            return _result(request_id,"blocked_external","original_scope_unsupported")
        attempt_id=native.child_id(request_id,"choice")
        row=next((row["data"] for row in records if row["kind"]=="execution_attempt"
            and row["data"]["attempt"]["attempt_id"]==attempt_id),None)
        if row is None:
            attempt=native.reserve(session,attempt_id,{"operation":"generate","purpose":"acquisition",
                "config_ref":route["config_ref"],"original_id":originals[0]["original_id"],
                "acquisition_instruction":instruction},view)
        else:
            attempt=session.execution.attempt(row["attempt"])
    observed=native._latest_observation(native.retained(session),attempt_id)
    for _index in range(32):
        if observed is not None and observed["state"]!="running":
            break
        method="invoke" if observed is None and row is None else "poll"
        observation,reason=native._external(session,method,attempt)
        if reason is not None:
            return _result(request_id,"blocked_external",reason,model_invoked=observed is not None)
        observed=native.intake(session,observation,request_id=native.child_id(request_id,"observation."+str(observation["sequence"])))
    if observed is None or observed["state"]!="succeeded" or not observed["final_accounting"]:
        return _result(request_id,"blocked_external","choice_not_completed",model_invoked=observed is not None)
    observed=session.execution.authenticate(observed,attempt)
    with native.gate(session,"acquire"):
        current=native.current(session)
        if (current["basis_id"]!=attempt.job["basis_id"] or current["qualification"]["state"]!="qualified"
                or datetime.now(timezone.utc)>=datetime.fromisoformat(attempt.job["deadline_utc"])):
            result=_result(request_id,"blocked_external","choice_basis_stale",model_invoked=True)
            return _settle(session,request_id,attempt_id,originals[0]["original_id"],result)
        _observed,source_view,record=_request(session.root,instruction["request_id"])
        source_view.finish()
        if digest(record)!=instruction["request_basis"]:
            result=_result(request_id,"blocked_external","request_basis_changed",model_invoked=True)
            return _settle(session,request_id,attempt_id,originals[0]["original_id"],result)
        try:
            selection=reduce_choice(instruction,observed["output"])
        except (ValueError, UsageError):
            selection=None
        result=(_result(request_id,"blocked_external","choice_invalid",model_invoked=True) if selection is None else
            _result(request_id,selection["status"],selection["reason"],proposal=selection,model_invoked=True))
        return _settle(session,request_id,attempt_id,originals[0]["original_id"],result)


def _settle(session,request_id,attempt_id,original_id,result):
    from . import execution_session as native
    native.append(session,"execution_acquisition_choice_result",native.child_id(request_id,"result"),result)
    native.append(session,"execution_applied",native.child_id(attempt_id,"applied"),
        {"attempt_id":attempt_id,"operation":"acquisition","original_id":original_id})
    return result


def _result(request_id,status,reason,*,proposal=None,model_invoked=False):
    value={"schema_version":"evidence-acquisition-agent-result/v1","status":status,"request_id":request_id,
        "reason":reason,"proposal":proposal,"model_invoked":model_invoked,"source_accepted":False,"rights_inferred":False}

    from .acquisition_contracts import schema_document
    from .onboarding_contract import _matches
    _matches(value,schema_document("evidence-acquisition-agent-result/v1"))
    return value
