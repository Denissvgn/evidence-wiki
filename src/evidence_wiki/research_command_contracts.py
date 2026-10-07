"""Closed noninteractive request/result data; CLI transport grants no authority."""

from .mutation_contracts import MutationError, _shape, canonical, detached
from .research_session_contracts import refuse

REQUEST = "research-command-request/v1"
RESULT = "research-command-result/v1"
OPERATIONS = ("run-cycle", "refresh", "current", "next-work", "brief", "resolution", "recovery-status", "changes", "impact", "history")


def schemas():
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    nullable = lambda value: {"anyOf": [value, {"type": "null"}]}
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    request = obj(schema_version={"const": REQUEST}, operation={"enum": list(OPERATIONS)}, arguments=opaque)
    result = obj(schema_version={"const": RESULT}, operation={"enum": list(OPERATIONS)}, terminal={"const": True},
        status={"enum": ["returned", "refused"]}, result=nullable(opaque), error=nullable(opaque),
        exit_code={"type": "integer", "minimum": 0, "maximum": 255}, authorized={"const": False}, evidence_accepted={"const": False})
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value} for name, value in ((REQUEST, request), (RESULT, result))}


def checked(name, value):
    value = detached(value)
    try:
        _shape(value, schemas()[name])
    except (MutationError, KeyError):
        refuse("COMMAND_DOCUMENT_INVALID")
    if len(canonical(value)) + (name == RESULT) > 1_048_576:
        refuse("LIMIT")
    if name == RESULT:
        if (value["status"] == "refused") != (value["error"] is not None) or (value["status"] == "returned") != (value["result"] is not None):
            refuse("COMMAND_DOCUMENT_INVALID")
    return value


def error_document(error):
    return {"schema_version": "1.0", "error_code": error.error_code, "message": error.message,
        "recoverable": error.recoverable, "remediation": error.remediation, "details": error.details}
