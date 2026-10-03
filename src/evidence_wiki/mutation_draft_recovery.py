"""Staged unaccepted draft admission and exact interrupted-publication proof."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from ._facades._base import call_seam
from ._pack_io import canonical, json_document, read_file, yaml_document
from .draft_contracts import PROPOSAL, RESULT, validate_document
from .host_context import checked_operation
from .mutation_contracts import INTENT, RECEIPT, STATE, checked, detached, digest, refuse
from .mutation_store import MAX_RECORD_BYTES, MAX_STAGE_BLOB_BYTES, MAX_STAGE_BYTES
from .pack_discovery import owner

BASIS = "research-mutation-draft-basis/v1"
CLOCK = "evidence-draft-clock/v1"
PLAN = "evidence-draft-effect-plan/v1"
STEM, SEAM = "strict_evidence", "run_admit_document"
_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")


def _intent(value):
    value = checked(INTENT, value)
    if value["operation"] != "strict.admit":
        refuse("OPERATION_UNSUPPORTED", "/operation")
    return value


def _json(store, descriptor):
    raw = store.read_stage(descriptor)
    if len(raw) > MAX_RECORD_BYTES:
        refuse("LIMIT", "/owner_plan")
    return detached(json_document(raw))


def _marker(root, intent, store):
    state = store.read()
    if state is None or state["status"] != "prepared":
        refuse("INPUT_CONFLICT", "/owner_state")
    previous = state["owner_basis"] or {}
    if previous.get("schema_version") == BASIS and previous.get("preparing") is True:
        if previous.get("kind") != "strict.admit" or previous.get("input_sha256") != intent["input_sha256"]:
            refuse("INPUT_CONFLICT", "/owner_basis")
        return previous["observed_at"]
    observed = datetime.now(timezone.utc).isoformat()
    config = yaml_document(read_file(root, "research.yml"))
    state["owner_basis"] = {"schema_version": BASIS, "kind": "strict.admit", "configuration_sha256": digest(config),
        "input_sha256": intent["input_sha256"], "observed_at": observed, "preparing": True}
    store.write(checked(STATE, state), expected_sha256=store.state_sha256())
    return observed


def _clock(store, observed=None):
    if observed is not None:
        descriptor = store.stage_blob("draft-clock", canonical({"schema_version": CLOCK, "observed_at": observed}))
    else:
        descriptor = store.staged("draft-clock")
        if descriptor is None:
            refuse("INPUT_CONFLICT", "/owner_clock")
    value = _json(store, descriptor)
    if type(value) is not dict or set(value) != {"schema_version", "observed_at"} or value["schema_version"] != CLOCK:
        refuse("INPUT_CONFLICT", "/owner_clock")
    try:
        return owner("_draft_admission").clock(datetime.fromisoformat(value["observed_at"]))
    except (ValueError, TypeError):
        refuse("INPUT_CONFLICT", "/owner_clock")


def _basis(root, descriptor):
    return {"schema_version": BASIS, "kind": "strict.admit", "configuration_sha256": digest(yaml_document(read_file(root, "research.yml"))),
            "plan": descriptor}


def _load(root, intent, basis, store):
    if (type(basis) is not dict or set(basis) != {"schema_version", "kind", "configuration_sha256", "plan"}
            or basis["schema_version"] != BASIS or basis["kind"] != "strict.admit"):
        refuse("STAGING_INCOMPLETE", "/owner_basis")
    plan = _json(store, basis["plan"])
    fields = {"schema_version", "input_sha256", "proposal_sha256", "path", "prior_sha256", "observed_at",
        "configuration_sha256", "policy_sha256", "private_view", "new_directories", "controls", "dependencies_sha256", "inventory_blob", "report_blob"}
    if type(plan) is not dict or set(plan) != fields or plan["schema_version"] != PLAN or plan["input_sha256"] != intent["input_sha256"]:
        refuse("INPUT_CONFLICT", "/owner_plan")
    for field in ("input_sha256", "proposal_sha256", "configuration_sha256", "policy_sha256", "dependencies_sha256"):
        if type(plan[field]) is not str or _HASH.fullmatch(plan[field]) is None:
            refuse("INPUT_CONFLICT", "/owner_plan")
    if plan["prior_sha256"] is not None and (type(plan["prior_sha256"]) is not str or _HASH.fullmatch(plan["prior_sha256"]) is None):
        refuse("INPUT_CONFLICT", "/owner_plan")
    if plan["observed_at"] != _clock(store).isoformat():
        refuse("INPUT_CONFLICT", "/owner_clock")
    proposal = validate_document(PROPOSAL, intent["arguments"]["proposal"])
    admission = owner("_draft_admission")
    if plan["proposal_sha256"] != admission.file_hash(owner("_evidence_revision").canonical_bytes(proposal)):
        refuse("INPUT_CONFLICT", "/owner_plan")
    config = yaml_document(read_file(root, "research.yml"))
    policy = owner("_strict_evidence").resolve_policy(root, config)
    if (policy is None or basis["configuration_sha256"] != digest(config) or type(plan["path"]) is not str
            or plan["path"] != admission.target(root, config, policy)):
        refuse("INPUT_CONFLICT", "/owner_target")
    allowed = {parent.as_posix() for parent in Path(plan["path"]).parents if parent.as_posix() != "."}
    directories = plan["new_directories"]
    if (type(directories) is not list or len(directories) > 64 or any(type(path) is not str or path not in allowed for path in directories)
            or directories != sorted(set(directories))):
        refuse("INPUT_CONFLICT", "/owner_directories")
    controls = plan["controls"]
    if (type(controls) is not dict or set(controls) != {"files", "new_directories"}
            or controls["files"] != [admission.LOCK] or controls["new_directories"] not in ([], [".locks"])):
        refuse("INPUT_CONFLICT", "/owner_controls")
    owner("_owner_paths").checked_path(root, admission.LOCK, field="claims_gate", kind="file")
    owner("_owner_paths").checked_path(root, ".locks", field="claims_gate_directory", kind="directory")
    view = plan["private_view"]
    if (type(view) not in {dict, type(None)} or view is not None and
            (set(view) != {"state_id", "checkpoint", "authority_sha256"} or type(view["state_id"]) is not str
             or type(view["authority_sha256"]) is not str or _HASH.fullmatch(view["authority_sha256"]) is None
             or view["checkpoint"] is not None and (type(view["checkpoint"]) is not str or _HASH.fullmatch(view["checkpoint"]) is None))):
        refuse("INPUT_CONFLICT", "/owner_policy_view")
    plan["data"] = store.read_stage(plan.pop("inventory_blob"))
    plan["report"] = validate_document(RESULT, _json(store, plan.pop("report_blob")))
    if (plan["data"] != owner("_evidence_revision").canonical_bytes(proposal["inventory"])
            or plan["report"]["inventory_sha256"] != admission.file_hash(plan["data"])
            or plan["report"]["proposal_sha256"] != plan["proposal_sha256"]
            or plan["report"]["claims_path"] != plan["path"] or plan["report"]["admitted_at"] != plan["observed_at"]):
        refuse("INPUT_CONFLICT", "/owner_plan")
    del plan["input_sha256"]
    return plan


def _quota(intent, basis, report):
    common = {"request_id": intent["request_id"], "input_sha256": intent["input_sha256"], "before_revision": intent["expected_revision"],
              "after_revision": intent["expected_revision"], "result": report, "error": None}
    error = {"error_code": "MUTATION_OWNER_OUTCOME_UNKNOWN", "message": "The owner did not return a complete outcome.",
             "recoverable": False, "remediation": "Inspect the canonical claims inventory before retrying.", "details": {}}
    proof = {"current_revision": intent["expected_revision"], "owner_observation": {"retained_result_sha256": digest(report), "current_acceptance": "not_evaluated"},
        "completion_proved": True, "desired_state_proved": False, "recovery_supported": True, "proof_kind": "owner_return", "operation_outcome": "owner_return_success"}
    documents = [checked(STATE, {"schema_version": STATE, **common, "status": "owner_completed", "owner_basis": basis, "reconciliation": None}),
        checked(STATE, {"schema_version": STATE, **common, "status": "partial", "error": error, "owner_basis": basis, "reconciliation": proof}),
        checked(RECEIPT, {"schema_version": RECEIPT, **common, "operation": "strict.admit", "status": "committed",
                         "historical": True, "authorized": False, "evidence_accepted": False})]
    if any(len(canonical(document)) > MAX_RECORD_BYTES for document in documents):
        refuse("LIMIT", "/owner_result")


def _projected_tree(root, plan):
    capture = owner("_evidence_revision").capture_workspace(root)
    files = {name: len(raw) for name, raw in capture.files.items()}
    files[plan["path"]] = len(plan["data"])
    directories = set(capture.directories) | set(plan["new_directories"]) | set(plan["controls"]["new_directories"])
    files[owner("_draft_admission").LOCK] = 0
    if len(files) > 10000 or len(files) + len(directories) > 20000 or sum(files.values()) > MAX_STAGE_BYTES:
        refuse("LIMIT", "/owner_tree")


def prepare(root, intent, store):
    intent = _intent(intent)
    root = Path(root).resolve()
    with checked_operation(STEM, SEAM, (root, intent["arguments"]["proposal"]), {}) as revalidate:
        store.prepare(intent)
        retained = store.staged("draft-plan")
        if retained is not None:
            basis = _basis(root, retained)
            plan = _load(root, intent, basis, store)
            _quota(intent, basis, plan["report"])
            revalidate()
            return basis
        observed = _clock(store, _marker(root, intent, store))
        plan = owner("_draft_admission").prepare_admission(root, intent["arguments"]["proposal"], observed_at=observed)
        _projected_tree(root, plan)
        report = validate_document(RESULT, plan.pop("report"))
        report_bytes = canonical(report)
        if len(plan["data"]) > MAX_STAGE_BLOB_BYTES or len(report_bytes) > MAX_RECORD_BYTES:
            refuse("LIMIT", "/owner_plan")
        plan["inventory_blob"] = store.stage_blob("draft-inventory", plan.pop("data"))
        plan["report_blob"] = store.stage_blob("draft-report", report_bytes)
        plan["input_sha256"] = intent["input_sha256"]
        metadata = canonical(detached(plan))
        if len(metadata) > MAX_RECORD_BYTES:
            refuse("LIMIT", "/owner_plan")
        basis = _basis(root, store.stage_blob("draft-plan", metadata))
        _quota(intent, basis, report)
        revalidate()
        return basis


def invoke(root, intent, basis, store):
    intent = _intent(intent)
    with checked_operation(STEM, SEAM, (root, intent["arguments"]["proposal"]), {}) as revalidate:
        plan = _load(Path(root).resolve(), intent, basis, store)
        revalidate()
        return call_seam(owner, STEM, SEAM, root, intent["arguments"]["proposal"], _plan=plan)


def prove(root, intent, basis, store):
    intent = _intent(intent)
    root = Path(root).resolve()
    with checked_operation(STEM, SEAM, (root, intent["arguments"]["proposal"]), {}) as revalidate:
        plan = _load(root, intent, basis, store)
        capture = owner("_evidence_revision").capture_workspace(root)
        actual = capture.files.get(plan["path"])
        matched = actual == plan["data"]
        dependencies_ok = True
        try:
            owner("_draft_admission").check_inputs(root, plan)
        except (Exception, SystemExit):
            dependencies_ok = False
        desired = matched and dependencies_ok
        revalidate()
    return {"present": matched or actual is not None and ("sha256:" + hashlib.sha256(actual).hexdigest()) != plan["prior_sha256"],
        "desired": desired, "completion": False, "operation_outcome": "desired_state_only" if desired else "original_unknown",
        "observation": {"claims_path": plan["path"], "inventory_sha256": plan["report"]["inventory_sha256"],
            "inventory_matched": matched, "dependencies_unchanged": dependencies_ok, "postconditions_proved": desired,
            "original_execution_timing": "unknown", "current_acceptance": "not_evaluated"}}
