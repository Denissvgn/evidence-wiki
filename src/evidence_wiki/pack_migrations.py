"""Content-bound first-pack and identity migration over the lifecycle owner."""

from __future__ import annotations

import copy
from pathlib import Path

from ._pack_io import canonical, json_document, refuse
from ._script_host import shared_assets_root
from .domain_pack_lifecycle import _validate_candidate
from .errors import EvidenceWikiError, error_from_envelope
from .onboarding_contract import _matches
from .onboarding_schemas import _nullable
from .pack_discovery import owner
from .pack_revisions import _candidate
from .planning_contracts import array, obj, string
from .runtime_identity import generation

REQUEST = "evidence-pack-migration-request/v1"
PLAN = "evidence-pack-migration-plan/v1"


def schemas():
    mapping = {"type": "object", "maxProperties": 256, "additionalProperties": _nullable(string(512))}
    opaque = {"type": "object", "additionalProperties": True}
    request = obj(schema_version={"const": REQUEST}, target=string(4096), path=_nullable(string(4096)),
        catalog=_nullable(string(4096)), revision=_nullable(string(128)), rationale=string(4096),
        keep_local=array(string(512), 256), accept_pack=array(string(512), 256),
        mappings=obj(policies=mapping, request_kinds=mapping, templates=mapping))
    return {REQUEST: request, PLAN: obj(schema_version={"const": PLAN}, request=request, plan_id=string(128),
        candidate_sha256=string(128), qualification=_nullable(opaque), owner_plan=opaque, generation=opaque)}


def decode(raw, schema):
    from .source_commands import _no_secret_values

    value = json_document(raw)
    _matches(value, schemas()[schema])
    _no_secret_values(value)
    if schema == PLAN and (not all(isinstance(value["owner_plan"].get(key), str) for key in ("revision_id", "revision_plan_id"))
            or not isinstance(value["owner_plan"].get("pack"), dict)):
        refuse("migration_owner_plan_invalid")
    return value


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except EvidenceWikiError:
        raise
    except Exception as error:
        if not getattr(error, "error_code", None):
            raise
        raise error_from_envelope({"error_code": error.error_code, "message": str(error),
            "details": getattr(error, "details", {}), "recoverable": getattr(error, "recoverable", False),
            "exit_code": getattr(error, "exit_code", 2)}) from error


def plan(raw):
    request = decode(raw, REQUEST)
    candidate, qualification = _candidate(request)
    lifecycle = owner("_domain_pack_lifecycle")
    identity = _call(_validate_candidate, candidate, assets=shared_assets_root(), lifecycle=lifecycle)
    observed = _call(lifecycle.run_migration, request["target"], candidate, mappings=request["mappings"],
        rationale=request["rationale"], keep_local=request["keep_local"], accept_pack=request["accept_pack"],
        qualification=qualification, candidate_validator=lambda _: identity, dry_run=True)
    value = {"schema_version": PLAN, "request": copy.deepcopy(request), "candidate_sha256": identity,
             "qualification": qualification, "owner_plan": observed, "generation": generation()}
    value["plan_id"] = owner("_pack_revision_impact").digest(value)
    return decode(canonical(value), PLAN)


def apply(raw):
    from .pack_catalog import _outside_assets

    value = decode(raw, PLAN)
    if value["generation"] != generation():
        refuse("migration_generation_changed", "ONBOARDING_PLAN_STALE")
    if owner("_pack_revision_impact").digest({k: v for k, v in value.items() if k != "plan_id"}) != value["plan_id"]:
        refuse("migration_plan_identity_changed", "ONBOARDING_PLAN_STALE")
    request = value["request"]
    target = Path(request["target"]).expanduser().absolute()
    _outside_assets(target, additional_roots=(Path(__file__).parent,))
    candidate, qualification = _candidate(request)
    if qualification != value["qualification"]:
        refuse("migration_qualification_changed", "ONBOARDING_PLAN_STALE")
    lifecycle = owner("_domain_pack_lifecycle")

    def validate(path):
        fingerprint = _call(_validate_candidate, path, assets=shared_assets_root(), lifecycle=lifecycle)
        if fingerprint != value["candidate_sha256"]:
            refuse("migration_candidate_changed", "ONBOARDING_PLAN_STALE")
        return fingerprint

    validate(candidate)
    if not (target / "domain-packs/.evidence-wiki-transaction.yml").exists() and (target / "domain-packs/.evidence-wiki-state.yml").is_file():
        state = _call(lifecycle.load_state, target)
        if state.get("research_revisions", []) and state["research_revisions"][-1]["revision_id"] == value["owner_plan"]["revision_id"]:
            from .pack_revisions import verify_replay

            _call(verify_replay, value)
            return {"status": "already_applied", "revision_id": value["owner_plan"]["revision_id"], "release_accepted": False}
    result = _call(lifecycle.run_migration, target, candidate, mappings=request["mappings"], rationale=request["rationale"],
        keep_local=request["keep_local"], accept_pack=request["accept_pack"], qualification=qualification,
        candidate_validator=validate, expected_plan_id=value["owner_plan"]["revision_plan_id"])
    return {"status": result["status"], "revision_id": result["revision_id"], "migration": result,
            "release_accepted": False, "next": "pack reevaluate each affected question before current review and controlled export"}
