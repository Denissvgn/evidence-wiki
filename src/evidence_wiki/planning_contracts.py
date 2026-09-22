"""Bounded research decisions and saved plans; neither is execution authority."""

from __future__ import annotations

import copy
import hashlib

from ._pack_io import canonical, json_document
from ._script_host import shared_assets_root
from .errors import UsageError
from .host_capabilities import known_credentials
from .onboarding_contract import _matches
from .onboarding_schemas import _nullable
from .onboarding_schemas import schema_document as onboarding_schema
from .pack_discovery import owner
from .source_contracts import HOST_TOOLS, array, obj, reject_execution_and_secrets, string
from .source_contracts import schema_document as source_schema

REQUEST = "evidence-research-setup/v1"
PLAN = "evidence-setup-plan/v1"
MAX_BYTES = 1_048_576


def refuse(field, code="ONBOARDING_INVALID"):
    raise UsageError(code, "Research planning request refused.", details={"field": field},
                     remediation="Correct the named input using agent plan-schemas; retain unresolved evidence and authority requirements.") from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def schemas():
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    ident = {**string(128), "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$(?![\s\S])"}
    nullable = _nullable
    quant = obj(fields=array(obj(source_id=ident, pointer=string(512), unit=string(128)), 32, 1),
                references=array(opaque, 32, 1), invariants=array(ident, 64, 1))
    criteria = obj(facet_id=string(128), source_classes=array(string(128), 16, 1),
                   required_scope=array(ident, 16), time=string(), units=string(), counterevidence=string(),
                   stopping=string(), inference=string(), quantitative=nullable(quant))
    question = obj(question_id=ident, template=nullable(string(128)), facets=array(opaque, 32), criteria=array(criteria, 32))
    requirement = obj(source_id=ident, output_format={"enum": ["markdown", "plain_text", "html", "pdf", "csv", "json"]},
                      needs_complete={"type": "boolean"}, scope={"type": "object", "maxProperties": 16, "additionalProperties": string()})
    decisions = obj(
        project_name=string(128), language=string(64), raw_roots=array(string(512), 16, 1),
        mode={"enum": ["strict", "legacy"]}, policy=nullable(opaque),
        trust=nullable(obj(policy_id=string(256), policy_revision=string(256))), reviewer_reference=nullable(string(256)),
        host_reference=nullable(string(256)), question_plans=array(question, 300),
        computation=nullable(opaque), framework=nullable(obj(id=string(128), version=string(128), mode=string(128))),
        discovery=array(string(128), 16), acquisition=array(string(128), 16), allowed_domains=array(string(256), 32),
        source_requirements=array(requirement, 200), host_tools=source_schema(HOST_TOOLS),
        host_token_limit=nullable({"type": "integer", "minimum": 1, "maximum": 1_000_000_000}),
        codebase=nullable(obj(provider=string(128), question_ids=array(ident, 100, 1))),
        project_local=nullable(opaque),
        pack_authoring=nullable(obj(decision=opaque, specification=nullable(opaque), derivation=nullable(opaque))),
        accepted_pack=nullable(obj(catalog=string(4096), revision=string(64), assessment_sha256={**string(64), "pattern": r"^[a-f0-9]{64}$"})),
    )
    decisions["required"] = []
    request = obj(schema_version={"const": REQUEST},
        request={"anyOf": [onboarding_schema("onboarding/research_request/v2"), onboarding_schema("onboarding/research_request/v1")]},
        decisions=decisions)
    plan = obj(schema_version={"const": PLAN}, plan_id={**string(64), "pattern": r"^[a-f0-9]{64}$"},
        request=request, request_sha256=string(64), decision_basis=opaque, bindings=opaque, profile=opaque,
        initialization=opaque, questions=opaque, coverage=array(opaque, 300), sources=opaque, strict=opaque,
        computation=nullable(opaque), framework=nullable(opaque), steps=array(opaque, 16),
        blockers=array(opaque, 4096), setup_ready={"type": "boolean"}, research_ready={"const": False},
        actions_executed={"const": False}, assumptions=array(string(4096), 64), open_decisions=array(string(4096), 64),
        limitations=array(string(), 16))
    plan["properties"]["authoring_action"] = nullable(opaque)
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value} for key, value in ((REQUEST, request), (PLAN, plan))}


def decode(raw, schema=REQUEST):
    value = json_document(raw)
    if schema == REQUEST and isinstance(value, dict) and value.get("kind") == "research_request":
        value = {"schema_version": REQUEST, "request": value, "decisions": {}}
    _matches(value, schemas()[schema])
    reject_execution_and_secrets(value["request"] if schema == PLAN else value, credential_values=known_credentials())
    return value


def normalize(value):
    """Expand only published setup defaults; keep original question bytes intact."""
    result = copy.deepcopy(value)
    payload = result["request"]["payload"]
    choices = result["decisions"]
    mode = "strict" if "strict_evidence" in payload else None
    if mode is None and "mode" not in choices:
        refuse("/decisions/mode_legacy_request_requires_explicit_selection")
    if mode == "strict" and choices.get("mode", mode) != "strict":
        refuse("/decisions/mode_strict_downgrade_forbidden")
    defaults = {
        "project_name": "research", "language": "en", "raw_roots": owner("init_research_workspace").load_yaml(
            shared_assets_root() / "workspace-template/research.yml", "starter")["raw"]["source_roots"],
        "mode": mode, "policy": None, "trust": None, "reviewer_reference": None, "host_reference": None,
        "question_plans": [], "computation": None, "framework": None, "discovery": [], "acquisition": [],
        "allowed_domains": [], "source_requirements": [], "host_tools": {"schema_version": HOST_TOOLS, "tools": []},
        "host_token_limit": None, "codebase": None, "project_local": None,
        "pack_authoring": None, "accepted_pack": None,
    }
    basis = {"caller_fields": sorted(choices), "default_fields": sorted(set(defaults) - set(choices)),
             "authority_basis": "caller_declaration_only"}
    result["decisions"] = {**defaults, **choices}
    # Arrays that represent sets are normalized; question and decomposition order is retained.
    for field in ("discovery", "acquisition", "allowed_domains"):
        values = result["decisions"][field]
        if len(values) != len(set(values)):
            refuse("/decisions/" + field + "_duplicate")
        result["decisions"][field] = sorted(values)
    return result, basis


def blocker(reason, *, questions=(), field="/", stage="setup", facet=None):
    return {"reason": reason, "question_ids": list(questions), "field": field, "stage": stage, "facet_id": facet}


def owned_call(field, operation, *args, **kwargs):
    """Do not replay validator messages containing untrusted input or credentials."""
    try:
        return operation(*args, **kwargs)
    except (Exception, SystemExit):
        refuse(field)


def schema_document(name):
    if name not in schemas():
        refuse("planning_schema_unknown", "ONBOARDING_RESOURCE_UNKNOWN")
    return copy.deepcopy(schemas()[name])


def contract_index():
    return {"capability": "research-planning/v1", "schema_ids": list(schemas()),
            "commands": ["agent plan", "agent plan-check", "agent plan-schemas", "agent plan-guide"],
            "default_effect": "read_only", "apply_available": False, "document_bytes": MAX_BYTES,
            "question_limit": owner("_strict_contract").MAX_CLAIMS,
            "source_of_authority": "external host verification required; plan declarations confer none"}
