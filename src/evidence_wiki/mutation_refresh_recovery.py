"""Protected refresh preimages, deterministic publication and finite recovery."""

from __future__ import annotations

from . import refresh_owner as owner
from ._pack_io import json_document, read_file, yaml_document
from .host_context import checked_operation
from .mutation_contracts import INTENT, canonical, checked, detached, digest, refuse
from .mutation_store import MAX_RECORD_BYTES, MAX_STAGE_BYTES

BASIS = "research-mutation-refresh-basis/v1"
STEM, SEAM = "refresh_owner", "apply"


def _intent(value):
    value = checked(INTENT, value)
    if value["operation"] != "questions.refresh":
        refuse("OPERATION_UNSUPPORTED")
    return value


def _basis(root, intent, descriptor):
    return {"schema_version": BASIS, "kind": intent["operation"],
        "configuration_sha256": digest(yaml_document(read_file(root, "research.yml"))),
        "input_sha256": intent["input_sha256"], "plan_blob": descriptor}


def load(root, intent, basis, store):
    intent = _intent(intent)
    if (type(basis) is not dict or set(basis) != {"schema_version", "kind", "configuration_sha256", "input_sha256", "plan_blob"}
            or basis != _basis(root, intent, basis.get("plan_blob"))):
        refuse("INPUT_CONFLICT")
    raw = store.read_stage(basis["plan_blob"])
    if len(raw) > MAX_RECORD_BYTES:
        refuse("LIMIT")
    plan = detached(json_document(raw))
    if (plan.get("schema_version") != owner.PLAN_SCHEMA or plan.get("arguments") != intent["arguments"]
            or plan.get("input_sha256") != intent["input_sha256"]):
        refuse("INPUT_CONFLICT")
    size = 0
    for row in plan["outputs"]:
        row["data"] = store.read_stage(row.pop("blob"))
        size += len(row["data"])
        if row["prior_blob"] is not None:
            prior = store.read_stage(row["prior_blob"])
            if owner.sha(prior) != row["prior_sha256"]:
                refuse("INPUT_CONFLICT")
    if size > MAX_STAGE_BYTES:
        refuse("LIMIT")
    if len(canonical(plan["report"])) > MAX_RECORD_BYTES:
        refuse("LIMIT")
    return plan


def prepare(root, intent, store):
    intent = _intent(intent)
    with checked_operation(STEM, SEAM, (root,), {}) as revalidate:
        store.prepare(intent)
        retained = store.staged("refresh-plan")
        if retained is not None:
            basis = _basis(root, intent, retained)
            load(root, intent, basis, store)
            revalidate()
            return basis
        plan = owner.prepare(root, intent["arguments"], input_sha256=intent["input_sha256"])
        capture = owner.owner("_evidence_revision").capture_workspace(root)
        size = 0
        for index, row in enumerate(plan["outputs"]):
            size += len(row["data"])
            if size > MAX_STAGE_BYTES:
                refuse("LIMIT")
            prior = capture.files.get(row["path"])
            if owner.sha(prior) != row["prior_sha256"]:
                refuse("INPUT_CONFLICT")
            row["prior_blob"] = None if prior is None else store.stage_blob("refresh-before-" + str(index), prior)
            row["blob"] = store.stage_blob("refresh-output-" + str(index), row.pop("data"))
        raw = canonical(plan)
        if len(raw) > MAX_RECORD_BYTES:
            refuse("LIMIT")
        basis = _basis(root, intent, store.stage_blob("refresh-plan", raw))
        load(root, intent, basis, store)
        revalidate()
        return basis


def invoke(root, intent, basis, store):
    with checked_operation(STEM, SEAM, (root,), {}) as revalidate:
        plan = load(root, intent, basis, store)
        revalidate()
        return owner.apply(root, plan=plan)


def prove(root, intent, basis, store):
    with checked_operation(STEM, SEAM, (root,), {}) as revalidate:
        plan = load(root, intent, basis, store)
        capture = owner.verify(root, plan)
        desired = all(capture.files.get(row["path"]) == row["data"] for row in plan["outputs"])
        present = any(owner.sha(capture.files.get(row["path"])) != row["prior_sha256"] for row in plan["outputs"])
        revalidate()
    return {"present": present, "desired": desired, "completion": False,
        "operation_outcome": "desired_state_only" if desired else "original_unknown",
        "observation": {"expected_result_sha256": digest(plan["report"]), "postconditions_proved": desired,
            "current_acceptance": "not_evaluated", "original_execution_timing": "unknown"}}
