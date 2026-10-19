"""Authenticated private originals behind bounded selective review delivery."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from contextlib import contextmanager

from ._pack_io import json_document, read_file, relative_path
from ._review_dependency_inputs import assert_current, current_inputs
from ._review_dependency_json import canonical, detached, digest
from ._review_dependency_wire import pack, unpack
from .mutation_contracts import validate_request_id
from .pack_discovery import owner
from .research_journal_contracts import canonical_record, digest_record
from .research_session_contracts import refuse

_CAPTURE = "research-review-dependency-capture/v1"
_CAPTURE_V2 = "research-review-dependency-capture/v2"
_LINK = "research-review-dependency-delivery-link/v1"
_CAPTURE_KIND = "execution_review_dependency_capture"
_LINK_KIND = "execution_review_dependency_delivery"
_MAX_RECORD = 262144
_CAPTURE_FIELDS = {"schema_version", "phase", "assembly_request_id", "attempt_id", "opening_record_ref",
    "claim_id", "claim_schema_version", "observed_workspace_basis_id", "capture_revision_id", "workspace_binding",
    "claims_path", "question_paths", "inputs", "owner_snapshot"}
_LINK_FIELDS = {"schema_version", "attempt_id", "attempt_content_sha256", "assembly_request_id",
    "opening_record_ref", "parent_binding_sha256"}


def _hash_bytes(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _encoded(raw):
    encoded = base64.b64encode(raw).decode("ascii")
    return {"sha256": _hash_bytes(raw), "bytes": len(raw),
        "raw_base64": [encoded[start:start + 65536] for start in range(0, len(encoded), 65536)]}


def _decoded(value, *, path=False):
    if (type(value) is not dict or set(value) != {"sha256", "bytes", "raw_base64"} | ({"path"} if path else set())
            or type(value["bytes"]) is not int or not 0 <= value["bytes"] <= _MAX_RECORD
            or type(value["raw_base64"]) is not list or len(value["raw_base64"]) > 6):
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    chunks = value["raw_base64"]
    if any(type(chunk) is not str or not 0 < len(chunk) <= 65536 or len(chunk) % 4
            or index + 1 < len(chunks) and len(chunk) != 65536 for index, chunk in enumerate(chunks)):
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    try:
        encoded = "".join(chunks)
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError):
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    if (base64.b64encode(raw).decode("ascii") != encoded or len(raw) != value["bytes"]
            or _hash_bytes(raw) != value["sha256"]):
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    if path:
        relative_path(value["path"])
    return raw


def _record_ref(record):
    return digest_record({key: value for key, value in record.items() if key != "event_ref"})


def _one(records, predicate):
    selected = [row for row in records if predicate(row)]
    if len(selected) != 1:
        refuse("REVIEW_DEPENDENCY_CUSTODY_AMBIGUOUS")
    return selected[0]


def _append(session, kind, request_id, data):
    from .execution_session import append, retained

    records = retained(session)
    saved = [row for row in records if row["request_id"] == request_id]
    if saved:
        if len(saved) != 1 or saved[0]["kind"] != kind or saved[0]["data"] != data:
            refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
        return saved[0]
    record = session.execution.seal_record(session, kind, request_id, data, records=records)
    if len(canonical_record(record)) > _MAX_RECORD:
        refuse("REVIEW_DEPENDENCY_CUSTODY_BOUND")
    append(session, kind, request_id, data)
    return _one(retained(session), lambda row: row["kind"] == kind and row["request_id"] == request_id)


def _capture(session, prepared, material, *, phase, assembly_request_id, attempt_id, opening_record_ref):
    from .mutation_store import capture_revision

    holder = current_inputs()
    assert_current(holder)
    if holder._session is not session or prepared.get("review_schema") != "evidence-strict-review/v3":
        refuse("REVIEW_DEPENDENCY_INPUTS_UNAVAILABLE")
    claims_path = relative_path(prepared["snapshot"]["policy"]["claims_path"])
    questions = sorted({relative_path(row["path"]) for row in material["questions"]}, key=lambda value: value.encode("utf-8"))
    paths = sorted({claims_path, *questions}, key=lambda value: value.encode("utf-8"))
    if len(paths) > 33:
        refuse("REVIEW_DEPENDENCY_CUSTODY_BOUND")
    captured = capture_revision(session.root, excluded=(".locks", "raw/.locks"), input_paths=paths)
    from .review_material import current_review_material
    actual = owner("strict_evidence").prepare_review(session.root, material["claim_id"])
    if (actual["snapshot"] != prepared["snapshot"] or actual["observed_workspace_basis_id"] != prepared["observed_workspace_basis_id"]
            or actual["claim_basis_id"] != prepared["claim_basis_id"] or actual["review_producer_id"] != prepared["review_producer_id"]
            or current_review_material(session.root, actual) != material):
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    inventory = owner("_strict_contract").claims_document(json_document(captured["input_bytes"][claims_path]))
    if inventory["schema_version"] != prepared["claim_schema_version"]:
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    data = {"schema_version": _CAPTURE_V2, "phase": phase, "assembly_request_id": assembly_request_id,
        "attempt_id": attempt_id, "opening_record_ref": opening_record_ref,
        "claim_id": material["claim_id"], "claim_schema_version": prepared["claim_schema_version"],
        "observed_workspace_basis_id": prepared["observed_workspace_basis_id"],
        "capture_revision_id": captured["revision_id"], "workspace_binding": captured["workspace_binding"],
        "claims_path": claims_path, "question_paths": questions,
        "inputs": [{"path": path, **_encoded(captured["input_bytes"][path])} for path in paths],
        "owner_snapshot": pack(prepared["snapshot"], "native_snapshot")}
    if capture_revision(session.root, excluded=(".locks", "raw/.locks"))["revision_id"] != captured["revision_id"]:
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    assert_current(holder)
    return data


def capture_opening(session, *, assembly_request_id, prepared, material):
    """Retain full raw originals before packet assembly and worker dispatch."""
    from .execution_session import child_id, retained

    assembly_request_id = validate_request_id(assembly_request_id)
    request_id = child_id(assembly_request_id, "dependency_opening")
    old = [row for row in retained(session) if row["kind"] == _CAPTURE_KIND and row["request_id"] == request_id]
    if old:
        if len(old) != 1:
            refuse("REVIEW_DEPENDENCY_CUSTODY_AMBIGUOUS")
        _check_capture(old[0]["data"])
        _compare_current(session, old[0]["data"], prepared, material)
        return old[0]
    data = _capture(session, prepared, material, phase="opening", assembly_request_id=assembly_request_id,
        attempt_id=None, opening_record_ref=None)
    return _append(session, _CAPTURE_KIND, request_id, data)


def _check_capture(data):
    if (type(data) is not dict or set(data) != _CAPTURE_FIELDS or data["schema_version"] not in {_CAPTURE, _CAPTURE_V2}
            or data["phase"] not in {"opening", "admission_closing"} or type(data["inputs"]) is not list
            or not 1 <= len(data["inputs"]) <= 33 or type(data["question_paths"]) is not list
            or len(data["question_paths"]) > 32):
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    validate_request_id(data["assembly_request_id"])
    if data["phase"] == "opening":
        if data["attempt_id"] is not None or data["opening_record_ref"] is not None:
            refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    else:
        validate_request_id(data["attempt_id"])
    inputs = {row["path"]: _decoded(row, path=True) for row in data["inputs"]}
    paths = [row["path"] for row in data["inputs"]]
    if (len(inputs) != len(paths) or paths != sorted({data["claims_path"], *data["question_paths"]},
            key=lambda value: value.encode("utf-8"))):
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    inventory = owner("_strict_contract").claims_document(json_document(inputs[data["claims_path"]]))
    if inventory["schema_version"] != data["claim_schema_version"]:
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    if data["schema_version"] == _CAPTURE_V2:
        try:
            snapshot = unpack(data["owner_snapshot"], "native_snapshot")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    else:
        snapshot = owner("_record_artifacts").json_document(_decoded(data["owner_snapshot"]))
        if canonical(snapshot) != _decoded(data["owner_snapshot"]):
            refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    return inputs, snapshot


def _compare_current(session, opening, prepared, material):
    current = _capture(session, prepared, material, phase="opening", assembly_request_id=opening["assembly_request_id"],
        attempt_id=None, opening_record_ref=None)
    # Intended journal appends change the mutation revision, while these exact
    # input bytes, native snapshot and full strict basis must remain unchanged.
    if (_check_capture(current) != _check_capture(opening)
            or any(current[key] != opening[key] for key in _CAPTURE_FIELDS - {"capture_revision_id", "schema_version", "owner_snapshot"})):
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")


@contextmanager
def _hint_directory(session, *, create=False):
    from .host_context import _root_descriptor

    holder = current_inputs()
    assert_current(holder)
    if holder._session is not session:
        refuse("REVIEW_DEPENDENCY_CUSTODY_UNAVAILABLE")
    control = session.context.mutation_control_directory
    if control is None or os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        refuse("REVIEW_DEPENDENCY_CUSTODY_UNAVAILABLE")
    with _root_descriptor(control) as root:
        owner("_host_evidence_store").private_entry(os.fstat(root), directory=True)
        if create:
            try:
                os.mkdir("review-dependency-bindings", mode=0o700, dir_fd=root)
                os.fsync(root)
            except FileExistsError:
                pass
        descriptor = os.open("review-dependency-bindings", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            before = os.fstat(descriptor)
            owner("_host_evidence_store").private_entry(before, directory=True)
            yield descriptor
            after = os.stat("review-dependency-bindings", dir_fd=root, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
        finally:
            os.close(descriptor)


def _hint(session, identity, *, value=None):
    if type(identity) is not str or len(identity) != 71 or not identity.startswith("sha256:"):
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    try:
        bytes.fromhex(identity[7:])
    except ValueError:
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    name = identity[7:] + ".json"
    with _hint_directory(session, create=value is not None) as descriptor:
        if value is not None:
            raw = owner("_evidence_revision").canonical_bytes(value)
            if owner("_evidence_revision").content_id("evidence-review-delivery-binding/v1", value) != identity or len(raw) > 65536:
                refuse("REVIEW_DEPENDENCY_CUSTODY_BOUND")
            try:
                leaf = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
            except FileExistsError:
                if read_file(descriptor, name, 65536) != raw:
                    refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
            else:
                with os.fdopen(leaf, "wb") as stream:
                    owner("_host_evidence_store").private_entry(os.fstat(stream.fileno()))
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(descriptor)
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
        owner("_host_evidence_store").private_entry(info)
        raw = read_file(descriptor, name, 65536)
        document = json_document(raw)
        if (owner("_evidence_revision").content_id("evidence-review-delivery-binding/v1", document) != identity
                or owner("_evidence_revision").canonical_bytes(document) != raw):
            refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
        return document


def bind_attempt(session, *, attempt, assembly_request_id, prepared, context):
    """Bind an actual immutable attempt after its native reservation exists."""
    from .execution_session import child_id, retained

    records = retained(session)
    saved = _one(records, lambda row: row["kind"] == "execution_attempt"
        and row["data"]["attempt"]["attempt_id"] == attempt.attempt_id)
    if session.execution.attempt(saved["data"]["attempt"]).document() != attempt.document():
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    opening = _one(records, lambda row: row["kind"] == _CAPTURE_KIND and row["data"]["phase"] == "opening"
        and row["data"]["assembly_request_id"] == assembly_request_id)
    _check_capture(opening["data"])
    returned = _one(records, lambda row: row["kind"] == "execution_native_observation"
        and row["data"]["observation"]["attempt_id"] == assembly_request_id and row["data"]["error_code"] is None)
    delivery = attempt.input_document["complete_review_delivery"]
    if returned["data"]["result"]["packet"] != delivery["packet"]:
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    binding = context.binding()
    parent = {"schema_version": "evidence-review-delivery-binding/v1", "run_id": session.run_id,
        "session_binding_id": digest(session.binding), "claim_id": prepared["snapshot"]["claim"]["id"],
        "required_review_role": prepared["dependency_basis"]["required_review_role"], "basis_id": prepared["claim_basis_id"],
        "observed_workspace_basis_id": prepared["observed_workspace_basis_id"],
        "semantic_material_id": prepared["semantic_material_id"],
        "owner_snapshot_sha256": _hash_bytes(owner("_evidence_revision").canonical_bytes(prepared["snapshot"])),
        "packet_sha256": delivery["packet_sha256"], "worker_input_sha256": "sha256:" + attempt.input_sha256,
        "assembly_request_id": assembly_request_id, "assembly_receipt_id": _record_ref(returned),
        "attempt_id": attempt.attempt_id, "attempt_content_sha256": digest(attempt.document()),
        **{key: binding[key] for key in ("aliases", "source_permutation", "order_seed", "order_seed_sha256")}}
    identity = owner("_evidence_revision").content_id("evidence-review-delivery-binding/v1", parent)
    _hint(session, identity, value=parent)
    data = {"schema_version": _LINK, "attempt_id": attempt.attempt_id, "attempt_content_sha256": digest(attempt.document()),
        "assembly_request_id": assembly_request_id, "opening_record_ref": _record_ref(opening), "parent_binding_sha256": identity}
    _append(session, _LINK_KIND, child_id(attempt.attempt_id, "dependency_delivery"), data)
    return {key: parent[key] for key in ("packet_sha256", "worker_input_sha256", "semantic_material_id",
        "assembly_receipt_id", "attempt_id", "attempt_content_sha256")} | {"binding_sha256": identity}


def delivery_for_attempt(session, attempt_id):
    records = session.execution.records(session)
    link = _one(records, lambda row: row["kind"] == _LINK_KIND and row["data"]["attempt_id"] == attempt_id)["data"]
    if set(link) != _LINK_FIELDS or link["schema_version"] != _LINK:
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    parent = _hint(session, link["parent_binding_sha256"])
    return {key: parent[key] for key in ("packet_sha256", "worker_input_sha256", "semantic_material_id",
        "assembly_receipt_id", "attempt_id", "attempt_content_sha256")} | {"binding_sha256": link["parent_binding_sha256"]}


def close_admission(session, *, attempt, prepared, material):
    """Close original raw/native material before the first registration effect."""
    from .execution_session import child_id, retained

    records = retained(session)
    link = _one(records, lambda row: row["kind"] == _LINK_KIND and row["data"]["attempt_id"] == attempt.attempt_id)["data"]
    opening = _one(records, lambda row: row["kind"] == _CAPTURE_KIND and _record_ref(row) == link["opening_record_ref"])
    _compare_current(session, opening["data"], prepared, material)
    prior = [row for row in records if row["kind"] == _CAPTURE_KIND and row["data"]["phase"] == "admission_closing"
        and row["data"]["attempt_id"] == attempt.attempt_id]
    if prior:
        if len(prior) != 1 or _check_capture(prior[0]["data"]) != _check_capture(opening["data"]):
            refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
        return prior[0]
    observation = _one(records, lambda row: row["kind"] == "execution_observation"
        and row["data"]["observation"]["attempt_id"] == attempt.attempt_id
        and row["data"]["observation"]["final_accounting"] is True)["data"]["observation"]
    observed = session.execution.authenticate(observation, attempt)
    if observed["state"] != "succeeded" or observed["final_accounting"] is not True:
        refuse("REVIEW_DEPENDENCY_WORKER_PROOF_REQUIRED")
    data = _capture(session, prepared, material, phase="admission_closing", assembly_request_id=link["assembly_request_id"],
        attempt_id=attempt.attempt_id, opening_record_ref=link["opening_record_ref"])
    return _append(session, _CAPTURE_KIND, child_id(attempt.attempt_id, "dependency_closing"), data)


class _Original:
    def __init__(self, holder, inputs, snapshot, parent, records, envelope):
        self._holder, self._inputs, self._snapshot = holder, inputs, snapshot
        self._parent, self._records = parent, records
        self._envelope = detached(envelope)

    def assert_current(self):
        assert_current(self._holder)
        session = self._holder._session
        if session.execution._records_for_binding(session.root, self._parent["run_id"], self._original_binding) != self._records:
            refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")

    def original_inputs(self):
        self.assert_current()
        return dict(self._inputs)

    def original_snapshot(self):
        self.assert_current()
        return detached(self._snapshot)

    def assert_review_envelope(self, envelope):
        """Compare an actual native record to the authenticated original application."""
        self.assert_current()
        if envelope != self._envelope:
            refuse("REVIEW_DEPENDENCY_APPLICATION_PROOF_REQUIRED")


def resolve_original(review, *, purpose):
    """Authenticate original native joins; a hint alone never grants custody."""
    from .research_session import _load

    holder = current_inputs()
    assert_current(holder)
    if purpose not in {"admission", "reuse"} or type(review) is not dict or review.get("schema_version") != "evidence-strict-review/v3":
        refuse("REVIEW_DEPENDENCY_CUSTODY_UNAVAILABLE")
    session = holder._session
    contract = owner("_strict_contract")
    contract.review_document(review)
    parent = _hint(session, review["delivery"]["binding_sha256"])
    owner("_review_dependency_contract").validate("evidence-review-delivery-binding/v1", parent)
    run_id = validate_request_id(parent["run_id"])
    controller = _load(session.root, "run_controller")
    runs = controller.runs_root(session.root)
    if runs.is_symlink() or len(list(runs.iterdir())) > 1024:
        refuse("REVIEW_DEPENDENCY_RUN_INVENTORY_INVALID")
    run = controller.run_dir(session.root, run_id)
    if run.is_symlink() or run.parent != runs or not run.is_dir():
        refuse("REVIEW_DEPENDENCY_RUN_INVENTORY_INVALID")
    state = controller._session_state(session.root, run_id)
    binding = state.get("research_session")
    if (type(binding) is not dict or binding.get("run_id") != run_id or binding.get("target") != str(session.root)
            or binding.get("workspace_binding") != session.binding["workspace_binding"]
            or binding.get("policy") != session.policy.to_dict()
            or binding.get("owner_generation") != session.binding["owner_generation"]
            or binding.get("host_context_sha256") != session.binding["host_context_sha256"]
            or any(binding.get(key) != value for key, value in session.execution.identity().items())
            or digest(binding) != parent["session_binding_id"]):
        refuse("REVIEW_DEPENDENCY_ORIGINAL_BINDING_CHANGED")
    records = session.execution._records_for_binding(session.root, run_id, binding)
    link = _one(records, lambda row: row["kind"] == _LINK_KIND
        and row["data"]["parent_binding_sha256"] == review["delivery"]["binding_sha256"])["data"]
    if set(link) != _LINK_FIELDS or link["schema_version"] != _LINK:
        refuse("REVIEW_DEPENDENCY_CUSTODY_INVALID")
    opening = _one(records, lambda row: row["kind"] == _CAPTURE_KIND and _record_ref(row) == link["opening_record_ref"])
    closing = _one(records, lambda row: row["kind"] == _CAPTURE_KIND and row["data"]["phase"] == "admission_closing"
        and row["data"]["attempt_id"] == parent["attempt_id"])
    inputs, snapshot = _check_capture(opening["data"])
    close_inputs, close_snapshot = _check_capture(closing["data"])
    if (inputs != close_inputs or snapshot != close_snapshot or snapshot != review["snapshot"]
            or closing["data"]["opening_record_ref"] != _record_ref(opening)
            or opening["data"]["observed_workspace_basis_id"] != review["observed_workspace_basis_id"]
            or closing["data"]["observed_workspace_basis_id"] != review["observed_workspace_basis_id"]):
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    saved = _one(records, lambda row: row["kind"] == "execution_attempt"
        and row["data"]["attempt"]["attempt_id"] == parent["attempt_id"])
    attempt = session.execution.attempt(saved["data"]["attempt"])
    if digest(attempt.document()) != parent["attempt_content_sha256"] or link["attempt_content_sha256"] != parent["attempt_content_sha256"]:
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    observations = [row["data"]["observation"] for row in records if row["kind"] == "execution_observation"
        and row["data"]["observation"]["attempt_id"] == attempt.attempt_id]
    final = [session.execution.authenticate(row, attempt) for row in observations if row.get("final_accounting") is True]
    if len(final) != 1 or final[0]["state"] != "succeeded":
        refuse("REVIEW_DEPENDENCY_WORKER_PROOF_REQUIRED")
    application = _one(records, lambda row: row["kind"] == "execution_application"
        and row["data"]["attempt_id"] == attempt.attempt_id)["data"]
    if application.get("operation") != "evaluate" or application.get("envelope", {}).get("payload", {}).get("body", {}).get("review") != review:
        refuse("REVIEW_DEPENDENCY_APPLICATION_PROOF_REQUIRED")
    from .mutation_contracts import review_envelope
    native = _one(records, lambda row: row["kind"] == "execution_native"
        and row["data"]["operation"] == "strict.register_review"
        and review_envelope(row["data"].get("arguments", {}).get("envelope")) == application["envelope"])
    returned = _one(records, lambda row: row["kind"] == "execution_native_observation"
        and row["data"]["observation"]["attempt_id"] == parent["assembly_request_id"])
    complete = attempt.input_document.get("complete_review_delivery")
    if (returned["data"].get("error_code") is not None or _record_ref(returned) != parent["assembly_receipt_id"]
            or type(complete) is not dict or complete["packet"] != returned["data"]["result"]["packet"]
            or complete["packet_sha256"] != parent["packet_sha256"] or digest(complete["packet"]) != parent["packet_sha256"]
            or "sha256:" + attempt.input_sha256 != parent["worker_input_sha256"]
            or _hash_bytes(owner("_evidence_revision").canonical_bytes(snapshot)) != parent["owner_snapshot_sha256"]
            or any(parent[key] != review["delivery"][key] for key in ("packet_sha256", "worker_input_sha256", "semantic_material_id",
                "assembly_receipt_id", "attempt_id", "attempt_content_sha256"))
            or parent["basis_id"] != review["basis_id"] or parent["claim_id"] != review["claim_id"]
            or parent["observed_workspace_basis_id"] != review["observed_workspace_basis_id"]):
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    from .review_context import ReviewContext, dependency_material
    context = ReviewContext.restore(attempt.input_document["packet"], saved["data"]["review_context"])
    material = dependency_material(context)
    if (material != snapshot["semantic_material"] or material["dependency_basis_id"] != parent["basis_id"]
            or context.packet_id != complete["packet"]["header"]["context_sha256"]
            or context.packet_id != attempt.job["payload"]["packet_id"]
            or any(context.binding()[key] != parent[key] for key in ("aliases", "source_permutation", "order_seed", "order_seed_sha256"))
            or context.binding()["semantic_material_id"] != parent["semantic_material_id"]
            or saved["data"].get("review_assembly_request_id") != parent["assembly_request_id"]
            or saved["data"].get("claim_id") != review["claim_id"]
            or review["dependency_basis"]["required_review_role"] != parent["required_review_role"]
            or context.document()["task_role"] != attempt.job["task_role"]
            or complete["packet"]["header"]["task_role"] != attempt.job["task_role"]
            or link["assembly_request_id"] != parent["assembly_request_id"]
            or opening["data"]["assembly_request_id"] != parent["assembly_request_id"]
            or closing["data"]["assembly_request_id"] != parent["assembly_request_id"]):
        refuse("REVIEW_DEPENDENCY_CUSTODY_CHANGED")
    intention = _one(records, lambda row: row["kind"] == "execution_native"
        and row["data"]["reservation"]["attempt_id"] == parent["assembly_request_id"])
    arguments = intention["data"].get("arguments", {})
    if (intention["data"].get("operation") != "review_pages.host_assembly"
            or set(arguments) != {"claim_id", "task_role", "page_chars", "source_ids", "context_version"}
            or arguments["claim_id"] != review["claim_id"] or arguments["task_role"] != attempt.job["task_role"]
            or arguments["context_version"] != "dependencies"):
        refuse("REVIEW_DEPENDENCY_ASSEMBLY_PROOF_REQUIRED")
    packet = complete["packet"]
    pages = {page["page_id"]: page for page in packet["pages"]}
    if len(pages) != len(packet["pages"]) or packet["complete"]["complete"] is not True:
        refuse("REVIEW_DEPENDENCY_ASSEMBLY_PROOF_REQUIRED")
    for descriptor in packet["header"]["pages"]:
        page = pages.get(descriptor["page_id"])
        delivery = _one(records, lambda row, descriptor=descriptor: row["kind"] == "execution_review_page_delivery"
            and row["data"].get("page_id") == descriptor["page_id"]
            and row["data"].get("header_sha256") == digest(packet["header"]))
        if (page is None or digest(page) != descriptor["sha256"] or delivery["data"] != {
                "claim_id": review["claim_id"], "header_sha256": digest(packet["header"]), "page_id": descriptor["page_id"],
                "page_sha256": descriptor["sha256"], "delivery_scope": "host_assembly", "worker_consumption": "not_observed"}):
            refuse("REVIEW_DEPENDENCY_ASSEMBLY_PROOF_REQUIRED")
    if set(pages) != {descriptor["page_id"] for descriptor in packet["header"]["pages"]}:
        refuse("REVIEW_DEPENDENCY_ASSEMBLY_PROOF_REQUIRED")
    if purpose == "reuse":
        applied = _one(records, lambda row: row["kind"] == "execution_applied" and row["data"]["attempt_id"] == attempt.attempt_id)
        if applied["data"].get("operation") != "evaluate":
            refuse("REVIEW_DEPENDENCY_APPLICATION_PROOF_REQUIRED")
        _one(records, lambda row: row["kind"] == "execution_native_observation" and row["data"].get("error_code") is None
            and row["data"]["observation"]["attempt_id"] == native["data"]["reservation"]["attempt_id"])
    proof = _Original(holder, inputs, snapshot, parent, records, application["envelope"])
    proof._original_binding = binding
    proof.assert_current()
    return proof
