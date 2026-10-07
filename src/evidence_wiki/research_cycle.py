"""One bounded host drive composed over the canonical authenticated run journal."""

from __future__ import annotations

from .cycle_result_contracts import RESULT, checked
from .mutation_contracts import detached, digest, validate_request_id
from .research_session_contracts import refuse


def _originals(current, records, *, terminal):
    attempted = {record["data"].get("original_id") for record in records
        if record["kind"] in {"execution_prepared", "execution_attempt", "execution_application"}}
    result = []
    for original in current["accounting"]["original_outcomes"]:
        identifier = original["original_id"]
        statuses = [question["status"] for question in original["questions"]]
        missing = original["missing_question_ids"]
        native_accepted = original["accepted"] is True and not missing
        if native_accepted:
            disposition = "accepted_current"
        elif statuses and not missing and all(status == "rejected" for status in statuses):
            disposition = "rejected_native"
        elif statuses and not missing and all(status == "deferred" for status in statuses):
            disposition = "abstained_native"
        elif identifier not in attempted:
            disposition = "not_attempted"
        else:
            disposition = "blocked_current" if terminal else "pending"
        result.append({"original_id":identifier, "original_text":original["original_text"],
            "attempted":identifier in attempted, "disposition":disposition, "native_accepted":native_accepted,
            "questions":detached(original["questions"]), "missing_question_ids":detached(missing)})
    return result


def drive(session, *, request_id, expected_basis_id=None, max_steps=64, planner_config_ref=None, operation_control=None):
    """Freeze the drive before effects; exhaustion closes through the existing owner."""
    from . import execution_session as native
    from .operation_control import checkpoint
    checkpoint(operation_control, "research_cycle_start")

    if session.execution is None:
        refuse("EXECUTION_CONFIG_INVALID")
    request_id = validate_request_id(request_id)
    if type(max_steps) is not int or not 1 <= max_steps <= 64:
        refuse("LIMIT", "/max_steps")
    from .research_observation import ResearchObservation
    observed = ResearchObservation(session.root)
    managed = bool(observed.managed)
    observed.finish()
    if managed:
        refuse("MANAGED_RUN_OWNER_REQUIRED")
    if planner_config_ref is not None:
        from .execution_planning import configured
        configured(session, planner_config_ref)
    with native.gate(session):
        records = native.retained(session)
        prior = next((record for record in records if record["kind"] == "execution_cycle"
            and record["request_id"] == request_id), None)
        if prior is not None:
            initial = prior["data"]
            if (initial["max_steps"] != max_steps or initial["planner_config_ref"] != planner_config_ref
                    or expected_basis_id is not None and initial["before_basis_id"] != expected_basis_id):
                refuse("REQUEST_CONFLICT")
        else:
            current = native.current(session)
            basis = current["basis_id"] if expected_basis_id is None else expected_basis_id
            if basis != current["basis_id"]:
                refuse("BASIS_CHANGED")
            initial = {"before_basis_id":basis, "max_steps":max_steps, "planner_config_ref":planner_config_ref}
            native._begin(session)
            native.append(session,"execution_cycle",request_id,initial)
    step = session.run_until_terminal(request_id=native.child_id(request_id,"drive"),
        expected_basis_id=initial["before_basis_id"],max_steps=max_steps,operation_control=operation_control)
    checkpoint(operation_control, "research_cycle_close")
    if step is None or not step["terminal"]:
        step = native.close_drive_bound(session,request_id=native.child_id(request_id,"bound"))
    current = session.current()
    records = session.execution.records(session)
    originals = _originals(current,records,terminal=step["terminal"])
    ids = [row["original_id"] for row in originals]
    if ids != session.binding["original_ids"] or len(set(ids)) != len(ids):
        refuse("SCOPE_ACCOUNTING")
    return checked({"schema_version":RESULT,"request_id":request_id,"run_id":session.run_id,
        "binding_id":digest(session.binding),"initial_basis_id":initial["before_basis_id"],
        "current_basis_id":current["basis_id"],"terminal":step["terminal"],
        "canonical_run_state":step["canonical_run_state"],"outcome":step["outcome"],"reason":step["reason"],
        "step":step,"original_outcomes":originals,"accounting_complete":current["accounting"]["accounting_complete"],
        "current_release":current["canonical_release"],"usage":current["usage"],
        "pending":current["execution"]["pending"],"historical":step["historical"],
        "authorized":False,"evidence_accepted":False})
