"""Closed data contracts for evidence policy, claims and independent review."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from _evidence_authority import bounded_list, digest, exact_object, timestamp
from _evidence_revision import canonical_bytes, content_id
from _record_artifacts import artifact_path, json_document
from _temporal_contract import require
from _workspace_module_loader import load_workspace_module

POLICY_SCHEMA = "evidence-strict-policy/v1"
CLAIMS_SCHEMA = "evidence-strict-claims/v1"
REVIEW_SCHEMA = "evidence-strict-review/v1"
RESULT_SCHEMA = "evidence-strict-result/v1"
CLAIMS_SCHEMA_V2 = "evidence-strict-claims/v2"
REVIEW_SCHEMA_V2 = "evidence-strict-review/v2"
RESULT_SCHEMA_V2 = "evidence-strict-result/v2"
_COMPUTATION_CACHE = {}
MAX_BYTES = 1_048_576
MAX_CLAIMS = 100
CHECKS = ("support", "source_suitability", "scope", "time", "units", "counterevidence")
QUALIFICATIONS = ("attributed", "supported", "inference", "contested", "insufficient_evidence")
FIELDS = {
    POLICY_SCHEMA: {"schema_version", "policy_id", "revision", "assurance", "claims_path", "instructions",
                    "rubric", "human_review", "max_source_age_seconds", "max_review_age_seconds"},
    CLAIMS_SCHEMA: {"schema_version", "questions", "claims"},
    REVIEW_SCHEMA: {"schema_version", "basis_id", "claim_id", "generator", "verdicts", "rationale", "reviewed_at", "observation", "snapshot"},
}
FIELDS[CLAIMS_SCHEMA_V2] = FIELDS[CLAIMS_SCHEMA]
FIELDS[REVIEW_SCHEMA_V2] = FIELDS[REVIEW_SCHEMA]


def text(value, maximum=4096):
    require(isinstance(value, str) and 0 < len(value) <= maximum and bool(value.strip()), "strict_text_invalid")
    require(not any(0xD800 <= ord(char) <= 0xDFFF for char in value), "strict_unicode_invalid")
    return value


def identifier(value):
    text(value, 128)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is not None, "strict_identity_invalid")
    return value


def document(value, schema):
    # The byte reader must also bound the file before decoding it.
    pending = [(value, 1)]
    nodes = 0
    string_bytes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        require(depth <= 16 and nodes <= 65536, "strict_tree_bound_exceeded")
        if isinstance(item, dict):
            require(len(item) <= (4096 if schema == REVIEW_SCHEMA_V2 else 128)
                    and all(isinstance(key, str) for key in item), "strict_object_bound_exceeded")
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif isinstance(item, list):
            require(len(item) <= 4096, "strict_array_bound_exceeded")
            pending.extend((part, depth + 1) for part in item)
        elif isinstance(item, str):
            require(len(item) <= 65536, "strict_string_bound_exceeded")
            string_bytes += len(item.encode("utf-8"))
            require(string_bytes <= MAX_BYTES, "strict_document_bound_exceeded")
    validate_shape(value, schema_documents()[schema])
    data = canonical_bytes(value)
    require(len(data) <= MAX_BYTES, "strict_document_bound_exceeded")
    value = json_document(data)
    exact_object(value, FIELDS[schema])
    require(value["schema_version"] == schema, "strict_schema_unsupported")
    validate_shape(value, schema_documents()[schema])
    return value


def policy_document(value):
    value = document(value, POLICY_SCHEMA)
    identifier(value["policy_id"])
    identifier(value["revision"])
    require(value["assurance"] in {"artifact_checked", "host_enforced"}, "strict_assurance_unsupported")
    artifact_path(value["claims_path"])
    instructions = value["instructions"]
    require(isinstance(instructions, dict) and 1 <= len(instructions) <= 16, "strict_instructions_invalid")
    for path, expected in instructions.items():
        artifact_path(path)
        digest(expected)
    rubric = exact_object(value["rubric"], {"id", "revision", "criteria"})
    identifier(rubric["id"])
    identifier(rubric["revision"])
    require(set(rubric["criteria"]) == set(CHECKS), "strict_rubric_incomplete")
    for criterion in rubric["criteria"].values():
        text(criterion)
    require(type(value["human_review"]) is bool, "strict_review_policy_invalid")
    for field in ("max_source_age_seconds", "max_review_age_seconds"):
        require(type(value[field]) is int and 1 <= value[field] <= 31_536_000, "strict_age_invalid")
    return value


def claims_document(value):
    schema = value.get("schema_version") if type(value) is dict else None
    require(schema in {CLAIMS_SCHEMA, CLAIMS_SCHEMA_V2}, "strict_schema_unsupported")
    value = document(value, schema)
    questions = bounded_list(value["questions"], maximum=100, minimum=1)
    slugs = set()
    for question in questions:
        exact_object(question, {"slug", "original_id", "question"})
        slug = identifier(question["slug"])
        identifier(question["original_id"])
        require(slug not in slugs, "strict_duplicate_question")
        text(question["question"])
        slugs.add(slug)
    ids = set()
    for claim in bounded_list(value["claims"], maximum=MAX_CLAIMS):
        fields = {"id", "question_slug", "qualification", "text", "scope", "time", "units", "evidence", "premises", "derivation", "limitations"}
        exact_object(claim, fields | ({"calculations"} if schema == CLAIMS_SCHEMA_V2 else set()))
        if claim.get("calculations"):
            require(claim["qualification"] == "inference", "strict_computation_requires_inference")
            for calculation in claim["calculations"]:
                digest(calculation["result_id"])
                text(calculation["expected"], 512)
                text(calculation["unit"], 128)
        identifier(claim["id"])
        require(claim["id"] not in ids, "strict_duplicate_claim")
        ids.add(claim["id"])
        require(claim["question_slug"] in slugs and claim["qualification"] in QUALIFICATIONS, "strict_claim_scope_invalid")
        for field in ("text", "scope", "time", "units"):
            text(claim[field])
        for note in bounded_list(claim["limitations"], maximum=16):
            text(note)
        for premise in bounded_list(claim["premises"], maximum=32):
            identifier(premise)
        require(len(set(claim["premises"])) == len(claim["premises"]), "strict_duplicate_premise")
        if claim["qualification"] == "inference":
            require(bool(claim["premises"]), "strict_inference_premises_missing")
            text(claim["derivation"])
        else:
            require(not claim["premises"] and claim["derivation"] is None, "strict_unexpected_derivation")
        for evidence in bounded_list(claim["evidence"], maximum=16):
            exact_object(evidence, {"source_id", "record_sha256", "observed_at", "capture", "quote", "location_hint", "anchor"})
            text(evidence["source_id"], 256)
            digest(evidence["record_sha256"])
            timestamp(evidence["observed_at"])
            require(evidence["capture"] in {"primary", "excerpt", "summary", "unknown"}, "strict_capture_invalid")
            require((evidence["quote"] is None) != (evidence["anchor"] is None), "strict_grounding_form_invalid")
            if evidence["quote"] is not None:
                text(evidence["quote"])
                if evidence["location_hint"] is not None:
                    text(evidence["location_hint"], 512)
            else:
                require(evidence["location_hint"] is None, "strict_anchor_location_invalid")
                anchor = exact_object(evidence["anchor"], {"pointer", "expected"})
                text(anchor["pointer"], 512)
                text(anchor["expected"], 512)
    # A forward edge would make a missing/cyclic premise silently useful.
    resolved = set()
    for claim in value["claims"]:
        require(set(claim["premises"]) <= resolved, "strict_premise_missing_or_cyclic")
        resolved.add(claim["id"])
    return value


def review_document(value):
    schema = value.get("schema_version") if type(value) is dict else None
    require(schema in {REVIEW_SCHEMA, REVIEW_SCHEMA_V2}, "strict_schema_unsupported")
    value = document(value, schema)
    digest(value["basis_id"])
    identifier(value["claim_id"])
    exact_object(value["generator"], {"payload", "authentication"})
    exact_object(value["verdicts"], set(CHECKS))
    require(all(verdict in {"pass", "fail", "unknown"} for verdict in value["verdicts"].values()), "strict_review_verdict_invalid")
    text(value["rationale"])
    timestamp(value["reviewed_at"])
    observation = exact_object(value["observation"], {"basis_id", "claim_id", "producer_id", "reasons"})
    require(observation["basis_id"] == value["basis_id"] and observation["claim_id"] == value["claim_id"], "strict_observation_binding_invalid")
    digest(observation["producer_id"])
    for reason in bounded_list(observation["reasons"], maximum=32):
        identifier(reason)
    policy_document(value["snapshot"]["policy"])
    require(value["snapshot"]["claim"]["id"] == value["claim_id"], "strict_snapshot_claim_mismatch")
    return value


def artifact_id(value):
    return content_id(value["schema_version"], value)


def validate_shape(value, schema):
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try:
                validate_shape(value, choice)
                return
            except ValueError:
                continue
        require(False, "strict_schema_shape_invalid")
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    require("type" not in schema or type(value) is kinds[schema["type"]], "strict_schema_type_invalid")
    require("const" not in schema or type(value) is type(schema["const"]) and value == schema["const"], "strict_schema_constant_invalid")
    require("enum" not in schema or value in schema["enum"], "strict_schema_enum_invalid")
    if isinstance(value, dict):
        require(set(schema.get("required", [])) <= value.keys(), "strict_schema_field_missing")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        require(extra is not False or not value.keys() - properties.keys(), "strict_schema_unknown_field")
        require(schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", 128), "strict_schema_object_bound")
        for key, part in value.items():
            if key in properties:
                validate_shape(part, properties[key])
            elif isinstance(extra, dict):
                validate_shape(part, extra)
    elif isinstance(value, list):
        require(schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 4096), "strict_schema_array_bound")
        for part in value:
            validate_shape(part, schema.get("items", {}))
    elif isinstance(value, str):
        require(schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 65536), "strict_schema_string_bound")
        require("pattern" not in schema or re.search(schema["pattern"], value) is not None, "strict_schema_pattern_invalid")
    elif type(value) is int:
        require(schema.get("minimum", value) <= value <= schema.get("maximum", value), "strict_schema_integer_bound")


def schema_documents():
    """Return closed, bounded public shapes; owning functions add semantic checks."""
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    def string(maximum=4096):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    def array(item, maximum=100, minimum=0):
        return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}
    def nullable(item):
        return {"anyOf": [item, {"type": "null"}]}
    ident = {**string(128), "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$(?![\s\S])"}
    hashed = {**string(71), "minLength": 71, "pattern": r"^sha256:[0-9a-f]{64}$"}
    time = string(40)
    boolean = {"type": "boolean"}
    reasons = array(ident, 32)
    question = obj(slug=ident, original_id=ident, question=string())
    evidence = obj(source_id=string(256), record_sha256=hashed, observed_at=time,
                   capture={"enum": ["primary", "excerpt", "summary", "unknown"]},
                   quote=nullable(string()), location_hint=nullable(string(512)),
                   anchor=nullable(obj(pointer=string(512), expected=string(512))))
    claim = obj(id=ident, question_slug=ident, qualification={"enum": list(QUALIFICATIONS)}, text=string(),
                scope=string(), time=string(), units=string(), evidence=array(evidence, 16),
                premises=array(ident, 32), derivation=nullable(string()), limitations=array(string(), 16))
    observation = obj(basis_id=hashed, claim_id=ident, producer_id=hashed, reasons=reasons)
    authentication = obj(schema_version={"const": "evidence-authentication/v1"}, scheme={"const": "hmac-sha256"},
        principal=string(256), key_id=string(256), role={"const": "generator"}, policy_id=string(256),
        policy_revision=string(256), issued_at=time, expires_at=time,
        signature={**string(64), "minLength": 64, "pattern": r"^[0-9a-f]{64}$"})
    authorship = obj(payload=obj(schema_version={"const": "evidence-strict-authorship/v1"}, basis_id=hashed, claim_id=ident),
                     authentication=authentication)
    review = {"anyOf": [obj(passed=boolean, reason=ident), obj(passed=boolean, reason=ident, review_id=hashed,
              principal=string(256), independence={"const": "host_controller_assertion"}, human_review=boolean)]}
    policy = obj(schema_version={"const": POLICY_SCHEMA}, policy_id=ident, revision=ident,
        assurance={"enum": ["artifact_checked", "host_enforced"]}, claims_path=string(1024),
        instructions={"type": "object", "minProperties": 1, "maxProperties": 16, "additionalProperties": hashed},
        rubric=obj(id=ident, revision=ident, criteria=obj(**dict.fromkeys(CHECKS, string()))), human_review=boolean,
        max_source_age_seconds={"type": "integer", "minimum": 1, "maximum": 31536000},
        max_review_age_seconds={"type": "integer", "minimum": 1, "maximum": 31536000})
    result = obj(schema_version={"const": RESULT_SCHEMA}, basis_id=hashed, policy_id=hashed, producer_id=hashed,
                 evaluated_at=time, assurance={"enum": ["artifact_checked", "host_enforced"]},
                 questions=array(question, 100, 1), limits=array(string(), 16),
                 claims=array(obj(claim=claim, accepted=boolean, reasons=reasons, review=review, observation=observation,
                                  required_review_role={"enum": ["evaluator", "human-review"]})))
    accepted = obj(**{**claim["properties"], "verification": obj(observation=observation, review=review)})
    publication = obj(schema_version={"const": "evidence-strict-publication/v1"}, basis_id=hashed, policy_id=hashed,
        producer_id=hashed, evaluated_at=time, assurance={"enum": ["artifact_checked", "host_enforced"]},
        verdict={"enum": ["ship", "blocked_on_sources"]}, limitations=array(string(), 16),
        selection_complete=boolean,
        original_outcomes=array(obj(original_id=ident, question_slugs=array(ident, 100, 1),
                                   accepted=boolean, unselected_question_slugs=array(ident)), 100, 1),
        questions=array(obj(**{**question["properties"], "status": {"enum": ["answered", "blocked"]},
            "accepted": boolean, "claims": array(accepted), "gaps": reasons,
            "unresolved_claims": array(obj(id=ident, qualification={"enum": list(QUALIFICATIONS)}, reasons=reasons))}), 100, 1))
    action = obj(schema_version={"const": "evidence-strict-action/v1"}, operation={"enum": ["draft", "answer", "release"]},
        basis_id=hashed, policy_id=hashed, question_slugs=array(ident, 100, 1), parent_order_id={"type": "null"},
        host_implementation={**string(64), "minLength": 64}, action_id=hashed,
        preconditions=array(ident, 8), postconditions=array(ident, 8))
    documents = {POLICY_SCHEMA: policy, CLAIMS_SCHEMA: obj(schema_version={"const": CLAIMS_SCHEMA},
        questions=array(question, 100, 1), claims=array(claim)), REVIEW_SCHEMA: obj(schema_version={"const": REVIEW_SCHEMA},
        basis_id=hashed, claim_id=ident, generator=authorship, verdicts=obj(**dict.fromkeys(CHECKS, {"enum": ["pass", "fail", "unknown"]})),
        rationale=string(), reviewed_at=time, observation=observation,
        snapshot=obj(policy=policy, claim=claim, question=question, metadata={"type": "object"},
                     premises=array(claim, 32), source_revisions={"type": "object", "additionalProperties": hashed, "maxProperties": 64})), RESULT_SCHEMA: result,
        "evidence-strict-publication/v1": publication, "evidence-strict-action/v1": action}
    computation = load_workspace_module(Path(__file__).resolve().parent, "_computation_contract", cache=_COMPUTATION_CACHE)
    calculation = obj(result_id=hashed, pointer={**string(512), "pattern": r"^/(graphs/[A-Za-z][A-Za-z0-9_]*/outputs/[A-Za-z][A-Za-z0-9_]*|aggregations/[A-Za-z][A-Za-z0-9_]*/groups/[0-9]+/metrics/[A-Za-z][A-Za-z0-9_]*)$"},
                      expected=string(512), form={"enum": ["value", "formatted"]}, unit=string(128), rounded=boolean)
    claim_v2 = obj(**claim["properties"], calculations=array(calculation, 16))
    claims_v2 = obj(schema_version={"const": CLAIMS_SCHEMA_V2}, questions=array(question, 100, 1), claims=array(claim_v2))
    review_v2 = deepcopy(documents[REVIEW_SCHEMA])
    review_v2["properties"]["schema_version"] = {"const": REVIEW_SCHEMA_V2}
    snapshot = review_v2["properties"]["snapshot"]
    snapshot["properties"].update(claim=claim_v2, premises=array(claim_v2, 32), computation=nullable(computation.schemas()[computation.RESULT_SCHEMA]))
    snapshot["required"].append("computation")
    result_v2 = deepcopy(result)
    result_v2["properties"]["schema_version"] = {"const": RESULT_SCHEMA_V2}
    result_v2["properties"]["claims"]["items"]["properties"]["claim"] = claim_v2
    publication_v2 = deepcopy(publication)
    publication_v2["properties"]["schema_version"] = {"const": "evidence-strict-publication/v2"}
    publication_v2["properties"]["questions"]["items"]["properties"]["claims"]["items"] = obj(**claim_v2["properties"], verification=obj(observation=observation, review=review))
    documents.update({CLAIMS_SCHEMA_V2: claims_v2, REVIEW_SCHEMA_V2: review_v2, RESULT_SCHEMA_V2: result_v2,
                      "evidence-strict-publication/v2": publication_v2})
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:" + key.replace("/", ":"),
                  "x-evidence-wiki-limits": {"bytes": MAX_BYTES, "claims": MAX_CLAIMS, "depth": 16, "nodes": 65536},
                  **value} for key, value in documents.items()}
