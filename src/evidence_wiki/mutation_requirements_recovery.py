"""Canonical staged append-only requirements effects and finite recovery proof."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ._pack_io import json_document, relative_path, yaml_document
from .host_context import checked_operation
from .mutation_contracts import INTENT, RECEIPT, STATE, canonical, checked, detached, digest, refuse
from .mutation_store import MAX_RECORD_BYTES, MAX_STAGE_BYTES
from .pack_discovery import owner
from .requirements_operations import _hash, _host, _prepare_effects, expected_result, revise
from .requirements_projection import DOCUMENT, validated_lineage

BASIS = "research-mutation-requirements-basis/v1"
CLOCK = "research-requirements-clock/v1"
STEM, SEAM = "requirements_operations", "revise"


def _intent(value):
    value = checked(INTENT, value)
    if value["operation"] != "requirements.revise":
        refuse("OPERATION_UNSUPPORTED", "/operation")
    return value


def _json(store, descriptor):
    raw = store.read_stage(descriptor)
    if len(raw) > MAX_RECORD_BYTES:
        refuse("LIMIT", "/owner_plan")
    return json_document(raw)


def _basis(intent, descriptor, configuration_sha256):
    return {"schema_version": BASIS, "kind": "requirements.revise", "input_sha256": intent["input_sha256"], "configuration_sha256": configuration_sha256, "plan": descriptor}


def _clock(intent, store):
    state = store.read()
    if state is None or state["status"] != "prepared":
        refuse("INPUT_CONFLICT", "/owner_state")
    marker = state["owner_basis"]
    if marker is None or type(marker) is dict and set(marker) == {"kind", "configuration_sha256"} and marker["kind"] == "requirements.revise":
        marker = {"schema_version": BASIS, "preparing": True, "input_sha256": intent["input_sha256"],
                  "observed_utc": datetime.now(timezone.utc).isoformat()}
        state["owner_basis"] = marker
        store.write(checked(STATE, state), expected_sha256=store.state_sha256())
    if (type(marker) is not dict or set(marker) != {"schema_version", "preparing", "input_sha256", "observed_utc"}
            or marker["schema_version"] != BASIS or marker["preparing"] is not True or marker["input_sha256"] != intent["input_sha256"]):
        refuse("INPUT_CONFLICT", "/owner_clock")
    return marker["observed_utc"]


def load_plan(root, intent, basis, store, *, authenticate_host=True):
    """Read only protected immutable staging bound to the actual owning intent."""
    intent = _intent(intent)
    if (type(basis) is not dict or set(basis) != {"schema_version", "kind", "input_sha256", "configuration_sha256", "plan"}
            or basis != _basis(intent, basis.get("plan"), basis.get("configuration_sha256"))):
        refuse("INPUT_CONFLICT", "/owner_basis")
    plan = _json(store, basis["plan"])
    expected_keys = {"schema_version", "request_id", "before_revision", "proposal_sha256", "envelope_sha256", "workspace",
        "input_sha256", "workspace_binding", "owner_ref", "original_binding", "configuration_sha256", "observed_utc", "decision", "document_sha256", "dependencies",
        "outputs", "directories", "new_directories", "receipt_required", "eligibility"}
    if (type(plan) is not dict or set(plan) != expected_keys or plan["schema_version"] != "research-requirements-effect-plan/v1"
            or plan["input_sha256"] != intent["input_sha256"] or plan["request_id"] != intent["request_id"]
            or plan["workspace_binding"] != intent["workspace_binding"] or plan["workspace"] != str(Path(root).resolve())
            or plan["before_revision"] != intent["expected_revision"]
            or plan["proposal_sha256"] != digest(intent["arguments"]["proposal"])
            or plan["envelope_sha256"] != digest(intent["arguments"]["envelope"])
            or plan["receipt_required"] is not True or plan["configuration_sha256"] != basis["configuration_sha256"]):
        refuse("INPUT_CONFLICT", "/owner_plan")
    binding = {key: plan[key] for key in ("request_id", "before_revision", "proposal_sha256", "envelope_sha256", "workspace", "input_sha256", "workspace_binding")}
    if plan["owner_ref"] != digest({"schema_version": "research-requirements-owner-ref/v1", **binding}):
        refuse("INPUT_CONFLICT", "/owner_ref")
    if authenticate_host:
        _host(root, intent["arguments"]["envelope"])
    if type(plan["dependencies"]) is not list or len(plan["dependencies"]) > 10000:
        refuse("LIMIT", "/owner_dependencies")
    dependency_paths = []
    for row in plan["dependencies"]:
        if (type(row) is not dict or set(row) != {"path", "sha256"} or type(row["sha256"]) is not str
                or len(row["sha256"]) != 71 or not row["sha256"].startswith("sha256:")):
            refuse("INPUT_CONFLICT", "/owner_dependencies")
        relative_path(row["path"])
        dependency_paths.append(row["path"])
    if dependency_paths != sorted(set(dependency_paths)):
        refuse("INPUT_CONFLICT", "/owner_dependencies")
    if type(plan["outputs"]) is not list or not 1 <= len(plan["outputs"]) <= 404:
        refuse("LIMIT", "/owner_outputs")
    paths = []
    for row in plan["outputs"]:
        if type(row) is not dict or set(row) != {"path", "prior_sha256", "blob"}:
            refuse("INPUT_CONFLICT", "/owner_outputs")
        relative_path(row["path"])
        paths.append(row["path"])
        row["data"] = store.read_stage(row.pop("blob"))
    if len(set(paths)) != len(paths) or paths[-1] != "docs/research-requirements.json":
        refuse("INPUT_CONFLICT", "/owner_outputs")
    document = json_document(plan["outputs"][-1]["data"])
    lineage = validated_lineage(document)
    if (document.get("schema_version") != DOCUMENT or not lineage["revisions"]
            or lineage["revisions"][-1]["revision_id"] != intent["request_id"]
            or lineage["revisions"][-1]["owner_receipt_ref"] != plan["owner_ref"]
            or lineage["revisions"][-1]["proposal"] != intent["arguments"]["proposal"]
            or lineage["revisions"][-1]["envelope"] != intent["arguments"]["envelope"]
            or lineage["revisions"][-1]["decision"] != plan["decision"]
            or _hash(plan["outputs"][-1]["data"]) != plan["document_sha256"]):
        refuse("INPUT_CONFLICT", "/owner_document")
    return plan


def _quota(intent, basis, result):
    common = {"request_id": intent["request_id"], "input_sha256": intent["input_sha256"], "before_revision": intent["expected_revision"],
              "after_revision": intent["expected_revision"], "result": result, "error": None}
    candidates = [checked(STATE, {"schema_version": STATE, **common, "status": "owner_completed", "owner_basis": basis, "reconciliation": None}),
        checked(RECEIPT, {"schema_version": RECEIPT, **common, "operation": "requirements.revise", "status": "committed",
            "historical": True, "authorized": False, "evidence_accepted": False})]
    if any(len(canonical(value)) > MAX_RECORD_BYTES for value in candidates):
        refuse("LIMIT", "/owner_result")


def prepare(root, intent, store):
    intent = _intent(intent)
    root = Path(root).resolve()
    with checked_operation(STEM, SEAM, (root,), {}) as revalidate:
        _host(root, intent["arguments"]["envelope"])
        store.prepare(intent)
        retained = store.staged("requirements-plan")
        if retained is not None:
            basis = _basis(intent, retained, digest(yaml_document(owner("_evidence_revision").capture_workspace(root).files["research.yml"])))
            plan = load_plan(root, intent, basis, store)
            _quota(intent, basis, expected_result(plan))
            revalidate()
            return basis
        observed = _clock(intent, store)
        plan = _prepare_effects(root, **intent["arguments"], request_id=intent["request_id"],
            expected_revision=intent["expected_revision"], observed_utc=observed,
            input_sha256=intent["input_sha256"], workspace_binding=intent["workspace_binding"])
        capture = owner("_evidence_revision").capture_workspace(root)
        files = {path: len(raw) for path, raw in capture.files.items()}
        files.update({row["path"]: len(row["data"]) for row in plan["outputs"]})
        if len(files) > 10000 or sum(files.values()) > MAX_STAGE_BYTES:
            refuse("LIMIT", "/owner_tree")
        result = expected_result(plan)
        for index, row in enumerate(plan["outputs"]):
            row["blob"] = store.stage_blob(f"requirements-output-{index}", row.pop("data"))
        raw = canonical(detached(plan))
        if len(raw) > MAX_RECORD_BYTES:
            refuse("LIMIT", "/owner_plan")
        basis = _basis(intent, store.stage_blob("requirements-plan", raw), plan["configuration_sha256"])
        _quota(intent, basis, result)
        revalidate()
        return basis


def invoke(root, intent, basis, store):
    intent = _intent(intent)
    with checked_operation(STEM, SEAM, (root,), {}) as revalidate:
        load_plan(root, intent, basis, store)
        revalidate()
        return revise(root, **intent["arguments"], _store=store, _basis=basis)


def prove(root, intent, basis, store):
    intent = _intent(intent)
    with checked_operation(STEM, SEAM, (root,), {}) as revalidate:
        plan = load_plan(root, intent, basis, store)
        capture = owner("_evidence_revision").capture_workspace(Path(root))
        matched = all(capture.files.get(row["path"]) == row["data"] for row in plan["outputs"])
        dependencies = all(_hash(capture.files.get(row["path"])) == row["sha256"] for row in plan["dependencies"])
        known = {row["path"] for row in plan["dependencies"]} | {row["path"] for row in plan["outputs"]}
        desired = matched and dependencies and set(capture.files) <= known
        present = any(_hash(capture.files.get(row["path"])) != row["prior_sha256"] for row in plan["outputs"])
        revalidate()
    return {"present": present, "desired": desired, "completion": False,
        "operation_outcome": "desired_state_only" if desired else "original_unknown",
        "observation": {"expected_result_sha256": digest(expected_result(plan)), "document_matched": capture.files.get(plan["outputs"][-1]["path"]) == plan["outputs"][-1]["data"],
            "outputs_matched": matched, "dependencies_unchanged": dependencies, "postconditions_proved": desired,
            "original_execution_timing": "unknown", "current_acceptance": "not_evaluated"}}
