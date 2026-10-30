"""Pure scheduling over authenticated canonical attempts and measured reservations."""

from __future__ import annotations

from datetime import datetime, timedelta

from . import execution_accounting as accounting
from .mutation_contracts import detached, digest
from .research_session_contracts import refuse


def role_projection(profile, plan, records, config_ref):
    """Project one immutable role/controller bucket without resetting fallback spend.

    The owning session authenticates the complete history before calling this
    reducer. A role slice is an internal prefix, never a new coverage assertion.
    """
    schedule = profile.schedule
    if schedule is None:
        return None
    role = profile.role(config_ref)
    limit = schedule.limit(role.role, role.controller_ref)
    selected = set()
    native = set()
    for record in records:
        if record["kind"] == "execution_attempt":
            attempt = record["data"]["attempt"]
            actual = profile.role(attempt["config_ref"])
            if (actual.role, actual.controller_ref) == (role.role, role.controller_ref):
                selected.add(attempt["attempt_id"])
        elif record["kind"] == "execution_native":
            native.add(record["data"]["reservation"]["attempt_id"])
    known = {record["data"]["attempt"]["attempt_id"] for record in records if record["kind"] == "execution_attempt"}
    if {row["attempt_id"] for row in plan["attempts"]} != known | native:
        refuse("EXECUTION_SCHEDULING_HISTORY_INVALID")
    subset = detached(plan)
    subset["closure"] = None
    subset["attempts"] = [row for row in subset["attempts"] if row["attempt_id"] in selected]
    subset["observations"] = [row for row in subset["observations"]
        if row["envelope"]["attempt_id"] is None or row["envelope"]["attempt_id"] in selected]
    ceilings = {"seconds": str(limit.seconds), "model_calls": limit.model_calls,
        "tool_calls": limit.tool_calls, "tokens": limit.tokens, "cost": limit.cost_limit}
    return accounting.projection(subset, limits=ceilings)


def retry_work(profile, records, pending):
    """Return only configured continuation from fully observed settled work."""
    schedule = profile.schedule
    if schedule is None:
        return None
    identifier = pending["attempt_id"]
    previous = next(row["data"] for row in records if row["kind"] == "execution_attempt"
        and row["data"]["attempt"]["attempt_id"] == identifier)
    final = next((row["data"]["observation"] for row in records if row["kind"] == "execution_observation"
        and row["data"]["attempt_id"] == identifier and row["data"]["observation"]["final_accounting"]), None)
    if (final is None or pending["reservation_occupied"] or pending["cancellation_requested"]
            or final["state"] not in {"failed", "unavailable", "expired"}
            or final["reason"] not in schedule.retry_reasons):
        return None
    config_ref = previous["attempt"]["config_ref"]
    for chain in schedule.failover:
        if config_ref in chain and chain.index(config_ref) + 1 < len(chain):
            config_ref = chain[chain.index(config_ref) + 1]
            break
    purpose = previous.get("retry_purpose", previous.get("purpose", previous["attempt"]["job"]["operation"]))
    ready = datetime.fromisoformat(final["observed_utc"].replace("Z", "+00:00")) + timedelta(seconds=schedule.backoff_seconds)
    return {"operation": purpose if purpose in {"repair", "adjudicate"} else previous["attempt"]["job"]["operation"],
        "purpose": "retry", "retry_purpose": purpose, "original_id": previous["original_id"],
        "claim_id": previous.get("claim_id"), "config_ref": config_ref,
        "defects": detached(previous["attempt"].get("input", {}).get("defects", [])),
        "predecessor_attempt_id": identifier, "predecessor_observation_ref": digest(final),
        "retry_after_utc": ready.isoformat(), "reason": "bounded_retry",
        **{key: detached(previous[key]) for key in ("disagreement", "target_claim_id") if key in previous}}
