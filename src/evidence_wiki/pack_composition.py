"""Compile explicit, conflict-free member contracts to one immutable pack identity."""

from __future__ import annotations

import re
from pathlib import Path

from ._pack_io import canonical, capture_pack, refuse
from .extension_contracts import validate
from .pack_authoring import validate_files
from .pack_discovery import owner

REQUEST = "evidence-pack-composition-request/v1"
PLAN = "evidence-pack-composition-plan/v1"


def request(raw):
    value = validate(raw, REQUEST)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "name", "version", "scope", "members"}
            or value["schema_version"] != REQUEST
            or not isinstance(value["name"], str) or re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value["name"]) is None
            or not isinstance(value["version"], str) or not 1 <= len(value["version"]) <= 64
            or not isinstance(value["scope"], str) or not 1 <= len(value["scope"]) <= 4096
            or not isinstance(value["members"], list) or not 2 <= len(value["members"]) <= 8):
        refuse("composition_request_invalid")
    seen = set()
    for member in value["members"]:
        if (not isinstance(member, dict) or set(member) != {"path", "alias", "applicability"}
                or not all(isinstance(v, str) and 1 <= len(v) <= 4096 for v in member.values())
                or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", member["alias"]) is None or member["alias"] in seen):
            refuse("composition_member_invalid_or_duplicate")
        seen.add(member["alias"])
    return value


def _compile(value):
    sources, bindings = [], []
    for member in value["members"]:
        root = Path(member["path"]).expanduser().absolute()
        captured = capture_pack(root)
        validate_files(root.name, captured.files)
        sources.append({"alias": member["alias"], "applicability": member["applicability"], "files": captured.files})
        bindings.append({"path": str(root), "tree_sha256": captured.tree_sha256})
    try:
        files = owner("_pack_composition").build(value, sources)
        owner("_pack_composition").verify(files)
    except (ValueError, TypeError, KeyError) as error:
        refuse("composition_contract_invalid:" + str(error)[:256], "ONBOARDING_TARGET_CONFLICT")
    validate_files(value["name"], files)
    return files, bindings


def plan(raw):
    value = request(raw)
    files, bindings = _compile(value)
    result = {"schema_version": PLAN, "request": value, "members": bindings,
              "tree_sha256": owner("_pack_composition").tree_id(files), "semantic_adequacy": "not_certified"}
    result["plan_id"] = owner("_pack_revision_impact").digest(result)
    return result


def decode(raw):
    value = validate(raw, PLAN)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "request", "members", "tree_sha256", "semantic_adequacy", "plan_id"}
            or value["schema_version"] != PLAN):
        refuse("composition_plan_shape")
    request(canonical(value["request"]))
    if value["plan_id"] != owner("_pack_revision_impact").digest({k: v for k, v in value.items() if k != "plan_id"}):
        refuse("composition_plan_identity_changed", "ONBOARDING_PLAN_STALE")
    return value


def apply(raw, *, output):
    from .local_artifacts import create

    value = decode(raw)
    current = plan(canonical(value["request"]))
    if current != value:
        refuse("composition_plan_changed", "ONBOARDING_PLAN_STALE")
    target = Path(output).expanduser().absolute()
    for member in current["members"]:
        if target.resolve().is_relative_to(Path(member["path"]).resolve()):
            refuse("composition_output_overlaps_member")
    files, _ = _compile(value["request"])
    name = value["request"]["name"]
    def closing(created):
        if plan(canonical(value["request"])) != value or capture_pack(created / "packs" / name).tree_sha256 != value["tree_sha256"]:
            refuse("composition_inputs_changed", "ONBOARDING_PLAN_STALE")
    root = create(target, {"packs/" + name + "/" + key: raw for key, raw in files.items()},
                  {"schema_version": "evidence-composition-receipt/v1", "plan_id": value["plan_id"], "tree_sha256": value["tree_sha256"]}, closing=closing)
    return {"status": "composed", "candidate": str(Path(root) / "packs" / name), "plan_id": value["plan_id"],
            "tree_sha256": value["tree_sha256"], "semantic_adequacy": "not_certified", "next": "Assess the combined contract, then explicitly migrate or revise a workspace."}
