"""Inert bounded material diffs; native owners retain all acceptance authority."""

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.execution_disagreement import validate_targeted_repair
from evidence_wiki.mutation_contracts import canonical, detached, digest
from evidence_wiki.pack_discovery import owner

DIFF = "research-repair-diff/v1"
PROGRESS = "research-repair-progress/v1"
_FIELDS = ("qualification", "text", "scope", "time", "units", "evidence", "premises", "derivation", "limitations", "calculations")


def fail(reason):
    raise ExecutionError("repair_progress_" + reason)


def inventory_diff(before, after, *, target_claim_id):
    """Compare native inventories without exposing historical source quotations.

    Field commitments identify the exact before/after values. They are change
    evidence, rather than semantic progress or proof of currently permitted use.
    """
    if len(canonical(before)) > 1_048_576 or len(canonical(after)) > 1_048_576:
        fail("inventory_bound")
    contract = owner("_strict_contract")
    before, after = contract.claims_document(detached(before)), contract.claims_document(detached(after))
    if before["schema_version"] != after["schema_version"] or before["questions"] != after["questions"]:
        fail("original_scope_changed")
    validate_targeted_repair(before["claims"], after["claims"], target_claim_id=target_claim_id)
    original = next(row for row in before["claims"] if row["id"] == target_claim_id)
    revised = next(row for row in after["claims"] if row["id"] == target_claim_id)
    changes = [{"field": key, "before_sha256": digest(original.get(key)), "after_sha256": digest(revised.get(key))}
        for key in _FIELDS if original.get(key) != revised.get(key)]
    return {"schema_version": DIFF, "target_claim_id": target_claim_id,
        "before_inventory_sha256": digest(before), "after_inventory_sha256": digest(after),
        "changes": changes, "claim_bytes_changed": original != revised,
        "semantic_progress": "not_established", "current_approval": "not_established",
        "peer_claims_preserved": True, "original_scope_preserved": True,
        "authorized": False, "evidence_accepted": False}


def schemas(native=None):
    def obj(**properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    text = {"type": "string", "minLength": 1, "maxLength": 256}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$", "minLength": 71, "maxLength": 71}
    nullable_hash = {"anyOf": [hashed, {"type": "null"}]}
    flags = {"authorized": {"const": False}, "evidence_accepted": {"const": False}}
    diff = obj(schema_version={"const": DIFF}, target_claim_id=text, before_inventory_sha256=hashed,
        after_inventory_sha256=hashed, changes={"type": "array", "maxItems": len(_FIELDS),
        "items": obj(field={"enum": list(_FIELDS)}, before_sha256=hashed, after_sha256=hashed)},
        claim_bytes_changed={"type": "boolean"}, semantic_progress={"const": "not_established"},
        current_approval={"const": "not_established"}, peer_claims_preserved={"const": True},
        original_scope_preserved={"const": True}, **flags)
    if native is None:
        native = owner("_strict_contract").schema_documents()
    native_review = native["evidence-strict-review-status/v1"]["properties"]["claims"]["items"]["properties"]["review"]
    progress = obj(schema_version={"const": PROGRESS}, run_id=text, current_basis_id=nullable_hash,
        original_id={"anyOf": [text, {"type": "null"}]}, after_sequence={"type": "integer", "minimum": 0},
        next_sequence={"type": "integer", "minimum": 0}, truncated={"type": "boolean"},
        repairs_used={"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
        selected_repair_cycles={"type": "integer", "minimum": 0}, repair_limit={"type": "integer", "minimum": 0},
        claim_repair_limit={"const": 2},
        rows={"type": "array", "maxItems": 32, "items": obj(attempt_id=text, original_id=text, claim_id=text,
            state={"enum": ["reserved", "observed", "application_retained", "applied"]},
            before_material_basis_id=nullable_hash, after_material_basis_id=nullable_hash,
            material_change={"enum": ["changed", "unchanged", "unknown"]},
            diff={"anyOf": [diff, {"type": "null"}]}, current_review={"anyOf": [obj(availability={"const": "current"}, basis_id=hashed,
                native_accepted={"type": "boolean"}, selection=native_review),
                obj(availability={"const": "unavailable"}, basis_id={"type": "null"}, native_accepted={"type": "null"},
                    selection={"type": "null"}, reason={"const": "current_owner_unavailable"})]},
            historical_effect_recorded={"type": "boolean"}, execution_correlation={"enum": ["known", "unknown"]},
            fresh_independent_reviews={"enum": ["complete", "incomplete", "unknown"]})}, **flags)
    return {DIFF: diff, PROGRESS: progress}


def schema_document(identifier, native=None):
    documents = schemas(native)
    if identifier not in documents:
        fail("schema_unknown")
    return detached(documents[identifier])
