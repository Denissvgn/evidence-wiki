"""Bounded current resolution reports carry no external-action authority."""

from copy import deepcopy

from evidence_wiki._pack_io import bounded, canonical
from evidence_wiki.errors import EvidenceWikiError, UsageError
from evidence_wiki.mutation_contracts import detached
from evidence_wiki.onboarding_contract import _matches

RESOLUTION = "research-question-resolution/v1"
MAX_BYTES = 1_048_576


def refuse(reason):
    raise UsageError("RESEARCH_RESOLUTION_REFUSED", "Current resolution report refused.",
        details={"reason": reason}, recoverable=False)


def _eligible_shape():
    from .pack_discovery import owner
    native = owner("_strict_contract").schema_documents()
    question = deepcopy(native["evidence-strict-publication/v1"]["properties"]["questions"]["items"])
    claims = []
    for version in ("evidence-strict-publication/v1", "evidence-strict-publication/v2"):
        claim = deepcopy(native[version]["properties"]["questions"]["items"]["properties"]["claims"]["items"])
        claim["properties"].pop("verification")
        claim["required"].remove("verification")
        claims.append(claim)
    question["properties"]["claims"]["items"] = {"anyOf": claims}
    return question


def schema_document():
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    def text(size=256):
        return {"type": "string", "minLength": 1, "maxLength": size}
    def nullable(shape):
        return {"anyOf": [shape, {"type": "null"}]}
    def array(shape, maximum=300):
        return {"type": "array", "items": shape, "maxItems": maximum}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    verdict = {"enum": ["accept", "partial", "abstain", "reject"]}
    question = obj(question_id=text(), slug=text(), outcome={"enum": ["accept", "abstain", "reject"]},
        eligible_claim_refs=array(text(), 128), reasons=array(text(4096), 512),
        current_checks=nullable(obj(source_and_mechanics={"const": "current_native_pass"},
            required_reviews={"const": "current_owned_pass"},
            actual_target_qualification={"const": "current"},
            producer_and_context={"const": "current_owned_proof"})))
    original = obj(original_id=text(), original_text=nullable(text(65536)), outcome=verdict,
        questions=array(question), eligible_questions=array(_eligible_shape()),
        missing_question_ids=array(text()), reasons=array(text(4096), 512))
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **obj(
        schema_version={"const": RESOLUTION}, run_id=text(), binding_id=hashed,
        current_basis_id=nullable(hashed), publication_basis_id={"type": "null"}, policy_id=hashed,
        verdict=verdict, original_outcomes={**array(original, 100), "minItems": 1},
        global_blockers=array(text(4096), 512), accounting_complete={"type": "boolean"},
        current_publication={"type": "null"},
        lifecycle=obj(terminal={"type": "boolean"}, outcome=nullable(text()), reason=nullable(text())),
        resolution_owner={"const": "current_strict_publication_and_authenticated_run_policy"},
        external_action_authorized={"const": False}, authorized={"const": False},
        limitations=array(text(4096), 32))}


def checked(value):
    try:
        bounded(value)
        _matches(value, schema_document())
        if len(canonical(value)) > MAX_BYTES:
            refuse("resolution_document_bound")
        originals = value["original_outcomes"]
        if len({row["original_id"] for row in originals}) != len(originals):
            refuse("resolution_original_duplicate")
        qids = [q["question_id"] for row in originals for q in row["questions"]]
        if len(qids) > 300 or any(len({q["question_id"] for q in row["questions"]}) != len(row["questions"]) for row in originals):
            refuse("resolution_question_duplicate_or_bound")
        eligible = {row["original_id"] for row in originals if row["outcome"] in {"accept", "partial"}}
        if value["global_blockers"] and (eligible or value["current_publication"] is not None):
            refuse("resolution_global_override")
        from evidence_wiki.pack_discovery import owner
        contract = owner("_strict_contract")
        claims = {}
        for row in originals:
            native_questions = row["eligible_questions"]
            if row["outcome"] not in {"accept", "partial"} and native_questions:
                refuse("resolution_ineligible_content")
            if len({q.get("slug") for q in native_questions}) != len(native_questions):
                refuse("resolution_native_question_duplicate")
            for q in native_questions:
                contract.validate_shape(q, _eligible_shape())
                if q["accepted"] is not True or q["original_id"] != row["original_id"]:
                    refuse("resolution_native_question_invalid")
                claims[q["slug"]] = {claim["id"] for claim in q["claims"]}
            accepted = [q for q in row["questions"] if q["outcome"] == "accept"]
            if {q["slug"] for q in accepted} != {q["slug"] for q in native_questions}:
                refuse("resolution_native_question_projection")
            if row["outcome"] == "accept" and (not row["questions"] or row["missing_question_ids"] or len(accepted) != len(row["questions"])):
                refuse("resolution_original_acceptance_incomplete")
            if row["outcome"] == "partial" and (not accepted or len(accepted) == len(row["questions"]) and not row["missing_question_ids"]):
                refuse("resolution_partial_not_partial")
            for q in row["questions"]:
                if (q["outcome"] == "accept") != (q["current_checks"] is not None):
                    refuse("resolution_current_checks_projection")
                refs = q["eligible_claim_refs"]
                if (len(set(refs)) != len(refs) or q["outcome"] == "accept" and (not refs or not set(refs) <= claims.get(q["slug"], set()))
                        or q["outcome"] != "accept" and refs):
                    refuse("resolution_claim_projection_invalid")
        expected = "accept" if all(row["outcome"] == "accept" for row in originals) else "partial" if eligible else "reject" if all(row["outcome"] == "reject" for row in originals) else "abstain"
        if value["verdict"] != expected:
            refuse("resolution_verdict_not_reproduced")
        return detached(value)
    except UsageError:
        raise
    except (EvidenceWikiError, ValueError, TypeError, KeyError, SystemExit):
        refuse("resolution_document_invalid")


def schemas():
    return {RESOLUTION: deepcopy(schema_document())}
