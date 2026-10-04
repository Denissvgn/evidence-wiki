"""Sealed native immutable capture effects and explicit finite pair recovery."""

from __future__ import annotations

from pathlib import Path

from ._pack_io import json_document
from .host_context import current_context
from .mutation_contracts import INTENT, RECONCILIATION, canonical, checked, digest, refuse
from .mutation_store import MAX_RECORD_BYTES, MutationStore
from .source_capture_contracts import metadata, validate
from .source_capture_operations import (
    OPERATION,
    REPAIR_GRANT,
    _check_tree,
    _host,
    deliver,
    expected_result,
    file_hash,
    prepare_effects,
    provenance,
)

BASIS = "research-mutation-capture-basis/v1"


def _intent(intent):
    intent = checked(INTENT, intent)
    if intent["operation"] != OPERATION:
        refuse("OPERATION_UNSUPPORTED", "/operation")
    return intent


def _basis(intent, descriptor, configuration):
    return {"schema_version": BASIS, "kind": OPERATION, "input_sha256": intent["input_sha256"],
        "configuration_sha256": configuration, "plan": descriptor}


def load_plan(root, intent, basis, store):
    intent = _intent(intent)
    if (type(basis) is not dict or set(basis) != {"schema_version", "kind", "input_sha256", "configuration_sha256", "plan"}
            or basis != _basis(intent, basis.get("plan"), basis.get("configuration_sha256"))):
        refuse("INPUT_CONFLICT", "/capture/basis")
    plan = json_document(store.read_stage(basis["plan"]))
    keys = {"schema_version", "request_id", "input_sha256", "workspace_binding", "configuration_sha256", "delivery_sha256", "path",
        "outputs", "dependencies", "directories", "new_directories", "result"}
    if (type(plan) is not dict or set(plan) != keys or plan["schema_version"] != "research-capture-effect-plan/v1"
            or plan["request_id"] != intent["request_id"] or plan["input_sha256"] != intent["input_sha256"]
            or plan["workspace_binding"] != intent["workspace_binding"] or plan["workspace_binding"]["path"] != str(Path(root).resolve())
            or plan["configuration_sha256"] != basis["configuration_sha256"]
            or plan["delivery_sha256"] != digest(intent["arguments"]["delivery"]) or plan["path"] != intent["arguments"]["path"]):
        refuse("INPUT_CONFLICT", "/capture/plan")
    path = plan["path"]
    if type(plan["outputs"]) is not list or [row.get("path") for row in plan["outputs"]] != [path, path + ".provenance.yml"]:
        refuse("INPUT_CONFLICT", "/capture/outputs")
    for row in plan["outputs"]:
        if type(row) is not dict or set(row) != {"path", "prior_sha256", "blob"}:
            refuse("INPUT_CONFLICT", "/capture/output")
        row["data"] = store.read_stage(row.pop("blob"))
    delivery = validate(intent["arguments"]["delivery"])
    supplied = metadata(delivery["capture"])
    from .source_capture_inputs import CaptureInput
    candidate = CaptureInput(plan["outputs"][0]["data"], supplied)
    if (candidate.content_sha256 != delivery["capture"]["content_sha256"] or len(candidate.content) != delivery["capture"]["content_bytes"]
            or plan["outputs"][1]["data"] != canonical(provenance(delivery))
            or any(row["prior_sha256"] not in {None, file_hash(row["data"])} for row in plan["outputs"])):
        refuse("INPUT_CONFLICT", "/capture/bytes")
    status = "already_present" if all(row["prior_sha256"] is not None for row in plan["outputs"]) else "delivered"
    if any(row["prior_sha256"] is not None for row in plan["outputs"]) and status != "already_present":
        refuse("INPUT_CONFLICT", "/capture/partial_initial_pair")
    files = {row["path"]: row["data"] for row in plan["outputs"]}
    if plan["result"] != expected_result(path, delivery, files, status=status):
        refuse("INPUT_CONFLICT", "/capture/result")
    return plan


def prepare(root, intent, store):
    intent = _intent(intent)
    values = intent["arguments"]
    root, _, view = _host(root, values["path"], values["delivery"])
    store.prepare(intent)
    retained = store.staged("capture-plan")
    if retained is not None:
        basis = _basis(intent, retained, digest(view.config))
        load_plan(root, intent, basis, store)
        return basis
    plan = prepare_effects(root, intent)
    for index, row in enumerate(plan["outputs"]):
        row["blob"] = store.stage_blob("capture-output-" + str(index), row.pop("data"))
    raw = canonical(plan)
    if len(raw) > MAX_RECORD_BYTES:
        refuse("LIMIT", "/capture/plan")
    return _basis(intent, store.stage_blob("capture-plan", raw), plan["configuration_sha256"])


def invoke(root, intent, basis, store):
    intent = _intent(intent)
    load_plan(root, intent, basis, store)
    return deliver(root, **intent["arguments"], _store=store, _basis=basis)


def prove(root, intent, basis, store):
    intent = _intent(intent)
    values = intent["arguments"]
    root, context, _ = _host(root, values["path"], values["delivery"])
    plan = load_plan(root, intent, basis, store)
    from .source_capture_operations import acquisition_barrier
    with acquisition_barrier(root, context):
        capture = _check_tree(root, plan)
    matched = all(capture.files.get(row["path"]) == row["data"] for row in plan["outputs"])
    present = any(row["path"] in capture.files for row in plan["outputs"])
    return {"present": present, "desired": matched, "completion": False,
        "operation_outcome": "desired_state_only" if matched else "original_unknown",
        "observation": {"expected_result_sha256": digest(plan["result"]), "postconditions_proved": matched,
            "artifact_revision": plan["result"]["artifact_revision"] if matched else None,
            "current_acceptance": "not_evaluated", "original_execution_timing": "unknown"}}


def resume(root, request_id):
    """Complete only retained sealed outputs; never reread or reacquire an original."""
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("AUTHORITY_REQUIRED", "/host_context")
    context.require_grant("mutation_operations.reconcile")
    context.require_grant("mutation_operations.inspect")
    context.require_grant(REPAIR_GRANT)
    from .mutation_operations import _generation, _host_selection, reconcile
    root = context.require_path(Path(root), write=True).resolve(strict=True)
    record = MutationStore(root, context.mutation_control_directory).inspect(request_id)
    if record is None:
        refuse("REQUEST_UNKNOWN", "/request_id")
    intent = _intent(record["intent"])
    if _host_selection() != intent["host_context_sha256"] or _generation() != intent["owner_generation"]:
        refuse("HOST_CONTEXT_CONFLICT", "/capture/retained_owner")
    with MutationStore(root, context.mutation_control_directory, intent).locked() as store:
        store.prepare(intent)
        state, receipt = store.read(), store.receipt()
        if receipt is None or state is None:
            refuse("OUTCOME_UNKNOWN", "/capture/receipt")
        if receipt["status"] in {"committed", "refused"}:
            return receipt
        if state["status"] == "reconciled":
            return checked(RECONCILIATION, {"schema_version": RECONCILIATION, "request_id": request_id,
                "status": "reconciled", **state["reconciliation"], "historical": True, "authorized": False, "evidence_accepted": False})
        if state["status"] not in {"partial", "commit_unknown"} or state["result"] is not None:
            refuse("OUTCOME_UNKNOWN", "/capture/state")
        basis = state["owner_basis"]
        deliver(root, **intent["arguments"], _store=store, _basis=basis, _recovery=True)
    return reconcile(root, request_id)
