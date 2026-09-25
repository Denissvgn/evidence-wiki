"""Closed tool schemas for the optional scoped onboarding transport."""

from __future__ import annotations

import copy

from .onboarding import READ, WRITE
from .planning_contracts import array, obj, string

PROTOCOL_VERSION = "2024-11-05"


def specifications():
    """Declare method bindings; no caller-controlled attribute or command lookup."""
    text, opaque = string(4096), {"type": "object", "additionalProperties": True}
    boolean = {"type": "boolean"}
    result = {}

    def add(operation, method, schema, *, fixed=None, description=None):
        result[operation] = {"method": method, "fixed": fixed or {}, "schema": schema,
            "description": description or operation.replace("_", " ").replace(".", " ") + " through the scoped shared owner."}

    def optional(schema, names):
        schema["required"] = [name for name in schema["required"] if name not in names]
        return schema

    add("bootstrap", "bootstrap", optional(obj(target=text, requirements=array(text, 32),
        assurance={"enum": ["artifact_checked", "host_enforced"]}, request_id=string(128)), {"target", "requirements", "assurance", "request_id"}))
    for name in ("resources", "recipes", "contracts"):
        add(name, name, obj())
    add("resource", "resource", obj(resource_id=string(256)))
    add("pack_list", "pack_list", optional(obj(target=text, catalog=text), {"target", "catalog"}))
    add("pack_show", "pack_show", optional(obj(target=text, catalog=text, path=text, resource=text, selector=text), {"target", "catalog", "path", "resource", "selector"}))
    add("pack_decide", "pack_decide", optional(obj(value=opaque, target=text, catalog=text), {"target", "catalog"}))
    for name in ("pack_scaffold", "pack_derive"):
        add(name, name, obj(value=opaque, output=text))
    add("inspect", "inspect", optional(obj(target=text, source_ids=array(string(256), 200),
        source_paths=array(text, 200), host_tools=opaque), {"source_ids", "source_paths", "host_tools"}))
    for name in ("plan", "check_plan", "apply", "revision_plan", "revision_apply", "migration_plan", "migration_apply",
                 "composition_plan", "fleet_plan", "fleet_apply", "transition_plan", "transition_apply",
                 "instructions_plan", "instructions_apply", "instructions_remove"):
        add(name, name, obj(value=opaque))
    add("composition_apply", "composition_apply", obj(value=opaque, output=text))
    add("capture", "capture", obj(target=text, value=opaque, path=text))
    add("revision_status", "revision_status", optional(obj(target=text, evaluate=boolean), {"evaluate"}))
    add("reevaluate", "reevaluate", obj(target=text, value=opaque))
    add("research_next", "research_next", optional(obj(target=text, agent_id=string(128), run_id=string(128)), {"agent_id", "run_id"}))
    add("research_export", "research_export", optional(obj(target=text, allow_partial=boolean, run_id=string(128)), {"allow_partial", "run_id"}))
    for operation in ("start", "resume", "heartbeat", "ingest"):
        schema = obj(target=text, agent_id=string(128), run_id=string(128))
        if operation == "ingest":
            common = {"target": text, "agent_id": string(128), "run_id": string(128), "request_id": string(128)}
            schema = {"type": "object", "anyOf": [obj(**common, source_id=string(256)), obj(**common, source_path=text)]}
        add("research." + operation, "research", schema, fixed={"operation": operation})
    for operation in ("check", "aggregate", "evaluate", "verify", "schedule", "write", "apply-warnings", "dispatch"):
        schema = optional(obj(target=text, as_of=string(128), expected_result_id=string(128), request_id=string(128),
            cadence_id=string(128), dry_run=boolean), {"as_of", "expected_result_id", "request_id", "cadence_id", "dry_run"})
        add("computation." + operation, "computation", schema, fixed={"operation": operation})
    if set(result) != READ | WRITE:
        raise RuntimeError("Scoped API and tool operations must have one declaration.")
    return result


def manifest(allowed=READ):
    return [{"name": "onboarding_" + name.replace(".", "_").replace("-", "_"),
             "description": row["description"], "inputSchema": copy.deepcopy(row["schema"])}
            for name, row in specifications().items() if name in allowed]


def contract():
    return {"schema_version": "evidence-onboarding-tools/v1", "protocol_version": PROTOCOL_VERSION,
        "server": "serve-onboarding-mcp", "default_grants": sorted(READ), "mutation_grants": sorted(WRITE),
        "tools": manifest(READ | WRITE), "authority": "host-selected roots and explicit operation grants",
        "assurance": "scope is not OS isolation, provider authentication or host-enforced evidence",
        "lifetime": "one installed generation and interpreter; EOF closes the handle; restart after installation changes"}
