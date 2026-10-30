"""Staged canonical coverage outputs and conservative interrupted-write proof."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ._facades._base import call_seam
from ._pack_io import canonical, json_document, read_file, yaml_document
from ._script_host import load_packaged_script, shared_assets_root
from .host_context import checked_operation
from .mutation_contracts import INTENT, RECEIPT, STATE, checked, detached, digest, refuse
from .mutation_store import MAX_RECORD_BYTES, MAX_STAGE_BLOB_BYTES, MAX_STAGE_BYTES

BASIS = "research-mutation-coverage-basis/v1"
PLAN = "evidence-coverage-plan/v1"
CLOCK = "evidence-coverage-clock/v1"
OPERATIONS = {"coverage.evaluate": "run_evaluate_checked", "coverage.revision": "run_revision"}


def _owner(stem):
    return load_packaged_script(shared_assets_root(), stem)


def _arguments(intent):
    intent = checked(INTENT, intent)
    if intent["operation"] not in OPERATIONS:
        refuse("OPERATION_UNSUPPORTED", "/operation")
    return intent, intent["arguments"]


def _read_json(store, descriptor):
    raw = store.read_stage(descriptor)
    if len(raw) > MAX_RECORD_BYTES:
        refuse("LIMIT", "/owner_plan")
    return detached(json_document(raw))


def _clock(store, observed_at=None):
    if observed_at is not None:
        descriptor = store.stage_blob("coverage-clock", canonical({"schema_version": CLOCK, "observed_at": observed_at}))
    else:
        descriptor = store.staged("coverage-clock")
        if descriptor is None:
            refuse("INPUT_CONFLICT", "/owner_clock")
    value = _read_json(store, descriptor)
    if type(value) is not dict or set(value) != {"schema_version", "observed_at"} or value["schema_version"] != CLOCK:
        refuse("INPUT_CONFLICT", "/owner_clock")
    try:
        return _owner("coverage_manifest").observed_clock(datetime.fromisoformat(value["observed_at"]))
    except (ValueError, TypeError):
        refuse("INPUT_CONFLICT", "/owner_clock")


def _basis(root, intent, descriptor):
    config = yaml_document(read_file(root, "research.yml"))
    return {"schema_version": BASIS, "kind": intent["operation"], "configuration_sha256": digest(config), "plan": descriptor}


def _preparing(root, intent, store):
    state = store.read()
    if state is None or state["status"] != "prepared":
        refuse("INPUT_CONFLICT", "/owner_state")
    existing = state["owner_basis"] or {}
    if existing.get("schema_version") == BASIS and existing.get("preparing") is True:
        if existing.get("kind") != intent["operation"]:
            refuse("INPUT_CONFLICT", "/owner_basis")
        return existing["observed_at"]
    clock = datetime.now(timezone.utc).isoformat()
    config = yaml_document(read_file(root, "research.yml"))
    state["owner_basis"] = {"schema_version": BASIS, "kind": intent["operation"],
        "configuration_sha256": digest(config), "preparing": True, "observed_at": clock}
    store.write(checked(STATE, state), expected_sha256=store.state_sha256())
    return clock


def _load(root, intent, basis, store):
    if (type(basis) is not dict or set(basis) != {"schema_version", "kind", "configuration_sha256", "plan"}
            or basis["schema_version"] != BASIS or basis["kind"] != intent["operation"]):
        refuse("INPUT_CONFLICT", "/owner_basis")
    plan = _read_json(store, basis["plan"])
    common = {"schema_version", "kind", "slug", "observed_at", "configuration_sha256", "dependencies_sha256",
              "new_directories", "private_view", "outputs", "report_blob", "input_sha256", "controls"}
    fields = common | ({"request_sha256", "original_question_blob", "already"} if intent["operation"] == "coverage.revision" else set())
    if (type(plan) is not dict or set(plan) != fields or plan["schema_version"] != PLAN
            or plan["kind"] != intent["operation"] or plan["input_sha256"] != intent["input_sha256"]
            or type(plan["outputs"]) is not list or len(plan["outputs"]) != (3 if intent["operation"] == "coverage.revision" else 1)
            or type(plan["new_directories"]) is not list or len(plan["new_directories"]) > 64):
        refuse("INPUT_CONFLICT", "/owner_plan")
    coverage = _owner("coverage_manifest")
    slug = intent["arguments"].get("slug") or intent["arguments"].get("request", {}).get("slug")
    if plan["slug"] != coverage.validate_slug(slug) or plan["observed_at"] != _clock(store).isoformat():
        refuse("INPUT_CONFLICT", "/owner_plan")
    for key in ("configuration_sha256", "dependencies_sha256"):
        if type(plan[key]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", plan[key]) is None:
            refuse("INPUT_CONFLICT", "/owner_plan")
    if intent["operation"] == "coverage.revision" and plan["request_sha256"] != _owner("_pack_revision_impact").digest(intent["arguments"]["request"]):
        refuse("INPUT_CONFLICT", "/owner_plan")
    seen = set()
    targets = []
    for row in plan["outputs"]:
        if (type(row) is not dict or set(row) != {"path", "prior_sha256", "blob"} or type(row["path"]) is not str
                or row["path"] in seen or row["prior_sha256"] is not None and
                (type(row["prior_sha256"]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", row["prior_sha256"]) is None)):
            refuse("INPUT_CONFLICT", "/owner_plan/outputs")
        _owner("_owner_paths").checked_path(root, row["path"], field="coverage_output", kind="file")
        seen.add(row["path"])
        targets.append(row["path"])
        row["data"] = store.read_stage(row.pop("blob"))
    plan["report"] = _read_json(store, plan.pop("report_blob"))
    if intent["operation"] == "coverage.revision":
        try:
            plan["original_question"] = store.read_stage(plan.pop("original_question_blob")).decode("utf-8")
        except UnicodeError:
            refuse("INPUT_CONFLICT", "/owner_plan/question")
    del plan["input_sha256"]
    config = yaml_document(read_file(root, "research.yml"))
    if (basis["configuration_sha256"] != digest(config) or type(plan["private_view"]) not in {dict, type(None)}
            or plan["private_view"] is not None and (set(plan["private_view"]) != {"state_id", "checkpoint", "authority_sha256"}
                or type(plan["private_view"]["state_id"]) is not str
                or type(plan["private_view"]["authority_sha256"]) is not str
                or re.fullmatch(r"sha256:[a-f0-9]{64}", plan["private_view"]["authority_sha256"]) is None
                or plan["private_view"]["checkpoint"] is not None and
                (type(plan["private_view"]["checkpoint"]) is not str
                 or re.fullmatch(r"sha256:[a-f0-9]{64}", plan["private_view"]["checkpoint"]) is None))):
        refuse("INPUT_CONFLICT", "/owner_plan")
    allowed = {parent.as_posix() for target in targets for parent in Path(target).parents if parent.as_posix() != "."}
    if (any(type(path) is not str or path not in allowed for path in plan["new_directories"])
            or plan["new_directories"] != sorted(set(plan["new_directories"]))):
        refuse("INPUT_CONFLICT", "/owner_plan/directories")
    for path in plan["new_directories"]:
        _owner("_owner_paths").checked_path(root, path, field="coverage_output_directory", kind="directory")
    if intent["operation"] == "coverage.evaluate":
        expected = [coverage.manifest_path(root, config, plan["slug"]).relative_to(root).as_posix()]
        if plan["new_directories"] or plan["controls"] != {"files": [], "new_directories": []}:
            refuse("INPUT_CONFLICT", "/owner_plan/directories")
    else:
        if type(plan["already"]) is not bool:
            refuse("INPUT_CONFLICT", "/owner_plan")
        question = coverage.ensure_question_exists(root, config, plan["slug"])
        original_fm, _body = _owner("verify_quotes").split_page(plan["original_question"])
        selected = coverage.selected_manifest_path(root, config, plan["slug"], original_fm.get("coverage_manifest"))
        request = intent["arguments"]["request"]
        expected = ["runs/pack-revisions/" + request["revision_id"][7:] + "/" + plan["slug"] + "-" + plan["request_sha256"][7:] + ".json",
                    question.relative_to(root).as_posix(), selected.relative_to(root).as_posix()]
        lock = _owner("question_claim").question_lock_path(question).relative_to(root).as_posix()
        parent = Path(lock).parent.as_posix()
        controls = plan["controls"]
        if (type(controls) is not dict or set(controls) != {"files", "new_directories"} or controls["files"] != [lock]
                or controls["new_directories"] not in ([], [parent])):
            refuse("INPUT_CONFLICT", "/owner_plan/controls")
        _owner("_owner_paths").checked_path(root, lock, field="coverage_control", kind="file")
        _owner("_owner_paths").checked_path(root, parent, field="coverage_control_directory", kind="directory")
    if targets != expected:
        refuse("INPUT_CONFLICT", "/owner_plan/outputs")
    return plan


def _report_quota(intent, basis, report):
    report = detached(report)
    common = {"request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "before_revision": intent["expected_revision"], "after_revision": intent["expected_revision"], "result": report, "error": None}
    documents = [checked(STATE, {"schema_version": STATE, **common, "status": "owner_completed", "owner_basis": basis, "reconciliation": None}),
        checked(RECEIPT, {"schema_version": RECEIPT, **common, "operation": intent["operation"], "status": "committed",
                        "historical": True, "authorized": False, "evidence_accepted": False})]
    uncertain = {"error_code": "MUTATION_OWNER_OUTCOME_UNKNOWN", "message": "The owner did not return a complete outcome.",
                 "recoverable": False, "remediation": "Inspect canonical durable state before retrying.", "details": {}}
    proof = {"current_revision": intent["expected_revision"], "owner_observation": {"result_sha256": digest(report), "error_code": None},
        "completion_proved": True, "desired_state_proved": False, "recovery_supported": True,
        "proof_kind": "owner_return", "operation_outcome": "owner_return_success"}
    documents.append(checked(STATE, {"schema_version": STATE, **common, "status": "reconciled", "owner_basis": basis,
                                   "reconciliation": proof, "error": uncertain}))
    if any(len(canonical(document)) > MAX_RECORD_BYTES for document in documents):
        refuse("LIMIT", "/owner_result")


def _prepare(root, intent, store):
    """Stage every exact output and report before the dispatcher records execution."""
    root = Path(root).resolve()
    intent, arguments = _arguments(intent)
    store.prepare(intent)
    retained = store.staged("coverage-plan")
    if retained is not None:
        basis = _basis(root, intent, retained)
        plan = _load(root, intent, basis, store)
        _report_quota(intent, basis, plan["report"])
        return basis
    clock = _clock(store, _preparing(root, intent, store))
    with checked_operation("coverage_manifest", OPERATIONS[intent["operation"]], (root,), arguments) as revalidate:
        coverage = _owner("coverage_manifest")
        if intent["operation"] == "coverage.evaluate":
            plan = coverage.prepare_evaluation(root, slug=arguments["slug"], _observed_at=clock)
        else:
            plan = _owner("_coverage_revision").prepare_migration(root, arguments["request"], _observed_at=clock)
        revalidate()
    report = detached(plan.pop("report"))
    report_bytes = canonical(report)
    original = plan.pop("original_question", None)
    payloads = [row["data"] for row in plan["outputs"]] + [report_bytes] + ([original.encode()] if original is not None else [])
    if (len(report_bytes) > MAX_RECORD_BYTES or any(len(raw) > MAX_STAGE_BLOB_BYTES for raw in payloads)
            or sum(len(raw) for raw in payloads) > MAX_STAGE_BYTES):
        refuse("LIMIT", "/owner_plan")
    for index, row in enumerate(plan["outputs"]):
        row["blob"] = store.stage_blob("coverage-output-" + str(index), row.pop("data"))
    plan["report_blob"] = store.stage_blob("coverage-report", report_bytes)
    if original is not None:
        plan["original_question_blob"] = store.stage_blob("coverage-original-question", original.encode())
    plan["input_sha256"] = intent["input_sha256"]
    metadata = canonical(detached(plan))
    if len(metadata) > MAX_RECORD_BYTES:
        refuse("LIMIT", "/owner_plan")
    basis = _basis(root, intent, store.stage_blob("coverage-plan", metadata))
    _report_quota(intent, basis, report)
    return basis


def prepare(root, intent, store):
    """Authorize the actual owner before reading or retaining private plan metadata."""
    intent, arguments = _arguments(intent)
    with checked_operation("coverage_manifest", OPERATIONS[intent["operation"]], (root,), arguments) as revalidate:
        basis = _prepare(root, intent, store)
        revalidate()
        return basis


def invoke(root, intent, basis, store):
    """Consume the verified private plan through the canonical owning commit."""
    intent, arguments = _arguments(intent)
    with checked_operation("coverage_manifest", OPERATIONS[intent["operation"]], (root,), arguments) as revalidate:
        plan = _load(Path(root).resolve(), intent, basis, store)
        revalidate()
        return call_seam(_owner, "coverage_manifest", OPERATIONS[intent["operation"]], root, **arguments, _plan=plan)


def prove(root, intent, basis, store):
    """Prove exact desired post-state without replay or current acceptance claims."""
    root = Path(root).resolve()
    intent, arguments = _arguments(intent)
    with checked_operation("coverage_manifest", OPERATIONS[intent["operation"]], (root,), arguments) as revalidate:
        plan = _load(root, intent, basis, store)
        coverage = _owner("coverage_manifest")
        capture = _owner("_evidence_revision").capture_workspace(root)
        rows = [{"path": row["path"], "present": row["path"] in capture.files,
                 "matched": capture.files.get(row["path"]) == row["data"],
                 "sha256": "sha256:" + hashlib.sha256(row["data"]).hexdigest()} for row in plan["outputs"]]
        present = any(row["matched"] for row in rows)
        dependency_ok = True
        try:
            coverage.check_recovery_inputs(root, plan)
        except (Exception, SystemExit):
            dependency_ok = False
        desired = dependency_ok and all(row["matched"] for row in rows)
        if desired and intent["operation"] == "coverage.revision":
            document = yaml.safe_load(plan["outputs"][2]["data"])
            desired = _owner("_pack_revision_guard").pending_question(root, plan["slug"], document) is None
            history = json_document(plan["outputs"][0]["data"])
            desired = desired and history["request"] == arguments["request"] and history["question"] == plan["original_question"]
        revalidate()
    return {"present": present, "desired": desired, "completion": False,
        "operation_outcome": "desired_state_only" if desired else "original_unknown",
        "observation": {"outputs": rows, "dependencies_unchanged": dependency_ok,
                        "postconditions_proved": desired, "current_acceptance": "not_evaluated"}}
