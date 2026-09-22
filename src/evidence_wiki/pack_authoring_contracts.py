"""Closed inert authoring, case and assessment records for caller-local packs."""

from __future__ import annotations

import copy
import hashlib

from ._pack_io import canonical, json_document
from .errors import UsageError
from .host_capabilities import known_credentials
from .onboarding_contract import _matches
from .onboarding_schemas import _nullable
from .source_contracts import array, obj, reject_execution_and_secrets, string

SPEC = "evidence-pack-authoring-spec/v1"
DERIVE = "evidence-pack-derivation/v1"
SUITE = "evidence-pack-cases/v1"
OBSERVATIONS = "evidence-pack-observations/v1"
VALIDATION = "evidence-pack-validation/v2"
ASSESSMENT = "evidence-pack-assessment/v1"
DRAFT = "evidence-pack-draft/v1"
MAX_BYTES = 1_048_576


def refuse(field, code="ONBOARDING_INVALID"):
    raise UsageError(code, "Pack authoring request refused.", details={"field": field},
        remediation="Use explicit current candidate/case identities and canonical pack owners; preserve domain and human-review gaps.") from None


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def schemas():
    ident = {**string(64), "pattern": r"^[a-z][a-z0-9_-]{0,63}$(?![\s\S])"}
    hashed = {**string(64), "pattern": r"^[a-f0-9]{64}$(?![\s\S])"}
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    texts = array(string(4096), 64)
    mapping = lambda shape: {"type": "object", "maxProperties": 32, "additionalProperties": shape}
    requirement = obj(id=ident, text=string(4096), meaning={"enum": ["guidance", "policy", "computation", "review", "normalization"]})
    spec = obj(schema_version={"const": SPEC}, name=ident, version=string(64), description=string(4096),
        scope=string(4096), exclusions=texts, intended_users=texts, question_classes=texts, source_classes=texts,
        required_scope_inputs=array(obj(id=ident, description=string())), review_requirements=texts,
        human_gated={"type": "boolean"}, taxonomy=mapping(obj(page_type=ident, description=string())),
        claim_types=array(ident, 32), claim_fields=array(obj(name=ident, type={"enum": ["string", "scalar", "number", "boolean", "list"]}, required={"type": "boolean"}, description=string())),
        extraction_targets=texts, filing_rules=texts, outputs=texts, policies=opaque, policy_rules=opaque,
        request_kinds=array(opaque, 32), scaffolds=mapping(string(65536)), coverage_templates=mapping(opaque),
        recommended_providers=obj(discovery=array(string(128), 16), acquisition=array(string(128), 16)),
        computation=_nullable(opaque), requirements=array(requirement, 64, 1), unresolved=texts)
    base = obj(selector=_nullable(string(256)), path=_nullable(string(4096)), target=_nullable(string(4096)),
               catalog=_nullable(string(4096)), tree_sha256=hashed)
    derive = obj(schema_version={"const": DERIVE}, base=base,
        mode={"enum": ["revision", "specialization"]}, name=string(128), version=string(64), rationale=string(4096),
        changes=array(obj(path=string(512), content=_nullable(string(65536))), 64),
        requirements=array(requirement, 64, 1), unresolved=texts)
    case = obj(id=ident, requirement_ids=array(ident, 64, 1),
        scenario={"enum": ["adequate", "missing", "conflicting", "wrong_scope", "other"]},
        kind={"enum": ["policy", "computation", "semantic"]}, target=_nullable(string(256)),
        inputs=opaque, expected=opaque, rationale=string(4096))
    case["allOf"] = [
        {"if": {"properties": {"kind": {"const": "policy"}}}, "then": {"properties": {"inputs": obj(
            structured=_nullable(opaque), provenance=opaque, question=_nullable(opaque), origin_host=_nullable(string(256)),
            provider_ids=array(string(128), 16), as_of=string(40))}}},
        {"if": {"properties": {"kind": {"const": "computation"}}}, "then": {"properties": {"inputs": obj(
            records=opaque, as_of=_nullable(string(40)))}}},
    ]
    suite = obj(schema_version={"const": SUITE}, draft_id=hashed, cases=array(case, 64, 1),
        exceptions=array(obj(requirement_id=ident, scenario=case["properties"]["scenario"], rationale=string(4096)), 256), limitations=texts)
    observations = obj(schema_version={"const": OBSERVATIONS}, suite_sha256=hashed,
        observations=array(obj(case_id=ident, verdicts=mapping({"enum": ["pass", "fail", "unknown"]}), rationale=string(4096),
            reviewer_reference=string(256), basis={"const": "caller_declared"}), 64))
    identity = obj(tree_sha256=hashed, overlay_sha256=_nullable(hashed))
    validation = obj(schema_version={"const": VALIDATION}, candidate=string(4096), identity=identity,
        checker_sha256=hashed, package_version=string(64), research_contract_version=string(32),
        ok={"type": "boolean"}, checks=array(opaque, 128), smoke=opaque,
        authority={"const": "caller_local_structural_observation"}, semantic_adequacy={"const": "not_evaluated"})
    assessment = obj(schema_version={"const": ASSESSMENT}, draft_id=hashed, candidate=string(4096), identity=identity,
        validation_sha256=hashed, checker_sha256=hashed, suite=suite, suite_sha256=hashed,
        observations=_nullable(observations), reference_basis=opaque, changes=opaque,
        cases=array(opaque, 64), gaps=array(opaque, 256), mechanical_cases_passed=_nullable({"type": "boolean"}),
        mechanical_case_count={"type": "integer", "minimum": 0, "maximum": 64},
        semantic_adequacy={"const": "not_certified"}, independent_review={"const": "not_verified"},
        human_gates_removed={"const": False}, limitations=texts)
    draft = obj(schema_version={"const": DRAFT}, kind={"enum": ["new", "revision", "specialization"]},
        name=string(128), pack_relative=string(512), specification=opaque, specification_sha256=hashed,
        initial_tree_sha256=hashed, base=_nullable(opaque), requirements=array(requirement, 64, 1),
        rationale=string(4096), unresolved=texts, classification=opaque)
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape} for key, shape in (
        (SPEC, spec), (DERIVE, derive), (SUITE, suite), (OBSERVATIONS, observations), (VALIDATION, validation), (ASSESSMENT, assessment), (DRAFT, draft))}


def decode(raw, schema):
    value = json_document(raw)
    _matches(value, schema_document(schema))
    if schema in {SPEC, DERIVE, SUITE, OBSERVATIONS}:
        reject_execution_and_secrets(value, credential_values=known_credentials())
    return value


def schema_document(name):
    if name not in schemas():
        refuse("pack_authoring_schema_unknown", "ONBOARDING_RESOURCE_UNKNOWN")
    return copy.deepcopy(schemas()[name])


def checked(value, schema):
    return decode(canonical(value), schema)


def contract_index():
    return {"capability": "pack-authoring/v1", "schema_ids": list(schemas()),
        "commands": ["pack scaffold", "pack derive", "pack qualify", "pack freeze-cases", "pack assess", "pack accept", "pack resume"],
        "guide": "pack guide --topic authoring", "schemas": "pack schemas",
        "example": "pack guide --topic specification", "references": "pack guide --topic references",
        "effects": "explicit caller-local files or canonical temporary validation; no workspace installation or network",
        "semantic_certification": False, "write_platform": "POSIX descriptor-relative files", "document_bytes": MAX_BYTES}
