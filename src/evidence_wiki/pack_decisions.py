"""Caller-declared domain-fit decisions with canonical pack/profile validation."""

from __future__ import annotations

import copy
import hashlib

from ._pack_io import canonical, json_document, refuse
from .onboarding_contract import _matches
from .pack_discovery import owner, select, validate_snapshot

SCHEMA = "evidence-pack-decision/v1"
CHOICES = ("generic", "reuse", "project_local", "create", "revise", "defer", "partition")


def schemas() -> dict:
    def text(size=1024):
        return {"type": "string", "minLength": 1, "maxLength": size, "pattern": r"\S"}
    def obj(**properties):
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    def array(shape, maximum=64, minimum=0):
        return {"type": "array", "items": shape, "maxItems": maximum, "minItems": minimum}
    ident = {**text(64), "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"}
    hashed = {"type": "string", "pattern": r"^[a-f0-9]{64}$"}
    choice = {"enum": list(CHOICES)}
    selector = {**text(256), "pattern": r"^(bundled|installed|local):[^:]+$"}
    selected = obj(selector=selector, tree_sha256=hashed)
    scope = {"type": "object", "maxProperties": 32, "additionalProperties": text()}
    guidance = {"type": "object", "maxProperties": 16, "additionalProperties": True,
                "description": "The initializer owns the complete project-local guidance contract."}
    decision = obj(
        schema_version={"const": SCHEMA}, request_id=ident, choice=choice, rationale=text(4096),
        requirements=array(obj(id=ident, text=text(), kind={"enum": ["evidence", "scope", "review", "output"]}), minimum=1),
        selections=array(selected, 8), scope_inputs=scope,
        mapping=array(obj(requirement_id=ident, support={"enum": ["supported", "partial", "gap", "unknown"]},
                          pack_basis=array(obj(selector=selector, pointer=text(256)), 8), rationale=text())),
        alternatives=array(obj(choice=choice, rationale=text()), 8, 1),
        gaps=array(obj(requirement_id=ident, kind={"enum": ["guidance", "source", "adapter", "scope", "review"]}, detail=text())),
        unresolved_scope=array(text(), 32), local_guidance={"anyOf": [guidance, {"type": "null"}]},
        partitions=array(obj(id=ident, requirement_ids=array(ident, minimum=1), rationale=text(), scope_inputs=scope,
                             selection={"anyOf": [selected, {"type": "null"}]}), 8),
    )
    selection = obj(schema_version={"const": "1.0"}, typical_questions=array(text(), 32), exclusions=array(text(), 32),
                    required_scope_inputs=array(obj(id={**text(64), "pattern": r"^[a-z][a-z0-9_-]{0,63}$"}, description=text()), 32), review_requirements=array(text(), 32))
    selection["required"] = ["schema_version"]
    identity = obj(tree_sha256=hashed, overlay_sha256=hashed)
    derivation = obj(selector=selector, tree_sha256=hashed, basis={"const": "caller_declared"})
    catalog = obj(schema_version={"const": "evidence-pack-catalog/v1"},
        roots={"type": "object", "minProperties": 1, "maxProperties": 8, "additionalProperties": obj(
            path=text(4096), identity=obj(device=text(64), inode=text(64)))},
        revisions={"type": "object", "maxProperties": 32, "additionalProperties": obj(
            root_id=ident, path=text(512), name=text(128), version=text(128), identity=identity, scope=text(),
            derived_from={"anyOf": [derivation, {"type": "null"}]}, receipt=hashed)})
    receipt = obj(schema_version={"const": "evidence-pack-validation/v1"}, tree_sha256=hashed, overlay_sha256=hashed,
                  checker_sha256=hashed, ok={"const": True}, checks=array(obj(id=text(128), status={"const": "pass"}), 128, 1),
                  authority={"const": "caller_local_structural_observation"}, semantic_adequacy={"const": "not_evaluated"})
    result = obj(schema_version={"const": "evidence-pack-decision-result/v1"}, request_id=ident, decision_sha256=hashed,
                 decision=decision, choice=choice, status={"enum": ["valid", "partial", "needs_scope", "proposed", "deferred", "unsupported"]},
                 mapping_assurance={"const": "caller_declared"}, semantic_adequacy={"const": "not_evaluated"},
                 selected_revisions=array(obj(selector=selector, identity=identity, validation_receipt=hashed,
                     human_gated={"type": "boolean"}, human_review_policies=array(text()),
                     review_requirements={"anyOf": [array(text(), 32), {"type": "null"}]}), 8),
                 missing_scope=array(obj(selector=selector, partition={"anyOf": [ident, {"type": "null"}]}, id=text(64)), 256),
                 gaps=decision["properties"]["gaps"], alternatives=decision["properties"]["alternatives"],
                 providers_enabled={"const": False}, research_ready={"const": False}, evidence_accepted={"const": False})
    result["properties"].update(reason=text(), supported_alternatives=array(text(), 8),
        partitions=decision["properties"]["partitions"], recommended_choice={"const": "defer"},
        unknown_selection_fields={"type": "object", "maxProperties": 8, "additionalProperties": array(text(), 4)},
        unresolved_scope=decision["properties"]["unresolved_scope"])
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape} for key, shape in (
        (SCHEMA, decision), ("evidence-pack-selection-metadata/v1", selection),
        ("evidence-pack-catalog/v1", catalog), ("evidence-pack-validation/v1", receipt),
        ("evidence-pack-decision-result/v1", result))}


def schema_document(resource_id: str) -> dict:
    if not isinstance(resource_id, str) or resource_id not in schemas():
        refuse("pack_schema_unknown", "ONBOARDING_RESOURCE_UNKNOWN")
    return copy.deepcopy(schemas()[resource_id])


def _result(value):
    _matches(value, schema_document("evidence-pack-decision-result/v1"))
    return value


def _pointer(document, pointer):
    if not pointer.startswith("/") or "~" in pointer.replace("~0", "").replace("~1", ""):
        refuse("pack_basis_pointer_invalid")
    current = document
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, list):
                if not part.isdigit() or str(int(part)) != part:
                    refuse("pack_basis_pointer_invalid")
                current = current[int(part)]
            else:
                current = current[part]
    except (KeyError, IndexError, ValueError, TypeError):
        refuse("pack_basis_pointer_unresolved")
    if current is None:
        refuse("pack_basis_unknown")


def decide(raw: bytes, *, target=None, catalog=None) -> dict:
    value = json_document(raw)
    _matches(value, schema_document(SCHEMA))
    requirements = [row["id"] for row in value["requirements"]]
    if len(set(requirements)) != len(requirements):
        refuse("pack_requirement_ids_duplicate")
    mapping = value["mapping"]
    if len(mapping) != len(requirements) or {row["requirement_id"] for row in mapping} != set(requirements):
        refuse("pack_requirement_mapping_incomplete")
    if any(row["requirement_id"] not in requirements for row in value["gaps"]):
        refuse("pack_gap_requirement_unknown")
    result = {"schema_version": "evidence-pack-decision-result/v1", "request_id": value["request_id"],
              "decision_sha256": hashlib.sha256(canonical(value)).hexdigest(), "choice": value["choice"],
              "decision": value,
              "status": "valid", "mapping_assurance": "caller_declared", "semantic_adequacy": "not_evaluated",
              "selected_revisions": [], "missing_scope": [], "gaps": value["gaps"], "alternatives": value["alternatives"],
              "providers_enabled": False, "research_ready": False, "evidence_accepted": False}
    if len(value["selections"]) > 1:
        result.update(status="unsupported", reason="multi_pack_composition_unavailable",
                      supported_alternatives=["partition", "project_local", "reuse_one_pack"])
        return _result(result)
    selected = value["selections"]
    choice = value["choice"]
    if choice in {"reuse", "revise"} and len(selected) != 1:
        refuse("pack_choice_requires_one_revision")
    if choice not in {"reuse", "revise"} and selected:
        refuse("pack_choice_cannot_select_revision")
    if choice == "partition":
        partitions = value["partitions"]
        members = [member for part in partitions for member in part["requirement_ids"]]
        if (len(partitions) < 2 or len({part["id"] for part in partitions}) != len(partitions)
                or sorted(members) != sorted(requirements)):
            refuse("pack_partition_coverage_invalid")
        selected = [part["selection"] for part in partitions if part["selection"] is not None]
        result.update(status="proposed", partitions=partitions)
    elif value["partitions"]:
        refuse("pack_partitions_require_partition_choice")
    if choice in {"create", "revise"} and not any(gap["kind"] == "guidance" for gap in value["gaps"]):
        result.update(status="unsupported", reason="guidance_change_does_not_supply_missing_evidence",
                      supported_alternatives=["retain_guidance_and_acquire_sources", "supply_adapter", "defer"])
        return _result(result)
    metadata = {}
    pairs = [(reference, value["scope_inputs"], None) for reference in selected]
    if choice == "partition":
        pairs = [(part["selection"], part["scope_inputs"], part["id"]) for part in value["partitions"] if part["selection"] is not None]
    for reference, scopes, partition_id in pairs:
        row, path = select(reference["selector"], target=target, catalog=catalog)
        if row["state"] not in {"available"}:
            refuse("pack_selected_revision_unavailable")
        if row["origin"] == "installed" and row["lifecycle"]["state"] not in {"current", "local_modifications", "legacy_untracked"}:
            refuse("pack_installed_state_unusable")
        info, receipt = validate_snapshot(path, reference["tree_sha256"])
        if not info["compatible"]:
            refuse("pack_contract_incompatible")
        metadata[reference["selector"]] = info
        result["selected_revisions"].append({"selector": reference["selector"], "identity": info["identity"],
                                             "validation_receipt": hashlib.sha256(canonical(receipt)).hexdigest(),
                                             "human_gated": info["human_gated"], "human_review_policies": info["human_review_policies"],
                                             "review_requirements": info["selection"]["review_requirements"]})
        missing = [item["id"] for item in info["selection"]["required_scope_inputs"] or [] if item["id"] not in scopes]
        result["missing_scope"].extend({"selector": reference["selector"], "partition": partition_id, "id": item} for item in missing)
        if info["selection"]["unknown_fields"]:
            result.setdefault("unknown_selection_fields", {})[reference["selector"]] = info["selection"]["unknown_fields"]
    for row in mapping:
        partition = next((part for part in value["partitions"] if row["requirement_id"] in part["requirement_ids"]), None)
        needs_basis = choice in {"reuse", "revise"} or partition is not None and partition["selection"] is not None
        if needs_basis and row["support"] == "supported" and not row["pack_basis"]:
            refuse("pack_supported_mapping_requires_basis")
        for basis in row["pack_basis"]:
            if basis["selector"] not in metadata:
                refuse("pack_mapping_selector_unknown")
            if choice == "partition":
                if partition["selection"] is None or partition["selection"]["selector"] != basis["selector"]:
                    refuse("pack_partition_basis_mismatch")
            _pointer(metadata[basis["selector"]], basis["pointer"])
    initializer = owner("init_research_workspace")
    if choice == "project_local":
        guidance = value["local_guidance"]
        if not isinstance(guidance, dict) or guidance.get("mode") != "project_local":
            refuse("pack_local_guidance_required")
        profile = {"domain_pack": {"enabled": False}, "domain_guidance": guidance}
    else:
        if value["local_guidance"] is not None:
            refuse("pack_local_guidance_cannot_mix_with_pack")
        mode = "domain_pack" if choice in {"reuse", "revise"} else "none" if choice == "generic" else "deferred"
        profile = {"domain_guidance": {"mode": mode, "rationale": value["rationale"]},
                   "domain_pack": {"path": selected[0]["selector"]} if mode == "domain_pack" else {"enabled": False}}
    try:
        initializer.validate_domain_decision(profile)
    except SystemExit:
        refuse("pack_guidance_profile_incompatible")
    if value["unresolved_scope"] or result["missing_scope"]:
        result.update(status="needs_scope", recommended_choice="defer")
    elif choice == "defer":
        result["status"] = "deferred"
    elif any(row["support"] in {"partial", "gap", "unknown"} for row in mapping):
        result["status"] = "partial"
    result["unresolved_scope"] = value["unresolved_scope"]
    return _result(result)
