"""Supplemental setup result/checkpoint schemas; local observations confer no authority."""

from __future__ import annotations

import copy

from .onboarding_contract import _matches
from .source_contracts import array, obj, string

RESULT = "evidence-setup-result/v1"
CHECKPOINT = "evidence-setup-checkpoint/v1"


def schemas():
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    nullable = lambda value: {"anyOf": [value, {"type": "null"}]}
    hashed = {**string(64), "pattern": "^[a-f0-9]{64}$"}
    result = obj(schema_version={"const": RESULT}, plan_id=hashed, transaction_id=string(32), target=string(4096),
        checkpoint=string(4096), status={"enum": ["ready", "partially_usable", "needs_input", "failed"]},
        setup_ready={"type": "boolean"}, research_complete={"const": False}, claims_verified={"const": False},
        profile_sha256=hashed, pack=nullable(opaque), installation=opaque, question_map=array(opaque, 300),
        checks=array(opaque, 64), sources=array(opaque, 200), usable_source_count={"type": "integer", "minimum": 0}, evidence_empty={"type": "boolean"},
        blocked_routes=array(opaque, 4096), strict=opaque, computation=opaque, framework=nullable(opaque),
        blockers=array(opaque, 4096), authority=opaque, next_actions=array(opaque, 16),
        recovery={"enum": ["none", "inspect_conflict", "resume"]}, observed_at=string(64), limitations=array(string(), 16))
    checkpoint = obj(schema_version={"const": CHECKPOINT}, plan_id=hashed, transaction_id=string(32),
        state={"enum": ["prepared", "running", "failed", "complete"]}, pending=nullable(string(64)),
        completed=array(string(64), 64), snapshot=nullable(obj(files=array(opaque, 1024), directories=array(opaque, 1024))),
        observations=opaque, results=opaque, clock=nullable(string(64)))
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value} for key, value in ((RESULT, result), (CHECKPOINT, checkpoint))}


def schema_document(name):
    from .planning_contracts import refuse

    if name not in schemas():
        refuse("setup_schema_unknown", "ONBOARDING_VERSION_UNSUPPORTED")
    return copy.deepcopy(schemas()[name])


def checked(value, name):
    _matches(value, schema_document(name))
    return value


def contract_index():
    return {"capability": "workspace-application/v1", "schema_ids": list(schemas()),
            "operations": ["agent apply", "agent setup-guide", "agent setup-schemas"],
            "platform": "POSIX with native descriptor locking and no-follow filesystem operations",
            "assurance": "artifact_checked; local observations are unauthenticated",
            "host_enforced_setup": "unsupported; requires separately protected provisioning",
            "recovery": "same plan, unchanged ownership, repeated observed checks; partial unjournaled writes require inspection",
            "limits": {"artifact_bytes": 1048576, "observation_bytes": 65536, "steps": 64,
                       "workspace_files": 1024, "workspace_bytes": 100663296, "check_seconds": 600}}
