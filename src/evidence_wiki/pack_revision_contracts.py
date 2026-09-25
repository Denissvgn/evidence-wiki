"""Discoverable contracts for explicit same-pack revisions and reevaluation."""

from __future__ import annotations

import copy

from ._pack_io import json_document
from .onboarding_contract import _matches
from .planning_contracts import array, obj, string

PLAN = "evidence-pack-revision-plan/v1"
REQUEST = "evidence-pack-revision-request/v1"
REEVALUATION = "evidence-pack-reevaluation/v1"
OPERATIONS = ("revision-plan", "revision-apply", "revision-status", "reevaluate")


def schemas():
    from .onboarding_schemas import _nullable

    opaque = {"type": "object", "additionalProperties": True}
    request = obj(schema_version={"const": REQUEST}, target=string(4096), path=_nullable(string(4096)),
        catalog=_nullable(string(4096)), revision=_nullable(string(128)), rationale=string(4096),
        keep_local=array(string(512), 256), accept_pack=array(string(512), 256))
    return {REQUEST: request, PLAN: obj(schema_version={"const": PLAN}, plan_id=string(128), request=request,
        candidate_sha256=string(128), qualification=_nullable(opaque), owner_plan=opaque),
        REEVALUATION: obj(schema_version={"const": REEVALUATION}, revision_id=string(128), slug=string(128),
            rationale=string(4096), template=opaque, retired_facets=array(string(128), 128),
            computation_migrations={"type": "object", "maxProperties": 256, "additionalProperties": _nullable(string(256))},
            request_replacements={"type": "object", "maxProperties": 200, "additionalProperties": string(128)})}


def schema_document(name):
    from ._pack_io import refuse

    if name not in schemas():
        refuse("pack_revision_schema_unknown", "ONBOARDING_RESOURCE_UNKNOWN")
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **copy.deepcopy(schemas()[name])}


def decode(raw, schema):
    from .source_commands import _no_secret_values

    value = json_document(raw)
    _matches(value, schema_document(schema))
    _no_secret_values(value)
    return value


def contract_index():
    return {"capability": "pack-revisions/v1", "commands": ["pack " + name for name in OPERATIONS],
        "schema_ids": list(schemas()), "guide": "pack guide --topic revisions", "schemas": "pack schemas",
        "installation_owner": "same-pack refresh with explicit conflict resolutions and recovery",
        "errors": {"DOMAIN_PACK_REVISION_CONFLICT": 3, "COVERAGE_REVISION_REQUIRED": 2, "ORCHESTRATION_ABANDON_BLOCKED": 2},
        "authority": "local structural observations; never semantic certification or signed review",
        "platform": "POSIX no-follow workspace capture and anchored publication; no unrestricted-caller isolation claim",
        "limits": {"questions": 300, "requests": 200, "changes": 1024, "history": 32, "document_bytes": 1_048_576},
        "computation_effects": "Only explicit computation commands write outputs, warning questions or schedules."}
