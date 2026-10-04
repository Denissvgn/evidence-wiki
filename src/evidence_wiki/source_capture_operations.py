"""Native immutable capture publication and explicit signed protected deposition."""

from __future__ import annotations

import hashlib
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ._filesystem import os
from ._pack_io import canonical, relative_path
from .host_context import _require_mutation_workspace_gate, current_context
from .mutation_contracts import MutationError, digest, refuse
from .mutation_store import MutationStore
from .pack_discovery import owner
from .source_capture_contracts import metadata, profile, read_input, validate
from .source_capture_inputs import FORMATS, CaptureInput
from .source_inputs import SourceView

OPERATION = "sources.capture_revision"
GRANT = "source_capture_operations.deliver"
REPAIR_GRANT = "source_capture_operations.resume"
RESULT = "evidence-host-delivery-result/v2"


@contextmanager
def acquisition_barrier(root, context):
    """Hold the existing native raw barrier with exact live namespace custody."""
    from .source_delivery import _directory

    if (not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd):
        refuse("CAPTURE_PLATFORM_UNSUPPORTED", "/capture/barrier")
    locks = owner("_workspace_locks")
    with ExitStack() as custody:
        try:
            context.require_path(root / "raw/.locks", write=True)
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            custody.callback(os.close, directory)
            raw_directory = _directory(directory, "raw")
            custody.callback(os.close, raw_directory)
            lock_directory = _directory(raw_directory, ".locks")
            custody.callback(os.close, lock_directory)
            descriptor = os.open("acquisition.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600, dir_fd=lock_directory)
            custody.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
                refuse("CAPTURE_BARRIER_UNSAFE", "/capture/barrier")
            custody.enter_context(locks.descriptor_lock(descriptor, timeout_seconds=0))
        except locks.LockUnavailableError as error:
            reason = "CONTENDED" if error.contended else "UNAVAILABLE"
            raise MutationError("MUTATION_CAPTURE_ACQUISITION_" + reason,
                "Native acquisition barrier refused.", recoverable=error.contended,
                remediation="Inspect retained capture state after the raw writer finishes; prepared work is not automatically redispatched.",
                details={"field": "/capture/barrier"}) from None
        except OSError:
            refuse("CAPTURE_BARRIER_UNAVAILABLE", "/capture/barrier")

        def check():
            context.require_path(root, write=True)
            try:
                named_root, held_root = os.stat(root, follow_symlinks=False), os.fstat(directory)
                if (named_root.st_dev, named_root.st_ino) != (held_root.st_dev, held_root.st_ino):
                    refuse("CAPTURE_BARRIER_CHANGED", "/capture/barrier")
                for parent, name, held in ((directory, "raw", raw_directory),
                        (raw_directory, ".locks", lock_directory), (lock_directory, "acquisition.lock", descriptor)):
                    named, live = os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(held)
                    if (named.st_dev, named.st_ino) != (live.st_dev, live.st_ino):
                        refuse("CAPTURE_BARRIER_CHANGED", "/capture/barrier")
                lock = os.fstat(descriptor)
                if not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1 or lock.st_size != 0:
                    refuse("CAPTURE_BARRIER_UNSAFE", "/capture/barrier")
            except OSError:
                refuse("CAPTURE_BARRIER_CHANGED", "/capture/barrier")
        check()
        yield check
        check()


def file_hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _host(root, path, delivery, *, gate=True, ordinary=True):
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("AUTHORITY_REQUIRED", "/host_context")
    root = context.require_path(Path(root), write=True).resolve(strict=True)
    context.require_grant(GRANT)
    relative_path(path)
    delivery = validate(delivery)
    supplied = metadata(delivery["capture"])
    if (not path.startswith("raw/") or path.startswith("raw/links/")
            or Path(path).suffix.lower() != FORMATS[supplied["content_format"]][0]):
        refuse("INPUT_UNBOUND", "/capture/path")
    context.require_path(root / path, write=True)
    context.require_path(root / (path + ".provenance.yml"), write=True)
    if gate:
        _require_mutation_workspace_gate(root)
    view = SourceView(root)
    roots = view.config.get("raw", {}).get("source_roots", [])
    if view.workspace != "present" or type(roots) is not list or not any(type(item) is str and path.startswith(item.rstrip("/") + "/") for item in roots):
        refuse("INPUT_UNBOUND", "/capture/raw_roots")
    if ordinary:
        if supplied["parents"] or supplied["sanitization"]["required"] or supplied["sanitization"]["approved_revision_id"] is not None:
            refuse("CAPTURE_PROTECTED_DEPOSIT_REQUIRED", "/capture/ancestry")
        owner("_usage_gate").require_host_intake(view.config, [])
    orchestration = owner("_orchestration_config")
    delegated = orchestration.is_delegated(orchestration.orchestration_config(view.config))
    owner("_delegation_gate").require_sanctioned_mutation(root, delegated,
        request_id=supplied["request_id"], error_code="ONBOARDING_OWNERSHIP_CONFLICT",
        subject="Host capture revision", remediation="Use the current acquisition order and delivery scope.")
    return root, context, view


def provenance(delivery):
    return provenance_profile(validate(delivery)["capture"])


def provenance_profile(captured):
    metadata(captured)
    result = {"host_capture": captured, "checksum": captured["content_sha256"], "origin_url": captured["origin_url"],
        "retrieved_at": captured["retrieved_at"], "retrieved_by": "host:" + captured["tool_id"], "title": captured["title"],
        "source_type": "host_capture_v2", "license": captured["rights"]["license"], "terms_note": captured["rights"]["note"]}
    for key, value in (("scope", captured["scope"]), ("request_id", captured["request_id"]), ("terms_url", captured["rights"]["terms_url"])):
        if value:
            result[key] = value
    return result


def expected_result(path, delivery, files, *, status="delivered"):
    profile = delivery["capture"]
    return {"schema_version": RESULT, "status": status, "path": path,
        "source_id": owner("source_inventory").stable_id(path), "capture_id": profile["capture_id"],
        "content_sha256": profile["content_sha256"], "content_bytes": profile["content_bytes"],
        "artifact_revision": owner("_record_artifacts").closure_identity(files),
        "inventory": "not_run", "extraction": "not_run", "research_ready": False,
        "provider_enabled": False, "request_fulfilled": False, "authority": "caller_declared_capture_with_checked_bytes",
        "authorized": False, "evidence_accepted": False}


def prepare_effects(root, intent):
    values = intent["arguments"]
    root, context, view = _host(root, values["path"], values["delivery"])
    path, delivery = values["path"], values["delivery"]
    original, sidecar = view.read(path, 16_777_216), view.read(path + ".provenance.yml")
    if (original is not None or sidecar is not None) and (original is None or sidecar is None
            or file_hash(original) != delivery["capture"]["content_sha256"]
            or len(original) != delivery["capture"]["content_bytes"] or sidecar != canonical(provenance(delivery))):
        refuse("CAPTURE_TARGET_CONFLICT", "/capture/outputs")
    view.finish()
    with acquisition_barrier(root, context) as check:
        result = _prepare_effects(root, intent, view)
        check()
        return result


def _prepare_effects(root, intent, view):
    values = intent["arguments"]
    candidate = read_input(values["delivery"])
    path = values["path"]
    files = {path: candidate.content, path + ".provenance.yml": canonical(provenance(values["delivery"]))}
    capture = owner("_evidence_revision").capture_workspace(root)
    present = {name for name in files if name in capture.files}
    if present and (present != set(files) or any(capture.files[name] != raw for name, raw in files.items())):
        refuse("CAPTURE_TARGET_CONFLICT", "/capture/outputs")
    new_directories = {parent.as_posix() for name in files for parent in Path(name).parents
        if parent.as_posix() != "." and parent.as_posix() not in capture.directories}
    projected_files = {name: len(raw) for name, raw in capture.files.items()}
    projected_files.update({name: len(raw) for name, raw in files.items()})
    changed = [raw for name, raw in files.items() if capture.files.get(name) != raw]
    temporary_count = 1 if changed else 0
    revision = owner("_evidence_revision")
    if (len(projected_files) + temporary_count > revision.MAX_FILES
            or len(projected_files) + len(set(capture.directories) | new_directories) + temporary_count > revision.MAX_ENTRIES
            or sum(projected_files.values()) + max(map(len, changed), default=0) > revision.MAX_BYTES
            or any(len(Path(name).parts) - 1 > revision.MAX_DEPTH for name in files)):
        refuse("LIMIT", "/capture/projected_tree")
    view.finish()
    return {"schema_version": "research-capture-effect-plan/v1", "request_id": intent["request_id"],
        "input_sha256": intent["input_sha256"], "workspace_binding": intent["workspace_binding"],
        "configuration_sha256": digest(view.config), "delivery_sha256": digest(values["delivery"]),
        "path": path, "outputs": [{"path": name, "prior_sha256": file_hash(capture.files[name]) if name in capture.files else None,
            "data": raw} for name, raw in files.items()],
        "dependencies": [{"path": name, "sha256": file_hash(raw)} for name, raw in sorted(capture.files.items()) if name not in files],
        "directories": list(capture.directories),
        "new_directories": sorted(new_directories),
        "result": expected_result(path, values["delivery"], files, status="already_present" if present else "delivered")}


def _check_tree(root, plan, *, temporary=None):
    capture = owner("_evidence_revision").capture_workspace(root)
    outputs = {row["path"]: row for row in plan["outputs"]}
    dependencies = {row["path"]: row["sha256"] for row in plan["dependencies"]}
    if any(file_hash(capture.files[name]) != sha if name in capture.files else True for name, sha in dependencies.items()):
        refuse("CAPTURE_INPUTS_CHANGED", "/capture/dependencies")
    extras = set(capture.files) - set(outputs) - set(dependencies)
    if temporary is not None:
        selected = {name for name in extras if Path(name).parent == Path(temporary["path"]).parent
            and Path(name).name.startswith(".normalized-") and len(Path(name).name) == 44 and capture.files[name] == temporary["data"]}
        if len(selected) != 1:
            refuse("CAPTURE_TEMPORARY_CHANGED", "/capture/temporary")
        extras -= selected
    if extras or not set(plan["directories"]) <= set(capture.directories) or set(capture.directories) - set(plan["directories"]) - set(plan["new_directories"]):
        refuse("CAPTURE_UNDECLARED_INPUTS", "/capture/tree")
    for name, row in outputs.items():
        if name in capture.files and capture.files[name] != row["data"]:
            refuse("CAPTURE_OUTPUT_CONFLICT", "/capture/output")
        if name not in capture.files and row["prior_sha256"] is not None:
            refuse("CAPTURE_OUTPUT_MISSING", "/capture/output")
    return capture


def deliver(root, delivery, *, path, _store=None, _basis=None, _recovery=False):
    root, context, _ = _host(root, path, delivery)
    if type(_store) is not MutationStore or not _store._active or _store._historical_readonly or _store.workspace_root != root or _store.control_root != context.mutation_control_directory.resolve(strict=True):
        refuse("CAPTURE_STAGED_GATE_REQUIRED", "/capture/store")
    if _recovery is True:
        context.require_grant(REPAIR_GRANT)
        context.require_grant("mutation_operations.reconcile")
    _store._require_locked()
    intent, state = _store._read("intent.json"), _store.read()
    if (intent is None or intent["operation"] != OPERATION or intent["arguments"] != {"path": path, "delivery": delivery}
            or state is None or state["owner_basis"] != _basis
            or state["status"] not in ({"partial", "commit_unknown"} if _recovery is True else {"executing"})):
        refuse("CAPTURE_STAGED_BINDING_CHANGED", "/capture/state")
    from .mutation_capture_recovery import load_plan
    plan = load_plan(root, intent, _basis, _store)
    with acquisition_barrier(root, context) as check:
        _check_tree(root, plan)
        writer = owner("_usage_materialization")
        def before_publish(row):
            _host(root, path, delivery)
            _store._require_locked()
            check()
            _check_tree(root, plan, temporary=row)
        for row in reversed(plan["outputs"]):
            check()
            context.require_path(root / row["path"], write=True)
            writer.publish_file(root, row["path"], row["data"], row["prior_sha256"], before_publish=lambda row=row: before_publish(row))
        capture = _check_tree(root, plan)
        if any(capture.files.get(row["path"]) != row["data"] for row in plan["outputs"]):
            refuse("CAPTURE_POSTCONDITION_MISSING", "/capture/output")
        return plan["result"]


def protected_artifacts(raw, supplied, *, path):
    """Build inert exact artifacts for independent host authorization and scrub."""
    supplied = CaptureInput(raw, supplied).metadata()
    # Native signed grant/scrub envelopes live outside their own artifact hash.
    supplied["sanitization"]["approved_revision_id"] = None
    supplied["sanitization"]["receipt_ref"] = None
    candidate = CaptureInput(raw, supplied)
    relative_path(path)
    source_id = owner("source_inventory").stable_id(path)
    files = {"evidence/" + Path(path).name: raw, "capture-provenance.json": canonical(provenance_profile(profile(candidate)))}
    files["source-record.json"] = canonical({"schema_version": "evidence-source-revision/v1", "source_id": source_id,
        "parents": supplied["parents"], "normalized_path": None, "evidence_root": "evidence", "temporal": {"retrieved_at": supplied["retrieved_at"]}})
    return files


def deposit_protected(root, delivery, envelope, *, path):
    """Reuse actual signed usage deposition; this function creates no grant or scrub."""
    root, context, _ = _host(root, path, delivery, gate=False, ordinary=False)
    context.require_grant("evidence_usage.run_transact")
    candidate = read_input(delivery)
    files = protected_artifacts(candidate.content, candidate.metadata(), path=path)
    revision = owner("_record_artifacts").closure_identity(files)
    supplied = candidate.metadata()
    if supplied["sanitization"]["approved_revision_id"] != revision or supplied["sanitization"]["receipt_ref"] != envelope.get("payload", {}).get("request_id"):
        refuse("CAPTURE_SANITIZATION_BINDING_CHANGED", "/capture/sanitization")
    if envelope.get("payload", {}).get("action") != "deposit":
        refuse("CAPTURE_DEPOSIT_REQUIRED", "/capture/envelope")
    from .workspace import Workspace
    with Workspace.open(root) as workspace:
        receipt = workspace.usage.transact(envelope, artifacts=files)
    return {"schema_version": RESULT, "status": "protected_deposited", "source_id": owner("source_inventory").stable_id(path),
        "artifact_revision": revision, "receipt": receipt, "raw_workspace_publication": "not_run",
        "inventory": "not_run", "extraction": "not_run", "research_ready": False, "evidence_accepted": False}
