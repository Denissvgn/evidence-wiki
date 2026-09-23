"""Bounded caller guidance and reports, separate from execution authority."""

from __future__ import annotations

import copy

from ._pack_io import canonical, json_document
from .onboarding_contract import _matches
from .pack_discovery import owner
from .planning_contracts import MAX_BYTES, digest, refuse
from .source_commands import _no_secret_values
from .source_contracts import array, obj, string

ACTION = "evidence-research-action/v1"
GUIDANCE = "evidence-research-guidance/v1"
COMPLETION = "evidence-research-completion/v1"
PROGRESS = "evidence-research-progress/v1"
OPERATIONS = (
    "inspect",
    "repair",
    "start",
    "resume",
    "heartbeat",
    "claim",
    "retrieve",
    "request",
    "discover",
    "acquire",
    "ingest",
    "reopen",
    "review",
    "resolve",
    "release",
    "compute",
    "inspect_schedule",
    "stop",
    "managed_resume",
)


def schemas():
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    nullable = lambda value: {"anyOf": [value, {"type": "null"}]}
    # Reuse the strict host's typed action vocabulary without claiming its host boundary.
    action = copy.deepcopy(owner("_strict_contract").schema_documents()["evidence-strict-action/v1"])
    action["properties"].update(
        schema_version={"const": ACTION},
        operation={"enum": list(OPERATIONS)},
        policy_id=nullable(string(128)),
        question_slugs=array(string(128), 300),
        host_implementation=string(64),
        argv=array(string(4096), 64),
        parameters=opaque,
        guides=array(opaque, 8),
        reasons=array(string(), 64),
        authorized={"const": False},
        evidence_accepted={"const": False},
        expires_at=string(64),
    )
    action["required"] = list(action["properties"])
    guidance = obj(
        schema_version={"const": GUIDANCE},
        target=string(4096),
        basis=opaque,
        observed_at=string(64),
        cache=opaque,
        status=opaque,
        questions=array(opaque, 300),
        requests=array(opaque, 200),
        run=nullable(opaque),
        setup=opaque,
        strict=opaque,
        computation=nullable(opaque),
        actions=array(action, 64),
        gaps=array(string(), 512),
        actions_executed={"const": False},
        research_complete={"const": False},
        limitations=array(string(), 16),
    )
    completion = obj(
        schema_version={"const": COMPLETION},
        target=string(4096),
        observed_at=string(64),
        basis=opaque,
        status={"enum": ["complete", "partial", "incomplete"]},
        research_complete={"type": "boolean"},
        accounting_complete={"type": "boolean"},
        partial_allowed={"type": "boolean"},
        original_outcomes=array(opaque, 300),
        publication=nullable(opaque),
        gaps=array(string(), 512),
        source_context=opaque,
        computation=nullable(opaque),
        authority=opaque,
        limitations=array(string(), 16),
    )
    progress = obj(
        schema_version={"const": PROGRESS},
        target=string(4096),
        observed_at=string(64),
        basis=opaque,
        measured=opaque,
        caller_estimates=opaque,
        semantic_evaluation=opaque,
        framework=opaque,
        computation=nullable(opaque),
        limitations=array(string(), 16),
    )
    return {
        key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value}
        for key, value in ((ACTION, action), (GUIDANCE, guidance), (COMPLETION, completion), (PROGRESS, progress))
    }


def checked(value):
    _no_secret_values(value)
    if len(canonical(value)) > MAX_BYTES:
        refuse("research_output_bound", "ONBOARDING_LIMIT")
    _matches(value, schemas()[value["schema_version"]])
    return value


def schema_document(name):
    if name not in schemas():
        refuse("research_schema_unknown", "ONBOARDING_VERSION_UNSUPPORTED")
    return copy.deepcopy(schemas()[name])


def contract_index():
    return {
        "capability": "caller-research/v1",
        "schema_ids": list(schemas()),
        "commands": [
            "agent next",
            "agent start",
            "agent resume",
            "agent heartbeat",
            "agent acquire",
            "agent ingest",
            "agent research-export",
            "agent progress",
            "agent research-guide",
            "agent research-schemas",
        ],
        "assurance": "read-only advice and current owner observations; declarations do not authorize actions",
        "limits": {"output_bytes": MAX_BYTES, "questions": 300, "requests": 200, "actions": 64},
    }


def decode_estimates(raw):
    value = json_document(raw)
    schema = obj(
        **{
            key: {"type": "integer", "minimum": 0, "maximum": 1000000000}
            for key in ("tokens", "seconds", "tool_calls", "setup_interventions", "configuration_repairs")
        }
    )
    schema["required"] = []
    schema["minProperties"] = 1
    _matches(value, schema)
    return {"basis": "caller_estimate", **value, "input_id": digest(value)}
