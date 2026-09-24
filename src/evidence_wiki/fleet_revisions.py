"""Explicit, bounded revision proposals with independent workspace outcomes."""

from __future__ import annotations

import copy
import os
from pathlib import Path

from ._pack_io import canonical, refuse
from .extension_contracts import validate
from .pack_discovery import owner
from .pack_revisions import _candidate
from .pack_revisions import apply as apply_revision
from .pack_revisions import plan as plan_revision

REQUEST = "evidence-fleet-revision-request/v1"
PLAN = "evidence-fleet-revision-plan/v1"
APPLY = "evidence-fleet-revision-apply/v1"


def _error(error):
    return {"error_code": getattr(error, "error_code", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE"),
            "details": getattr(error, "details", {"field": "workspace_or_candidate_unavailable"}),
            "recoverable": getattr(error, "recoverable", False)}


def request(raw):
    from .pack_revision_contracts import REQUEST as REVISION_REQUEST
    from .pack_revision_contracts import decode

    value = validate(raw, REQUEST)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "candidate", "rationale", "workspaces"}
            or value["schema_version"] != REQUEST or not isinstance(value["candidate"], dict)
            or set(value["candidate"]) != {"path", "catalog", "revision"}
            or not isinstance(value["rationale"], str) or not value["rationale"].strip()
            or not isinstance(value["workspaces"], list) or not 1 <= len(value["workspaces"]) <= 16):
        refuse("fleet_request_invalid")
    for selected in value["workspaces"]:
        if not isinstance(selected, dict) or set(selected) != {"target", "keep_local", "accept_pack"} or not isinstance(selected["target"], str):
            refuse("fleet_workspace_selection_invalid")
        decode(canonical({"schema_version": REVISION_REQUEST, **value["candidate"], **selected,
                          "rationale": value["rationale"]}), REVISION_REQUEST)
    return value


def decode(raw):
    from .pack_revision_contracts import PLAN as REVISION_PLAN
    from .pack_revision_contracts import decode as revision_document

    value = validate(raw, APPLY)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "plan", "targets"}
            or value["schema_version"] != APPLY or not isinstance(value["plan"], dict)
            or not isinstance(value["targets"], list) or not 1 <= len(value["targets"]) <= 16
            or any(not isinstance(item, str) for item in value["targets"])
            or len(set(value["targets"])) != len(value["targets"])):
        refuse("fleet_apply_requires_explicit_unique_targets")
    prepared = value["plan"]
    if (set(prepared) != {"schema_version", "request", "proposals", "bounds", "auto_propagation", "plan_id"}
            or prepared["schema_version"] != PLAN or not isinstance(prepared["proposals"], list)
            or prepared["plan_id"] != owner("_pack_revision_impact").digest({k: v for k, v in prepared.items() if k != "plan_id"})):
        refuse("fleet_plan_changed", "ONBOARDING_PLAN_STALE")
    selected = request(canonical(prepared["request"]))
    if len(prepared["proposals"]) != len(selected["workspaces"]):
        refuse("fleet_proposal_count_changed")
    targets = set()
    for row, workspace in zip(prepared["proposals"], selected["workspaces"], strict=True):
        if (not isinstance(row, dict) or set(row) != {"target", "status", "plan", "error", "candidate_available",
                "installed_consistency", "semantic_applicability"}
                or row["target"] != os.path.abspath(os.path.expanduser(workspace["target"]))
                or row["target"] in targets or row["status"] not in {"proposed", "blocked"}):
            refuse("fleet_proposal_shape_or_target_changed")
        targets.add(row["target"])
        if row["status"] == "proposed":
            child = revision_document(canonical(row["plan"]), REVISION_PLAN)
            expected = {"schema_version": "evidence-pack-revision-request/v1", **selected["candidate"],
                        **workspace, "target": row["target"], "rationale": selected["rationale"]}
            if child["request"] != expected or row["error"] is not None:
                refuse("fleet_embedded_request_changed")
        elif row["plan"] is not None or not isinstance(row["error"], dict):
            refuse("fleet_blocked_proposal_invalid")
    if not set(value["targets"]) <= targets:
        refuse("fleet_apply_target_not_proposed")
    return value


def plan(raw):
    value = request(raw)
    roots, identities, requests = [], set(), []
    for selected in value["workspaces"]:
        path = Path(selected["target"]).expanduser().absolute()
        if path.is_symlink():
            refuse("fleet_workspace_link")
        root = path.resolve()
        if any(root.is_relative_to(prior) or prior.is_relative_to(root) for prior in roots):
            refuse("fleet_workspace_overlap")
        if root.exists():
            observed = root.stat()
            identity = (observed.st_dev, observed.st_ino)
            if identity in identities:
                refuse("fleet_workspace_alias")
            identities.add(identity)
        roots.append(root)
        requests.append({"schema_version": "evidence-pack-revision-request/v1", **value["candidate"],
            **selected, "target": str(root), "rationale": value["rationale"]})
    candidate, _ = _candidate(requests[0])
    if any(candidate.resolve().is_relative_to(root) or root.is_relative_to(candidate.resolve()) for root in roots):
        refuse("fleet_candidate_overlaps_workspace")
    rows = []
    for revision_request in requests:
        try:
            proposed = plan_revision(revision_request)
            row = {"target": revision_request["target"], "status": "proposed", "plan": proposed, "error": None,
                   "candidate_available": True, "installed_consistency": "owner_preconditions_passed"}
        except (Exception, SystemExit) as error:
            row = {"target": revision_request["target"], "status": "blocked", "plan": None, "error": _error(error),
                   "candidate_available": None, "installed_consistency": "not_established"}
        row["semantic_applicability"] = "not_established"
        rows.append(row)
    value = copy.deepcopy(value)
    for selected, root in zip(value["workspaces"], roots, strict=True):
        selected["target"] = str(root)
    result = {"schema_version": PLAN, "request": value, "proposals": rows,
              "bounds": {"selected": len(rows), "maximum": 16, "truncated": False}, "auto_propagation": False}
    result["plan_id"] = owner("_pack_revision_impact").digest(result)
    if len(canonical(result)) > 1_048_576:
        refuse("fleet_output_bound_partition_selection", "ONBOARDING_LIMIT")
    return result


def apply(raw):
    value = decode(raw)
    prepared = value["plan"]
    if (prepared.get("schema_version") != PLAN or prepared.get("plan_id") != owner("_pack_revision_impact").digest(
            {key: item for key, item in prepared.items() if key != "plan_id"})):
        refuse("fleet_plan_changed", "ONBOARDING_PLAN_STALE")
    proposals = {row["target"]: row for row in prepared.get("proposals", [])}
    if not set(value["targets"]) <= set(proposals):
        refuse("fleet_apply_target_not_proposed")
    rows = []
    for target in value["targets"]:
        proposal = proposals[target]
        if proposal["status"] != "proposed":
            rows.append({"target": target, "status": "blocked", "error": proposal["error"], "result": None})
            continue
        try:
            if os.path.abspath(target) != proposal["plan"]["request"]["target"]:
                refuse("fleet_embedded_target_changed")
            result = apply_revision(canonical(proposal["plan"]))
            rows.append({"target": target, "status": result["status"], "result": result, "error": None})
        except (Exception, SystemExit) as error:
            rows.append({"target": target, "status": "failed", "error": _error(error), "result": None})
    return {"schema_version": "evidence-fleet-revision-result/v1", "plan_id": prepared["plan_id"], "workspaces": rows,
        "status": "complete" if all(row["error"] is None for row in rows) else "partial",
        "recovery": "Retry the same selected plans; successful owners reconcile their own revision receipts.",
        "semantic_applicability": "not_certified", "release_accepted": False}
