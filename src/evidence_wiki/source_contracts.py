"""Closed caller declarations for source inspection, routing and host delivery."""

from __future__ import annotations

import copy
import json
import re

from ._pack_io import bounded, unique
from .errors import UsageError
from .onboarding_contract import _matches
from .pack_discovery import owner

MAX_DOCUMENT = 1_048_576
HOST_TOOLS = "evidence-host-tools/v1"
DELIVERY = "evidence-host-delivery/v1"
ROUTES = "evidence-source-routes/v1"
FORMATS = ("markdown", "plain_text", "html", "pdf", "docx", "csv", "json", "url", "image")


def refuse(reason, code="ONBOARDING_INVALID"):
    raise UsageError(code, "Source capability request refused.",
                     remediation="Use current bounded declarations and explicit source scope; retain unverified access and evidence gaps.",
                     details={"field": reason})


def string(maximum=1024):
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"\S"}


def obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def array(item, maximum=32, minimum=0):
    return {"type": "array", "items": item, "maxItems": maximum, "minItems": minimum}


def schemas():
    capture = owner("_host_capture").schema()
    ident = {**string(64), "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$"}
    scope = {"type": "object", "maxProperties": 16, "additionalProperties": string()}
    money = {**string(32), "pattern": r"^(0|[1-9][0-9]{0,8})(\.[0-9]{1,6})?$"}
    limits = obj(max_requests={"type": "integer", "minimum": 0, "maximum": 128},
                 max_bytes={"type": "integer", "minimum": 0, "maximum": 16_777_216}, max_cost_usd=money)
    tools = obj(schema_version={"const": HOST_TOOLS}, tools=array(obj(
        id=ident, version=string(128), kind={"enum": ["browser", "connector", "terminal", "framework", "model_runner", "reviewer", "isolation_host"]},
        operations=array({"enum": ["search", "capture", "export", "extract", "review", "compute", "rpc", "sdk", "mcp"]}, 9, 1),
        scope=array(obj(kind={"enum": ["uri_prefix", "workspace_prefix"]}, value=string(4096)), 16, 1),
        formats=array({"enum": list(FORMATS)}, 8), credential_refs=array({**string(128), "pattern": r"^[A-Z][A-Z0-9_]*$"}, 16),
        limits=limits, authorization={"enum": ["declared", "not_granted", "unknown"]},
        claims=array({"enum": ["independent_review", "sandbox", "mcp", "rpc", "sdk"]}, 5), basis={"const": "declared"}), 32))
    delivery = obj(schema_version={"const": DELIVERY}, capture=capture,
                   content_base64={"type": "string", "maxLength": 699_052})
    requirement = obj(id=ident, question_ids=array(ident, 32, 1), kind=string(128), query_or_identifier=string(4096), scope=scope,
        source_request_id={"anyOf": [ident, {"type": "null"}]},
        output_format={"enum": list(FORMATS)}, content_kinds=array({"enum": list(owner("_host_capture").CONTENT_KINDS)}, 4, 1),
        needs_complete={"type": "boolean"}, source_ids=array(string(256), 16))
    routes = obj(schema_version={"const": ROUTES}, request_id=ident, requirements=array(requirement, 16, 1),
        budget=limits, preferred_tools=array(ident, 32), registered_requests=array(obj(requirement_id=ident,
            phase={"enum": ["discovery", "acquisition"]}, provider_id=ident,
            request={"type": "object", "maxProperties": 64, "additionalProperties": True},
            request_path={"anyOf": [string(512), {"type": "null"}]}, registration=string(256)), 16))
    facts = {"type": "object", "maxProperties": 128, "additionalProperties": True,
             "description": "Bounded observations from the named canonical owner; declarations retain their stated basis."}
    observation = obj(sha256={"type": "string", "pattern": r"^[a-f0-9]{64}$"}, files_observed={"type": "integer", "minimum": 0},
                      scope={"const": "selected_local_inputs"}, atomic_host_snapshot={"const": False})
    inspection = obj(schema_version={"const": "evidence-source-inspection/v1"}, installation=facts, target=facts,
        python=facts, dependencies=array(facts, 4), tools=array(facts, 8), tool_probes=array(facts, 2), providers=array(facts, 160), provider_inventory=facts,
        normalization=facts, host_tools=array(facts, 32), strict=facts, computation=facts, frameworks=facts, sources=array(facts, 32),
        research_ready={"const": False}, providers_enabled={"const": False}, observation=observation, limitations=array(string(), 16))
    outcome = obj(requirement_id=ident, question_ids=array(ident, 32, 1), selected_route={"anyOf": [string(256), {"type": "null"}]},
                  allowed_alternatives=array(string(256), 128), status={"enum": ["blocked", "usable_for_caller_review", "ready_to_attempt", "query_plan_available", "host_action_required"]},
                  gaps=array(string(), 32))
    planned = obj(schema_version={"const": "evidence-source-route-result/v1"}, request_id=ident,
        request_sha256={"type": "string", "pattern": r"^[a-f0-9]{64}$"}, requirements=array(outcome, 16), routes=array(facts, 128),
        remediation=array(facts, 1024), may_continue=array(ident, 16), blocked=array(ident, 16), observation=observation, budget=facts,
        providers_enabled={"const": False}, research_ready={"const": False}, semantic_adequacy={"const": "not_evaluated"})
    delivered = obj(schema_version={"const": "evidence-host-delivery-result/v1"}, status={"enum": ["delivered", "already_present"]},
        path=string(512), source_id=string(256), capture_id=ident, content_sha256=capture["properties"]["content_sha256"],
        content_bytes=capture["properties"]["content_bytes"], inventory={"const": "not_run"}, extraction={"const": "not_run"},
        research_ready={"const": False}, provider_enabled={"const": False}, request_fulfilled={"const": False},
        authority={"const": "caller_declared_capture_with_checked_bytes"})
    result = {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value}
            for name, value in ((HOST_TOOLS, tools), (DELIVERY, delivery), (ROUTES, routes), (owner("_host_capture").SCHEMA, capture),
                               ("evidence-source-inspection/v1", inspection), ("evidence-source-route-result/v1", planned),
                               ("evidence-host-delivery-result/v1", delivered))}
    from .source_capture_contracts import schemas as capture_schemas
    result.update(capture_schemas())
    from .source_usability_operations import schema as usability_schema
    result["evidence-source-usability-view/v1"] = usability_schema()
    from .retrieval_operations import schemas as retrieval_schemas
    result.update(retrieval_schemas())
    result["evidence-host-delivery-result/v2"] = {"$schema": "https://json-schema.org/draft/2020-12/schema",
        **obj(**{**delivered["properties"], "schema_version": {"const": "evidence-host-delivery-result/v2"},
            "content_bytes": {"type": "integer", "minimum": 1, "maximum": 8_388_608},
            "artifact_revision": {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$"},
            "authorized": {"const": False}, "evidence_accepted": {"const": False}})}
    return result


def schema_document(name):
    if name not in schemas():
        refuse("source_schema_unknown", "ONBOARDING_RESOURCE_UNKNOWN")
    return copy.deepcopy(schemas()[name])


def decode(name, raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_DOCUMENT:
        refuse("source_document_bound", "ONBOARDING_LIMIT")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: refuse("source_nonfinite_value"))
        inspected = dict(value) if isinstance(value, dict) else value
        if name == DELIVERY and isinstance(inspected, dict) and isinstance(inspected.get("content_base64"), str):
            inspected["content_base64"] = ""
        bounded(inspected)
        _matches(value, schema_document(name))
        if name == ROUTES:
            identifier = owner("_host_capture").identifier
            identifier(value["request_id"])
            for row in value["requirements"]:
                identifier(row["id"])
                for question in row["question_ids"]:
                    identifier(question)
                if row["source_request_id"] is not None:
                    identifier(row["source_request_id"])
                normalized = owner("_request_scope").normalize_scope(row["scope"])
                if set(normalized) != set(row["scope"]):
                    refuse("source_scope_invalid")
        return value
    except UsageError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        refuse("source_document_invalid")


def reject_execution_and_secrets(value, *, credential_values=()):
    """Reject control/credential fields without treating quoted source prose as code."""
    pending = [value]
    forbidden = {"command", "script", "python", "shell", "env", "headers", "password", "secret", "token", "api_key", "cookie", "credential_value"}
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if any(re.sub(r"[- ]", "_", key.casefold()) in forbidden for key in item):
                refuse("source_executable_or_secret_field")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            if any(secret and len(secret) >= 6 and secret in item for secret in credential_values):
                refuse("source_credential_value_forbidden")


def contract_index():
    return {"capability": "source-usability/v1", "schema_ids": sorted(schemas()),
        "commands": ["agent inspect", "agent routes", "agent source-status", "agent capture", "agent source-schemas", "agent source-guide"],
        "default_inspection_effect": "read_only_no_plugin_or_external_tool_execution",
        "probe_effect": "explicit_bounded_local_process; no evidence or host-protection approval",
        "capture_effect": "explicit new raw bytes and provenance; ingestion remains separately requested",
        "limits": {"tools": 32, "sources": 32, "requirements": 16, "document_bytes": MAX_DOCUMENT,
                   "capture_bytes": owner("_host_capture").MAX_BYTES},
        "revision_capture": {"operation": "sources.capture_revision", "delivery_schema": "evidence-host-delivery/v2",
            "metadata_schema": "evidence-source-capture-input/v1", "formats": ["plain_text", "markdown", "json", "csv", "pdf"],
            "inline_bytes": 49_152, "path_bytes": {"plain_text": 524_288, "markdown": 524_288,
                "json": 1_048_576, "csv": 1_048_576, "pdf": 8_388_608},
            "identity": "original and immutable provenance artifact closure; source path identity remains separate",
            "recovery": "explicit retained pair repair; preserves original partial receipt and proves desired state only",
            "protected_capture": "native signed usage deposit, scrub and ancestry; no inferred grant or raw publication",
            "extraction": "not_run; capture never qualifies source usability or semantic acceptance"},
        "current_usability": {"operation": "sources.usability", "schema": "evidence-source-usability-view/v1",
            "route": "explicit sources.inventory then normalize.sources, followed by read-only current usability",
            "bounds": {"source_ids": 32, "selected_bytes": 33_554_432, "per_file_bytes": 16_777_216},
            "qualification": "native stored consistency and current rights; semantic adequacy remains not_evaluated",
            "normalization_currency": "host_capture_normalization_version 1 for explicit v2 captures; stale/future observations refuse readiness",
            "pdf": "one explicit bounded native extraction; retained page consistency checks never rerun parser or prove extraction fidelity"},
        "retrieval": {"operations": ["sources.search", "sources.build_index"],
            "schemas": ["evidence-retrieval-view/v1", "evidence-retrieval-index-result/v1"],
            "bounds": {"sources": 32, "query_chars": 4096, "results": 32, "excerpt_chars": 4096,
                "selected_bytes": 33_554_432, "index_bytes": 16_777_216, "output_bytes": 1_048_576},
            "permission": "current native source use before rank/snippets/disclosed processed counts",
            "index": "explicit serialized native publication; cache schema and full current material hashes checked at read",
            "fallback": "stale, absent or unsupported cache visibly uses lexical; old runtimes without hard SQLite limits refuse cache use",
            "protected_cache": "persistent build unsupported; eligible-only in-memory FTS allowed",
            "acceptance": "exact mechanical body excerpts and current local/native revision identity; semantic adequacy not_evaluated"},
        "provider_enablement": False, "truth_guarantee": False}
