"""Closed provider-neutral host service reports carry no review authority."""

from copy import deepcopy

from evidence_wiki._pack_io import bounded, canonical
from evidence_wiki.errors import EvidenceWikiError, UsageError
from evidence_wiki.mutation_contracts import detached
from evidence_wiki.onboarding_contract import _matches

STATUS = "research-review-service-status/v1"
RESULT = "research-review-service-result/v1"
MAX_BYTES = 65536


def refuse(reason):
    raise UsageError("REVIEW_SERVICE_REFUSED", "Configured review service operation refused.",
        details={"reason": reason}, recoverable=False)


def schemas():
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    def text(maximum=256):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    def nullable(shape):
        return {"anyOf": [shape, {"type": "null"}]}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    authority = obj(policy_id=text(), policy_revision=text(), content_hash=hashed, expires_at=text(40))
    service = obj(config_ref=text(), slot_ref=nullable(text()), task_role={"enum": ["researcher", "repair", "host_worker", "evaluator", "critic", "adjudicator"]},
        principal=nullable(text()), controller_ref=text(), credential_state={"enum": ["verified", "unavailable"]},
        qualification_state={"enum": ["qualified", "unknown", "not_required"]},
        execution_state={"enum": ["not_observed", "authenticated_owned_execution"]},
        required={"type": "boolean"}, reason=text(128), challenge_ref=nullable(hashed), owned_observation_ref=nullable(hashed))
    status = obj(schema_version={"const": STATUS}, run_id=text(), binding_id=hashed, profile_id=hashed,
        policy_id=hashed, authority_id=nullable(authority), observed_at=text(40), state={"enum": ["ready", "blocked"]}, reason=text(128),
        services={"type": "array", "items": service, "minItems": 1, "maxItems": 9},
        credential_probe={"const": "parent_native_credential_challenge"}, execution_probe={"const": "existing_correlated_owned_attempts_only"},
        enrollment={"const": "not_performed"}, authorized={"const": False}, evidence_accepted={"const": False})
    result = obj(schema_version={"const": RESULT}, request_id=text(), run_id=text(), binding_id=hashed,
        original_id=text(), claim_id=text(), config_ref=text(), attempt_id=nullable(text()),
        state={"enum": ["registered", "blocked", "pending"]}, reason=text(128), review_receipt_ref=nullable(hashed),
        historical={"type": "boolean"}, authorized={"const": False}, evidence_accepted={"const": False})
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
        for name, shape in ((STATUS, status), (RESULT, result))}


def schema_document(identifier):
    if identifier not in schemas():
        refuse("service_schema_unknown")
    return deepcopy(schemas()[identifier])


def validate(identifier, value):
    try:
        bounded(value)
        _matches(value, schema_document(identifier))
        if len(canonical(value)) > MAX_BYTES:
            refuse("service_document_bound")
        if identifier == STATUS:
            required = [row for row in value["services"] if row["required"]]
            generators = [row for row in required if row["qualification_state"] == "not_required"]
            slots = {row["slot_ref"] for row in required if row["slot_ref"] is not None}
            ready = len(generators) == 1 and generators[0]["credential_state"] == "verified" and bool(slots) and all(
                any(row["slot_ref"] == slot and row["credential_state"] == "verified"
                    and row["qualification_state"] == "qualified" for row in required) for slot in slots)
            if value["state"] == "ready" and not ready:
                refuse("service_readiness_not_reproduced")
            if len({row["config_ref"] for row in value["services"]}) != len(value["services"]):
                refuse("service_configuration_duplicate")
        return detached(value)
    except UsageError:
        raise
    except (EvidenceWikiError, ValueError, TypeError, KeyError):
        refuse("service_document_invalid")
