"""Qualified local revision registration and read-only setup-plan resumption."""

from __future__ import annotations

import copy
from pathlib import Path

from ._pack_io import canonical, capture_pack, read_file
from .pack_assessment import assess, references
from .pack_authoring_contracts import ASSESSMENT, decode, digest, refuse
from .pack_authoring_store import load_draft


def revalidate(root, assessment_id, candidate):
    root, actual_candidate, _draft = load_draft(root)
    if actual_candidate.resolve() != candidate.resolve():
        refuse("authoring_catalog_candidate_mismatch")
    expected = decode(read_file(root, "records/assessment-" + assessment_id + ".json"), ASSESSMENT)
    if digest(expected) != assessment_id:
        refuse("authoring_assessment_identity_mismatch")
    observations = canonical(expected["observations"]) if expected["observations"] is not None else None
    observed = assess(root, observations=observations, persist=False)["assessment"]
    if observed != expected:
        refuse("authoring_assessment_stale", "ONBOARDING_PLAN_STALE")
    if expected["gaps"]:
        refuse("authoring_assessment_has_gaps", "ONBOARDING_CHECK_FAILED")
    return expected


def accept(root, *, assessment_id, catalog, root_id, revision, scope):
    from .pack_catalog import _read, _root, register
    from .source_commands import _no_secret_values

    _no_secret_values(scope)
    root, candidate, draft = load_draft(root)
    value = _read(Path(catalog))
    if root_id not in value["roots"]:
        refuse("authoring_catalog_root_unknown")
    relative = candidate.relative_to(_root(value, root_id)).as_posix()
    base = draft["base"]
    derived = None
    if base and base["selection"]["selector"] is not None:
        derived = {"selector": base["selection"]["selector"], "tree_sha256": base["tree_sha256"], "basis": "caller_declared"}
    return register(Path(catalog), revision=revision, root_id=root_id, relative=relative, scope=scope,
        derived_from=derived, authoring_root=root, assessment_id=assessment_id)


def registered_assessment(catalog, row):
    assessment_id = row.get("assessment")
    if assessment_id is None:
        refuse("authoring_catalog_assessment_missing")
    raw = read_file(Path(catalog), "assessment-" + assessment_id + ".json")
    value = decode(raw, ASSESSMENT)
    if digest(value) != assessment_id or value["identity"] != row["identity"] or value["gaps"]:
        refuse("authoring_catalog_assessment_invalid")
    if value["reference_basis"]["sha256"] != references()["sha256"]:
        refuse("authoring_reference_cases_changed", "ONBOARDING_PLAN_STALE")
    return value


def selection(reference, domain=None):
    from .pack_catalog import _read, entries, resolve_entry

    catalog = Path(reference["catalog"]).expanduser().absolute()
    revision = reference["revision"]
    value = _read(catalog)
    if revision not in value["revisions"]:
        refuse("authoring_catalog_revision_unknown")
    record = value["revisions"][revision]
    if record.get("assessment") != reference["assessment_sha256"]:
        refuse("authoring_catalog_assessment_changed", "ONBOARDING_PLAN_STALE")
    rows = entries(catalog, only=revision)
    if len(rows) != 1 or rows[0]["state"] != "available" or rows[0]["validation"]["state"] != "matching_observation":
        refuse("authoring_catalog_revision_stale", "ONBOARDING_PLAN_STALE")
    assessment = registered_assessment(catalog, record)
    candidate = resolve_entry(catalog, revision)
    metadata = rows[0]["metadata"]
    pack = {"name": metadata["name"], "version": metadata["version"], "origin": "caller_local", "locator": str(candidate),
        **record["identity"], "research_contract_version": metadata["compatible_research_yml_contract"]}
    if domain is not None and domain.get("pack") != pack:
        refuse("authoring_selected_pack_binding_mismatch")
    if capture_pack(candidate).tree_sha256 != pack["tree_sha256"]:
        refuse("authoring_selected_pack_changed", "ONBOARDING_PLAN_STALE")
    return pack, {"catalog": str(catalog), "revision": revision, "assessment_sha256": record["assessment"],
        "validation_receipt": record["receipt"], "suite_sha256": assessment["suite_sha256"],
        "semantic_adequacy": "not_certified", "independent_review": "not_verified", "limitations": assessment["limitations"],
        "changes": assessment["changes"]}


def resume(raw, *, catalog, revision):
    from .pack_catalog import _read
    from .planning import compile_plan
    from .planning_contracts import decode as decode_request

    request = copy.deepcopy(decode_request(raw))
    value = _read(Path(catalog))
    record = value["revisions"].get(revision)
    if not record or not record.get("assessment"):
        refuse("authoring_catalog_assessment_missing")
    reference = {"catalog": str(Path(catalog).expanduser().absolute()), "revision": revision, "assessment_sha256": record["assessment"]}
    pack, binding = selection(reference)
    prior = request["request"]["payload"]["domain"]
    request["request"]["payload"]["domain"] = {"mode": "domain_pack", "pack": pack,
        "rationale": "Explicitly selected caller-local revision " + revision + "; domain review remains pending. Previous "
                     + prior["mode"] + " rationale: " + prior["rationale"]}
    request["decisions"]["accepted_pack"] = reference
    request["decisions"]["project_local"] = None
    request["decisions"]["pack_authoring"] = None
    result = compile_plan(canonical(request))
    if result["bindings"]["accepted_pack"] != binding:
        refuse("authoring_resume_binding_changed", "ONBOARDING_PLAN_STALE")
    return result
