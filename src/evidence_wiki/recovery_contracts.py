"""Closed bounded restart observations never grant execution or release rights."""

from datetime import datetime

from ._pack_io import bounded, canonical
from .errors import EvidenceWikiError
from .mutation_contracts import detached
from .onboarding_contract import _matches
from .research_session_contracts import NEXT_V2, NEXT_V3, NEXT_V4, refuse
from .research_session_contracts import checked as next_checked
from .research_session_contracts import schema_document as next_schema

STATUS = "research-recovery-status/v1"


def schema_document():
    text = lambda size: {"type":"string", "minLength":1, "maxLength":size}
    hash_ = {**text(71), "pattern":r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    nullable = lambda item: {"anyOf":[item, {"type":"null"}]}
    integer = {"type":"integer", "minimum":0, "maximum":1_000_000}
    boolean = {"type":"boolean"}
    def obj(**fields):
        return {"type":"object", "properties":fields, "required":list(fields), "additionalProperties":False}
    effect = obj(sequence={**integer, "minimum":1}, attempt_id=text(96), operation=text(128),
        arguments_sha256=hash_, state={"enum":["observed", "unknown", "unobserved"]})
    question = obj(slug=text(128), status=text(64), claimed_by=nullable(text(128)), claimed_at=nullable(text(64)), owned_by_session=boolean)
    pending = obj(attempt_id=text(96), job_id=text(96), state={"enum":["dispatch_unknown", "unknown", "running", "succeeded", "failed", "cancelled", "expired", "unavailable"]},
        deadline_utc=text(64), cancellation_requested=boolean, cancellation_confirmed=boolean,
        reservation_occupied=boolean, applied=boolean, observation_ref=nullable(hash_))
    return {"$schema":"https://json-schema.org/draft/2020-12/schema", **obj(schema_version={"const":STATUS},
        run_id=text(96), binding_id=hash_, current_basis_id=hash_, journal_sha256=hash_, terminal=boolean,
        status={"enum":["terminal", "not_started", "reconcilable", "ownership_blocked"]}, original_deadline_utc=nullable(text(64)),
        steps_used=integer, pending=nullable(pending), questions={"type":"array", "items":question, "maxItems":300},
        effects={"type":"array", "items":effect, "maxItems":64}, truncated=boolean, next_sequence=integer,
        next_work=nullable({"anyOf":[next_schema(name) for name in (NEXT_V2, NEXT_V3, NEXT_V4)]}),
        authorized={"const":False}, evidence_accepted={"const":False})}


def checked(value):
    value = detached(value)
    bounded(value)
    try:
        _matches(value, schema_document())
    except (ValueError, EvidenceWikiError):
        refuse("INVALID", "/recovery")
    if len(canonical(value)) > 262_144:
        refuse("LIMIT", "/recovery")
    if value["terminal"] != (value["status"] == "terminal") or value["terminal"] and value["next_work"] is not None:
        refuse("INVALID", "/recovery/terminal")
    for clock in [value["original_deadline_utc"], *([value["pending"]["deadline_utc"]] if value["pending"] else [])]:
        if clock is not None:
            try:
                parsed = datetime.fromisoformat(clock.replace("Z", "+00:00"))
            except ValueError:
                refuse("INVALID", "/recovery/clock")
            if parsed.tzinfo is None:
                refuse("INVALID", "/recovery/clock")
    sequences = [row["sequence"] for row in value["effects"]]
    if sequences != sorted(set(sequences)) or sequences and value["next_sequence"] != sequences[-1]:
        refuse("INVALID", "/recovery/effects")
    slugs = [row["slug"] for row in value["questions"]]
    if len(slugs) != len(set(slugs)):
        refuse("INVALID", "/recovery/questions")
    if value["status"] == "not_started" and (value["original_deadline_utc"] is not None or value["steps_used"] or value["pending"] or value["effects"]):
        refuse("INVALID", "/recovery/not_started")
    if value["next_work"] is not None:
        next_checked(value["next_work"]["schema_version"], value["next_work"])
        current = value["next_work"]["current"]
        if (current["basis_id"] != value["current_basis_id"] or current["execution"]["steps_used"] != value["steps_used"]
                or current["execution"]["pending"] != value["pending"]):
            refuse("INVALID", "/recovery/current")
    return value
