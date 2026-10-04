"""Current restart custody over the existing authenticated native run journal."""

from . import execution_session as native
from .mutation_contracts import digest, validate_request_id
from .recovery_contracts import checked
from .research_observation import ResearchObservation
from .research_session_contracts import refuse


def status(session, *, after_sequence=0, max_effects=32):
    """Observe exact ownership and unresolved reservations without any dispatch."""
    if session.execution is None:
        refuse("EXECUTION_CONFIG_INVALID")
    if type(after_sequence) is not int or not 0 <= after_sequence <= 1_000_000 or type(max_effects) is not int or not 1 <= max_effects <= 64:
        refuse("LIMIT")
    session._check("recovery_status")
    records = native.retained(session)
    current = session.current()
    observed = ResearchObservation(session.root, run_id=session.run_id if records else None)
    if records and native._document(session).get("caller_context") != observed.controls.get("result"):
        observed.finish()
        refuse("BINDING_CHANGED")
    slugs = {question["slug"] for original in current["accounting"]["original_outcomes"] for question in original["questions"]}
    holders = [{"slug": row["slug"], "status": row["status"], "claimed_by": row["claimed_by"] or None,
        "claimed_at": row["claimed_at"] or None, "owned_by_session": row["claimed_by"] == session.context.actor_ref}
        for row in observed.questions if row["slug"] in slugs]
    observed.finish()
    settled = {row["data"]["observation"]["attempt_id"] for row in records if row["kind"] == "execution_native_observation"}
    unknown = {row["data"]["attempt_id"] for row in records if row["kind"] == "execution_native_history_unknown"}
    effects = [{"sequence": index + 1, "attempt_id": row["data"]["reservation"]["attempt_id"],
        "operation": row["data"]["operation"], "arguments_sha256": digest(row["data"]["arguments"]),
        "state": "observed" if row["data"]["reservation"]["attempt_id"] in settled else "unknown" if row["data"]["reservation"]["attempt_id"] in unknown else "unobserved"}
        for index, row in enumerate(records) if row["kind"] == "execution_native" and index + 1 > after_sequence]
    page = effects[:max_effects]
    terminal = bool(current["run"] and current["run"]["terminal"])
    next_work = None if terminal else session.next()
    if (native.retained(session) != records or session.current()["basis_id"] != current["basis_id"]
            or next_work is not None and next_work["current"]["basis_id"] != current["basis_id"]):
        refuse("BASIS_CHANGED")
    return checked({"schema_version": "research-recovery-status/v1", "run_id": session.run_id,
        "binding_id": digest(session.binding), "current_basis_id": current["basis_id"],
        "journal_sha256": digest(records), "terminal": terminal,
        "status": "terminal" if terminal else "ownership_blocked" if any(row["claimed_by"] and not row["owned_by_session"] and row["status"] == "in_progress" for row in holders) else "not_started" if not records else "reconcilable",
        "original_deadline_utc": native.deadline(session).isoformat() if records else None,
        "steps_used": current["execution"]["steps_used"], "pending": current["execution"]["pending"],
        "questions": holders, "effects": page, "truncated": len(effects) > len(page),
        "next_sequence": page[-1]["sequence"] if page else after_sequence,
        "next_work": next_work, "authorized": False, "evidence_accepted": False})


def reconcile(session, *, request_id, expected_basis_id):
    """Reconcile retained native intents, without invoking a new model or fetch."""
    if session.execution is None:
        refuse("EXECUTION_CONFIG_INVALID")
    request_id = validate_request_id(request_id)
    session._check("reconcile_recovery", write=True)
    status(session)
    with native.gate(session):
        records = native.retained(session)
        if not records:
            refuse("RUN_UNKNOWN")
        if native.current(session)["run"]["terminal"]:
            return status(session)
        previous = next((row for row in records if row["kind"] == "execution_recovery" and row["request_id"] == request_id), None)
        if previous is not None:
            if previous["data"]["expected_basis_id"] != expected_basis_id:
                refuse("REQUEST_CONFLICT")
        elif native.current(session)["basis_id"] != expected_basis_id:
            refuse("BASIS_CHANGED")
        else:
            native.append(session, "execution_recovery", request_id, {"expected_basis_id": expected_basis_id})
    native._recover_history(session)
    return status(session)
