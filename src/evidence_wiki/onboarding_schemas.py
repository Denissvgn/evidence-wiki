"""Closed, versioned data contracts for caller-driven onboarding.

Schema access is independent of workspace creation, installed assets, provider
imports, and network access. Workflow availability is negotiated separately.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import ONBOARDING_ERROR_CONTRACTS, UsageError

SCHEMA_VERSION = "1.0"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
LIMITS = {
    "document_bytes": 1_048_576,
    "depth": 16,
    "nodes": 65_536,
    "object_properties": 128,
    "array_items": 4096,
    "string_characters": 65_536,
    "questions": 100,
    "derived_questions": 200,
    "sources": 200,
    "host_tools": 32,
    "routes": 256,
    "steps": 64,
    "files": 4096,
    "resources": 64,
    "impact_items": 1000,
}


def _text(maximum: int = 4096) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _object(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": list(properties), "properties": properties,
    }


def _array(items: dict[str, Any], maximum: int = 64, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def _nullable(value: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [value, {"type": "null"}]}


def _integer(maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


_ID = {**_text(128), "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$(?![\s\S])"}
_HASH = {"type": "string", "pattern": r"^[a-f0-9]{64}$", "minLength": 64, "maxLength": 64}
_PATH = {**_text(1024), "description": "Portable relative path; the filesystem owner enforces containment."}
_TIME = {**_text(20), "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"}
_BOOL = {"type": "boolean"}
_NOTES = _array(_text())
_VERSIONS = _object(
    package_version=_text(64), starter_version=_text(64), library_api_version=_text(32),
    profile_schema_version=_text(32), research_contract_version=_text(32),
)
_TARGET = _object(writable_root=_text(4096), relative_path=_PATH)
_FILE_IDENTITY = _object(device=_text(64), inode=_text(64))
_TARGET_BASIS = _object(
    target=_TARGET, root_identity=_FILE_IDENTITY, parent_identity=_FILE_IDENTITY,
    state=_enum("absent", "empty"), directory_identity=_nullable(_FILE_IDENTITY),
)
_PACK = _object(
    name=_ID, version=_text(64), origin=_enum("bundled", "caller_local", "workspace_installed"),
    locator=_text(4096), overlay_sha256=_HASH, tree_sha256=_HASH,
    research_contract_version=_text(32),
)
_AUTHORITY = _object(
    role=_enum("caller", "managed_worker", "external_host"),
    reference=_text(512),
    allowed_actions=_array(_enum(
        "local_setup", "local_research", "local_pack_authoring", "same_pack_refresh",
        "discovery", "acquisition", "host_capture", "dependency_install", "export",
    )),
    source_scope=_array(_text(2048)),
    writable_roots=_array(_text(4096), 16, 1),
    credential_references=_array({**_text(128), "pattern": r"^env:[A-Z][A-Z0-9_]*$(?![\s\S])"}, 32),
)
_DOMAIN = _object(
    mode=_enum("none", "domain_pack", "project_local", "deferred"),
    pack=_nullable(_PACK), rationale=_text(),
)
_QUESTION = _object(id=_ID, text=_text())
_DERIVED = _object(id=_ID, text=_text(), original_ids=_array(_ID, LIMITS["questions"], 1))
_BUDGETS = _object(
    questions=_integer(1000, 1), source_requests=_integer(1000),
    downloads=_integer(1000), bytes=_integer(1_073_741_824), seconds=_integer(86_400, 1),
)
_CAPABILITY = _object(
    id=_ID, route=_enum("built_in", "registered_provider", "host_capture"),
    available=_enum("yes", "no", "unknown"), compatible=_enum("yes", "no", "unknown"),
    configured=_enum("yes", "no", "unknown"), authorized=_enum("granted", "denied", "unknown"),
    credentials=_enum("present", "missing", "not_required", "unknown"),
    connectivity=_enum("untested", "reachable", "failed"),
    capture=_enum("supported", "unsupported", "unknown"),
    evidence_fit=_enum("usable", "insufficient", "unknown"),
    basis=_enum("declared", "observed"), observed_at=_nullable(_TIME), limitations=_NOTES,
)
_BOUNDS = _object(total=_integer(1_000_000), returned=_integer(4096), truncated=_BOOL)
_RESOURCE = _object(
    id=_text(128), version=_text(32), media_type=_enum("application/schema+json", "text/markdown", "application/json"),
    sha256=_HASH, content=_text(LIMITS["string_characters"]),
)
_RESOURCE_METADATA = _object(**{key: value for key, value in _RESOURCE["properties"].items() if key != "content"})
_RESOURCE_METADATA["properties"]["media_type"] = _enum(
    "application/schema+json", "text/markdown", "application/json", "text/x-python",
)
_RESOURCE_V2 = _object(**_RESOURCE_METADATA["properties"], content=_text(LIMITS["string_characters"]))
_CHECKER = _object(available=_BOOL, basis=_text(), runtime_verified={"const": False})
_SUMMARY = _object(
    installation=_VERSIONS, resources=_array(_RESOURCE_METADATA, LIMITS["resources"]),
    schema_ids=_array(_text(128), LIMITS["resources"]),
    operations=_array(_object(name=_text(128), effect=_enum("read", "write", "temporary_write", "depends_on_options"), workspace_required=_BOOL)),
    strict=_object(
        capability=_text(), schemas=_array(_text(128)), checker=_CHECKER,
        default_request_schema=_text(), assurance_modes=_array(_text(64)),
        host_api=_text(), host_platform=_text(), host_probe={"const": "not_run"},
        protected_parent_orders={"const": False}, semantic_truth_guarantee={"const": False},
    ),
    computation=_object(
        capability=_text(), schemas=_array(_text(128)), checker=_CHECKER,
        numeric_wire=_text(), clock=_text(), evidence_limit=_text(),
    ),
    frameworks=_object(qualified=_array(_text()), basis=_text()),
    limits=_object(summary_bytes=_integer(1_048_576), resource_bytes=_integer(1_048_576)),
)
_ROUTE = _object(
    question_ids=_array(_ID, LIMITS["questions"], 1), source_id=_ID,
    provider_id=_nullable(_text(128)), host_tool_id=_nullable(_ID),
    route=_enum("built_in", "registered_provider", "host_capture", "local_file", "unavailable"),
    capture_format=_text(64), normalizer=_nullable(_text(128)),
    authorization=_enum("granted", "denied", "unknown"), limitations=_NOTES,
)
_STEP = _object(
    id=_ID, operation=_enum(
        "initialize", "question_intake", "coverage_setup", "local_source_delivery",
        "inventory", "normalize", "doctor", "smoke", "lint",
    ),
    depends_on=_array(_ID), paths=_array(_PATH, LIMITS["files"]),
)
_CHECK = _object(
    id=_ID, status=_enum("pending", "passed", "failed", "blocked", "not_run"),
    observed_at=_nullable(_TIME), exit_code=_nullable(_integer(255)),
    summary=_text(), evidence_sha256=_nullable(_HASH),
)
_QUESTION_MAP = _object(
    original_id=_ID, question_slugs=_array(_ID, LIMITS["derived_questions"]),
    outcome=_enum("pending", "seeded", "intaken", "deferred", "rejected"), reason=_nullable(_text()),
)
_FILE = _object(path=_PATH, sha256=_HASH, bytes=_integer(16_777_216))
_INPUT = _object(root=_text(4096), path=_PATH, sha256=_HASH, bytes=_integer(16_777_216))
_INTERPRETER = _object(
    selection=_enum("current", "explicit"), executable=_text(4096),
    implementation=_text(64), python_version=_text(64), executable_sha256=_HASH,
    environment_sha256=_HASH,
)

_PAYLOADS = {
    "capabilities": _SUMMARY,
    "resources": _object(resources=_array(_RESOURCE_METADATA, LIMITS["resources"]), bounds=_BOUNDS),
    "error": {**_object(
        schema_version={"const": SCHEMA_VERSION},
        error_code=_enum(*ONBOARDING_ERROR_CONTRACTS),
        message=_text(), recoverable=_BOOL, remediation=_text(),
        details={**_object(field=_text(2048)), "required": []},
    ), "allOf": [
        {"if": {"properties": {"error_code": {"const": code}}},
         "then": {"properties": {"recoverable": {"const": recoverable}}}}
        for code, (_, recoverable) in ONBOARDING_ERROR_CONTRACTS.items()
    ]},
    "bootstrap": _object(
        installation=_VERSIONS, python_version=_text(64),
        workspace=_enum("absent", "present", "invalid", "not_inspected"),
        guide=_RESOURCE, schema_ids=_array(_text(128), LIMITS["resources"]),
        supported_operations=_array(_text(128)), limitations=_NOTES,
    ),
    "inspection": _object(
        installation=_VERSIONS, target=_nullable(_TARGET),
        scope=_enum("installation", "target", "workspace"),
        capabilities=_array(_CAPABILITY, LIMITS["routes"]), bounds=_BOUNDS, limitations=_NOTES,
    ),
    "research_request": _object(
        goal=_text(16_384), questions=_array(_QUESTION, LIMITS["questions"], 1),
        derived_questions=_array(_DERIVED, LIMITS["derived_questions"]), target=_TARGET,
        outputs=_array(_enum("markdown", "json", "csv", "marp", "presentation_outline"), 5, 1),
        scope=_array(_object(name=_ID, value=_text())), domain=_DOMAIN,
        sources=_array(_object(id=_ID, locator=_text(4096), kind=_text(64),
                               question_ids=_array(_ID, LIMITS["questions"], 1)), LIMITS["sources"]),
        host_tools=_array(_object(id=_ID, kind=_enum("browser", "connector", "terminal"),
                                  capabilities=_NOTES, basis={"const": "declared"}), LIMITS["host_tools"]),
        authority=_AUTHORITY, budgets=_BUDGETS, assumptions=_NOTES, open_decisions=_NOTES,
    ),
    "setup_plan": _object(
        request_sha256=_HASH, installation=_VERSIONS, starter_tree_sha256=_HASH,
        target_basis=_TARGET_BASIS, interpreter=_INTERPRETER, pack=_nullable(_PACK),
        inputs=_array(_INPUT, LIMITS["files"]), authority=_AUTHORITY,
        profile={
            **_object(workspace_init={"type": "object"}),
            "description": "Opaque owned profile: init_research_workspace.validate_profile must also accept workspace_init.",
        },
        question_map=_array(_QUESTION_MAP, LIMITS["questions"], 1),
        routes=_array(_ROUTE, LIMITS["routes"]), steps=_array(_STEP, LIMITS["steps"], 1),
        assumptions=_NOTES, open_decisions=_NOTES,
    ),
    "setup_receipt": _object(
        plan_sha256=_HASH, transaction_id=_ID, target=_TARGET,
        state=_enum("prepared", "initializing", "initialized", "checking", "ready", "blocked", "failed"),
        checkpoint=_PATH, checks=_array(_CHECK, LIMITS["steps"]),
        question_map=_array(_QUESTION_MAP, LIMITS["questions"], 1),
        setup_ready=_BOOL, research_complete={"const": False},
        recovery=_enum("resume", "replan", "inspect_conflict", "none"),
        observed_at=_TIME, limitations=_NOTES,
    ),
    "setup_checkpoint": _object(
        plan_sha256=_HASH, transaction_id=_ID, target_basis=_TARGET_BASIS,
        target_identity=_nullable(_FILE_IDENTITY),
        state=_enum("prepared", "initializing", "initialized", "checking", "ready", "blocked", "failed"),
        completed_steps=_array(_ID, LIMITS["steps"]),
        owned_files=_array(_FILE, LIMITS["files"]),
        owned_directories=_array(_object(path=_PATH, identity=_FILE_IDENTITY), LIMITS["files"]),
        pending_write=_nullable(_object(path=_PATH, before_sha256=_nullable(_HASH), after_sha256=_HASH)),
        observed_at=_TIME,
    ),
    "pack_spec": _object(
        name=_ID, version=_text(64), research_contract_version=_text(32),
        scope=_text(), exclusions=_NOTES, intended_users=_NOTES,
        question_classes=_array(_text(), 64, 1), source_classes=_array(_text(), 64, 1),
        extraction_targets=_NOTES, claim_fields=_NOTES, coverage_requirements=_NOTES,
        output_forms=_NOTES,
        recommended_providers=_object(discovery=_array(_text(128)), acquisition=_array(_text(128))),
        human_review_requirements=_NOTES, base=_nullable(_PACK),
        observed_gap=_nullable(_text()), intended_change=_nullable(_text()),
    ),
    "revision_impact": _object(
        base=_PACK, candidate=_PACK, workspace_sha256=_HASH,
        changes=_array(_object(
            kind=_enum("file", "policy", "question", "request_kind", "coverage"),
            identity=_text(1024), change=_enum("added", "removed", "modified"),
            semantics=_enum("presentation", "requirements", "unknown"),
        ), LIMITS["impact_items"]),
        affected_questions=_array(_ID, LIMITS["impact_items"]),
        unresolved_mappings=_NOTES, reevaluation_required=_BOOL, bounds=_BOUNDS, limitations=_NOTES,
    ),
    "resource": _RESOURCE,
}


def _document(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": f"urn:evidence-wiki:onboarding:{kind}:1",
        "title": f"EvidenceWiki {kind.replace('_', ' ')}",
        "description": "Structural contract only; validation grants no execution or filesystem authority.",
        "x-evidence-wiki-limits": deepcopy(LIMITS),
        "x-evidence-wiki-integer-fields-require-integer-tokens": True,
        **(payload if kind == "error" else _object(
            schema_version={"const": SCHEMA_VERSION}, kind={"const": kind}, request_id=_ID, payload=payload,
        )),
    }


def schema_ids() -> tuple[str, ...]:
    """Return the closed public resource inventory, without probing assets."""
    return (*tuple(f"onboarding/{kind}/v1" for kind in _PAYLOADS), "onboarding/research_request/v2",
            "onboarding/bootstrap/v2", "onboarding/resource/v2")


def schema_document(resource_id: str) -> dict[str, Any]:
    """Return one caller-owned schema; IDs are data, never paths or URLs."""
    if not isinstance(resource_id, str) or resource_id not in schema_ids():
        raise UsageError(
            "ONBOARDING_RESOURCE_UNKNOWN", "Unknown onboarding schema resource.",
            recoverable=False,
            remediation="Select an exact resource ID from onboarding_contract.schema_ids.",
        )
    kind = resource_id.split("/")[1]
    result = deepcopy(_document(kind, _PAYLOADS[kind]))
    if resource_id.endswith("/v2"):
        result["$id"] = f"urn:evidence-wiki:onboarding:{kind}:2"
        result["properties"]["schema_version"] = {"const": "2.0"}
    if resource_id == "onboarding/resource/v2":
        result["properties"]["payload"] = deepcopy(_RESOURCE_V2)
    if resource_id == "onboarding/bootstrap/v2":
        payload = result["properties"]["payload"]
        extra = {
            "resources": _array(_RESOURCE_METADATA, LIMITS["resources"]),
            "environment": _object(implementation=_text(64), platform=_text(64),
                                   workspace_version=_nullable(_text(64)), workspace_schema_version=_nullable(_text(64)),
                                   observation=_text()),
            "strict_selection": _object(
                mode={"const": "strict"}, requested_assurance=_enum("artifact_checked", "host_enforced"),
                effective_assurance={"const": None}, selection_scope={"const": "new_workspace_template"},
                policy_id=_ID, policy_revision=_ID, policy_sha256=_HASH,
                instruction=_RESOURCE_METADATA, route=_text(), reason=_text(),
            ),
        }
        payload["properties"].update(deepcopy(extra))
        payload["required"].extend(extra)
    if resource_id == "onboarding/research_request/v2":
        result["$id"] = "urn:evidence-wiki:onboarding:research_request:2"
        result["properties"]["schema_version"] = {"const": "2.0"}
        payload = result["properties"]["payload"]
        payload["required"].append("strict_evidence")
        payload["properties"]["strict_evidence"] = _object(
            mode={"const": "strict"}, assurance=_enum("artifact_checked", "host_enforced"),
            policy_id=_ID, policy_revision=_ID,
        )
    return deepcopy(result)


def contract_index() -> dict[str, Any]:
    """Describe artifact access without advertising unimplemented workflows."""
    return {
        "schema_version": SCHEMA_VERSION,
        "default_research_request_schema": "onboarding/research_request/v2",
        "schema_ids": list(schema_ids()),
        "schema_accessor": "evidence_wiki.onboarding_schemas.schema_document",
        "decoder": "evidence_wiki.onboarding_contract.decode_document",
        "limits": deepcopy(LIMITS),
        "workflow_commands": ["agent", "agent summary", "agent resources", "agent resource"],
        "decoder_error_exit_code": 2,
        "error_codes": {code: {"exit_code": exit_code, "recoverable": recoverable}
                        for code, (exit_code, recoverable) in ONBOARDING_ERROR_CONTRACTS.items()},
        "contract_document": "docs/agent-contracts.md",
    }
