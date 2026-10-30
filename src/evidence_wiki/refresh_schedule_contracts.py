"""Bounded host scheduling data never grants research or notification authority."""

from datetime import datetime, timezone

from .mutation_contracts import MutationError, _shape, detached
from .research_session_contracts import refuse

PLAN = "research-refresh-schedule/v1"
NOTICE = "research-refresh-notice/v1"
STATUS = "research-refresh-schedule-status/v1"
CURSOR = "research-refresh-schedule-cursor/v1"
MAX_EVENTS = 256
MAX_COMMANDS = 128


def utc(value):
    if type(value) is not str or len(value) > 64:
        refuse("SCHEDULE_CLOCK_INVALID")
    try:
        clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if clock.tzinfo is None or clock.utcoffset().total_seconds() != 0:
            refuse("SCHEDULE_CLOCK_INVALID")
        return clock.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        refuse("SCHEDULE_CLOCK_INVALID")


def schemas():
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    def integer(low, high):
        return {"type": "integer", "minimum": low, "maximum": high}
    def nullable(value):
        return {"anyOf": [value, {"type": "null"}]}
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    identifier = {**text, "maxLength": 96, "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$(?![\s\S])"}
    sha = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    signature = {"type": "string", "pattern": r"^[a-f0-9]{64}$(?![\s\S])"}
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    plan = obj(schema_version={"const": PLAN}, previous_run_id=identifier, planner_config_ref=identifier,
        interval_seconds=integer(1, 2_592_000), expires_at=text, max_runs=integer(1, 8), max_steps=integer(1, 64),
        max_notices=integer(1, 64), source_updates={"type": "array", "maxItems": 16, "items": opaque})
    notice = obj(schema_version={"const": NOTICE}, notice_id=identifier, source_id={**text, "maxLength": 128},
        sequence=integer(1, 1_000_000), revision_id=nullable(sha))
    cursor = obj(schema_version={"const": CURSOR}, binding_id=sha, sequence=integer(0, MAX_EVENTS),
        event_ref=nullable(sha), authentication=signature)
    job = obj(job_id=sha, run_id=identifier, state={"enum": ["queued", "dispatching", "completed", "unknown", "cancelled"]},
        reason=text, notice_count=integer(0, 64))
    event = obj(sequence=integer(1, MAX_EVENTS), event_id=sha, kind=text, job_id=nullable(sha),
        notice_id=nullable(identifier), reason=text)
    status = obj(schema_version={"const": STATUS}, schedule_id=identifier, binding_id=sha,
        status={"enum": ["waiting", "ready", "running", "blocked", "exhausted"]}, terminal={"type": "boolean"},
        reason=text, previous_run_id=identifier, next_due_at=text, pending=nullable(job),
        allocated_runs=integer(0, 8), max_runs=integer(1, 8), notices_retained=integer(0, 64),
        usage_unknown={"type": "boolean"}, recorded_usage=opaque, current_authority={"type": "boolean"},
        historical={"type": "boolean"}, events={"type": "array", "maxItems": 32, "items": event},
        cursor=cursor, truncated={"type": "boolean"}, current_eligibility={"const": "not_evaluated"},
        authorized={"const": False}, evidence_accepted={"const": False}, daemon_created={"const": False})
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value}
        for name, value in ((PLAN, plan), (NOTICE, notice), (CURSOR, cursor), (STATUS, status))}


def checked(name, value):
    value = detached(value)
    try:
        _shape(value, schemas()[name])
    except (KeyError, MutationError):
        refuse("SCHEDULE_DOCUMENT_INVALID")
    if name == PLAN:
        utc(value["expires_at"])
    return value
