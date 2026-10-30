"""Finite authenticated host queue reduction; notices are inert hints."""

from datetime import timedelta
from decimal import Decimal

from .mutation_contracts import detached, digest
from .refresh_schedule_contracts import MAX_COMMANDS, MAX_EVENTS, utc
from .research_session_contracts import refuse


def event(state, kind, reason, *, job_id=None, notice_id=None):
    if len(state["events"]) >= MAX_EVENTS:
        state["terminal_reason"] = "event_capacity_exhausted"
        return
    row = {"sequence": len(state["events"])+1, "kind": kind, "job_id": job_id,
        "notice_id": notice_id, "reason": reason}
    row["event_id"] = digest(row)
    state["events"].append(row)


def cancel(state, reason):
    job = state["pending"]
    if job is not None and job["state"] == "queued":
        event(state, "job_cancelled", reason, job_id=job["job_id"])
        state["pending"] = None


def ingest(state, notices, permitted):
    for notice in notices:
        identifier, source = notice["notice_id"], notice["source_id"]
        fingerprint = digest(notice)
        prior = state["notices"].get(identifier)
        if prior is not None:
            if prior["sha256"] != fingerprint:
                refuse("SCHEDULE_NOTICE_CONFLICT")
            continue
        if len(state["notices"]) >= state["binding"]["plan"]["max_notices"]:
            state["terminal_reason"] = "notice_capacity_exhausted"
            break
        latest = state["source_sequences"].get(source, 0)
        status = "denied" if source not in permitted else "out_of_order" if notice["sequence"] <= latest else "accepted"
        state["notices"][identifier] = {"sha256": fingerprint, "status": status}
        if status == "accepted":
            state["source_sequences"][source] = notice["sequence"]
            state["change_generation"] += 1
        event(state, "notice_"+status, "inert_source_hint", notice_id=identifier)


def command(state, identifier, payload):
    sha = digest(payload)
    prior = state["commands"].get(identifier)
    if prior is not None:
        if prior["sha256"] != sha:
            refuse("SCHEDULE_REQUEST_CONFLICT")
        return prior, True
    if len(state["commands"]) >= MAX_COMMANDS:
        state["terminal_reason"] = "command_capacity_exhausted"
        return None, False
    prior = {"sha256": sha, "job_id": None, "complete": False}
    state["commands"][identifier] = prior
    return prior, False


def reconcile_due(state, observation, now):
    plan = state["binding"]["plan"]
    if utc(now) < utc(state["last_observed_at"]):
        refuse("SCHEDULE_CLOCK_REGRESSED")
    state["last_observed_at"] = now
    if utc(now) >= utc(plan["expires_at"]):
        state["terminal_reason"] = "schedule_expired"
    if state["allocated_runs"] >= plan["max_runs"] and state["pending"] is None:
        state["terminal_reason"] = "run_allocation_exhausted"
    pending = state["pending"]
    if pending is not None and pending["state"] == "dispatching":
        return
    if state["terminal_reason"] is not None:
        cancel(state, state["terminal_reason"])
        return
    if not observation["authority"]:
        cancel(state, "current_authority_unavailable")
        return
    due = (observation["basis_id"] != state["last_basis_id"] or not observation["eligible"]
        or state["change_generation"] > state["settled_generation"] or utc(now) >= utc(state["next_due_at"]))
    if not due:
        cancel(state, "no_longer_due")
        return
    key = digest({"binding": state["binding_id"], "parent": state["previous_run_id"],
        "basis": observation["basis_id"], "generation": state["change_generation"], "allocation": state["allocated_runs"]})
    if pending is not None and pending["job_id"] == key:
        return
    cancel(state, "coalesced_obsolete_work")
    state["pending"] = {"job_id": key, "run_id": "scheduled-"+key[7:39], "state": "queued",
        "reason": "current_native_refresh_due", "notice_count": state["change_generation"]-state["settled_generation"],
        "previous_run_id": state["previous_run_id"], "request_id": "refresh-"+key[7:39],
        "generation": state["change_generation"], "source_updates": [row for row in plan["source_updates"]
            if row["source_request_id"] not in state["fulfilled_updates"]]}
    event(state, "job_queued", "coalesced_current_work", job_id=key)


def reserve(state, job, request):
    if state["terminal_reason"] is not None or state["allocated_runs"] >= state["binding"]["plan"]["max_runs"]:
        refuse("SCHEDULE_EXHAUSTED")
    job["state"] = "dispatching"
    state["allocated_runs"] += 1
    request["job_id"] = job["job_id"]
    event(state, "job_reserved", "explicit_frozen_run_allocation", job_id=job["job_id"])


def settle(state, job, report, now):
    if state["pending"] is None or state["pending"]["job_id"] != job["job_id"]:
        refuse("SCHEDULE_JOB_CONFLICT")
    if report is None:
        state["usage_unknown"] = True
        state["terminal_reason"] = "dispatch_accounting_unknown"
        outcome, reason = "unknown", "protected_refresh_reconciliation_unavailable"
    else:
        state["previous_run_id"] = report["run_id"]
        state["last_basis_id"] = report["cycle"]["current_basis_id"]
        state["settled_generation"] = job["generation"]
        usage = report["usage"]
        for field in state["usage"]:
            if usage.get(field) is None:
                state["usage_unknown"] = True
            elif field == "cost_limit":
                state["usage"][field] = str(Decimal(state["usage"][field])+Decimal(usage[field]))
            else:
                state["usage"][field] += usage[field]
        if state["usage_unknown"]:
            state["terminal_reason"] = "dispatch_accounting_unknown"
        state["fulfilled_updates"] = sorted(set(state["fulfilled_updates"]) | {
            row["source_request_id"] for row in report["source_updates"] if row["status"] == "fulfilled"})
        outcome, reason = report["outcome"], report["reason"]
    state["jobs"].append({"job_id": job["job_id"], "run_id": job["run_id"], "outcome": outcome, "reason": reason})
    for request in state["commands"].values():
        if request["job_id"] == job["job_id"]:
            request["complete"] = True
    state["pending"] = None
    state["next_due_at"] = (utc(now)+timedelta(seconds=state["binding"]["plan"]["interval_seconds"])).isoformat()
    if state["allocated_runs"] >= state["binding"]["plan"]["max_runs"]:
        state["terminal_reason"] = state["terminal_reason"] or "run_allocation_exhausted"
    event(state, "job_completed", reason, job_id=job["job_id"])
    return detached(state)
