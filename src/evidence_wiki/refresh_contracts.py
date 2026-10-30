"""Bounded refresh observations; retained outcome is never current approval."""

from .brief_contracts import BRIEF
from .brief_contracts import checked as checked_brief
from .cycle_result_contracts import checked as checked_cycle
from .mutation_contracts import MutationError, _shape, canonical, detached
from .research_session_contracts import refuse

RESULT = "research-refresh-result/v1"


def schema_document():
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    sha = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    null = {"type": "null"}
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    nullable = lambda value: {"anyOf": [value, null]}
    identifiers = {"type": "array", "maxItems": 64, "items": text}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **obj(
        schema_version={"const": RESULT}, request_id=text, run_id=text, previous_run_id=text,
        terminal={"const": True}, outcome=text, reason=text, historical={"type": "boolean"}, brief_error=nullable(text),
        parent_binding_id=sha, parent_terminal_ref=sha, prior_recorded_outcome=text,
        prior_current_eligibility={"const": "not_evaluated"}, refresh_id=nullable(sha),
        reused_source_ids=identifiers, changed_source_ids=identifiers,
        source_updates={"type": "array", "maxItems": 16, "items": opaque},
        unresolved_source_update_request_ids={"type": "array", "maxItems": 16, "items": text},
        cycle=opaque, superseding_brief=nullable(opaque),
        current_acceptance={"const": "native_current_brief_only"}, approval_reuse={"const": False},
        usage=opaque, previous_usage=opaque,
        budget_scope={"const": "explicit_new_run_allocation; previous_accounting_retained_not_reset"},
        authorized={"const": False}, evidence_accepted={"const": False})}


def checked(value):
    value = detached(value)
    try:
        _shape(value, schema_document())
    except MutationError:
        refuse("REFRESH_RESULT_INVALID")
    if len(canonical(value)) > 1_048_576:
        refuse("LIMIT")
    checked_cycle(value["cycle"])
    if value["superseding_brief"] is not None:
        checked_brief(BRIEF, value["superseding_brief"])
    if (value["outcome"], value["reason"]) != current_outcome(value["cycle"], value["superseding_brief"]):
        refuse("REFRESH_RESULT_INVALID")
    if value["run_id"] == value["previous_run_id"] or value["cycle"]["run_id"] != value["run_id"]:
        refuse("REFRESH_RESULT_INVALID")
    if set(value["reused_source_ids"]) & set(value["changed_source_ids"]):
        refuse("REFRESH_RESULT_INVALID")
    if value["outcome"] in {"accepted", "partial"} and (value["refresh_id"] is None or value["superseding_brief"] is None):
        refuse("REFRESH_RESULT_INVALID")
    return value


def schemas():
    return {RESULT: schema_document()}

def current_outcome(cycle, brief):
    if cycle["outcome"] not in {"accepted", "partial"}:
        return cycle["outcome"], cycle["reason"]
    if brief is None:
        return "blocked_external", "superseding_brief_unavailable"
    if brief["verdict"] not in {"accept", "partial"}:
        return "abstained", "superseding_brief_" + brief["verdict"]
    if brief["verdict"] == "partial":
        return "partial", "superseding_brief_partial"
    return cycle["outcome"], cycle["reason"]
