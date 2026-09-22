"""Explicit immutable host capture delivery through existing intake boundaries."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import uuid
from pathlib import Path

from ._pack_io import canonical, read_file, relative_path
from .host_capabilities import known_credentials, read_tools, scope_allows
from .pack_catalog import _outside_assets
from .pack_discovery import owner
from .source_contracts import DELIVERY, decode, refuse, reject_execution_and_secrets
from .source_inputs import SourceView


def _directory(parent, relative):
    current = os.dup(parent)
    try:
        for part in relative.split("/"):
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _publish(directory, name, raw):
    temporary = ".host-capture-" + uuid.uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _check_lock(directory, descriptor):
    current = os.stat("acquisition.lock", dir_fd=directory, follow_symlinks=False)
    held = os.fstat(descriptor)
    if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        refuse("capture_lock_replaced", "ONBOARDING_OWNERSHIP_CONFLICT")


def deliver(raw, *, target, path, host_tools=None):
    value = decode(DELIVERY, raw)
    capture = owner("_host_capture")
    profile = capture.validate(value["capture"])
    reject_execution_and_secrets(profile, credential_values=known_credentials())
    content = base64.b64decode(value["content_base64"], validate=True)
    if (len(content) != profile["content_bytes"] or len(content) > capture.MAX_BYTES
            or "sha256:" + hashlib.sha256(content).hexdigest() != profile["content_sha256"]):
        refuse("capture_content_identity_mismatch")
    decoded = content.decode("utf-8")
    if "\x00" in decoded:
        refuse("capture_binary_content")
    relative_path(path)
    if not path.startswith("raw/") or path.startswith("raw/links/") or Path(path).suffix.lower() not in capture.SUFFIXES:
        refuse("capture_target_invalid")
    if (profile["content_format"] == "plain_text") != (Path(path).suffix.lower() == ".txt"):
        refuse("capture_format_mismatch")
    tools = read_tools(host_tools)
    if host_tools is not None:
        matches = [item for item in tools if item["id"] == profile["tool_id"] and item["version"] == profile["tool_version"]]
        if (len(matches) != 1 or not scope_allows(matches[0], profile["origin_url"])
                or not {"capture", "export"}.intersection(matches[0]["operations"])
                or profile["content_format"] not in matches[0]["formats"] or len(content) > matches[0]["limits"]["max_bytes"]
                or matches[0]["authorization"] != "declared"):
            refuse("capture_outside_declared_host_scope")
    view = SourceView(target)
    if view.workspace != "present":
        refuse("capture_workspace_required")
    roots = view.config.get("raw", {}).get("source_roots", [])
    if not isinstance(roots, list) or not any(isinstance(root, str) and path.startswith(root.rstrip("/") + "/") for root in roots):
        refuse("capture_outside_raw_roots")
    sidecar = {"host_capture": profile, "checksum": profile["content_sha256"], "origin_url": profile["origin_url"],
               "retrieved_at": profile["retrieved_at"], "retrieved_by": "host:" + profile["tool_id"],
               "title": profile["title"], "source_type": "host_text_capture", "license": profile["rights"]["license"],
               "terms_note": profile["rights"]["note"]}
    for key, item in (("scope", profile["scope"]), ("request_id", profile["request_id"]), ("terms_url", profile["rights"]["terms_url"])):
        if item:
            sidecar[key] = item
    record = {"kind": "host_capture", "raw_paths": [path], "provenance": sidecar}
    owner("_usage_gate").require_host_intake(view.config, [record])
    orchestration = owner("_orchestration_config")
    delegated = orchestration.is_delegated(orchestration.orchestration_config(view.config))
    owner("_delegation_gate").require_sanctioned_mutation(view.root, delegated, request_id=profile["request_id"],
        error_code="ONBOARDING_OWNERSHIP_CONFLICT", subject="Host capture delivery",
        remediation="Use the current acquisition order's request and delivery scope.")
    if os.name != "posix" or os.link not in os.supports_dir_fd:
        refuse("capture_write_platform_unsupported", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
    directory = os.open(view.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptors = [directory]
    try:
        _outside_assets(directory)
        observed = os.fstat(directory)
        if view.root_identity != {"device": str(observed.st_dev), "inode": str(observed.st_ino)}:
            refuse("capture_target_changed", "ONBOARDING_PLAN_STALE")
        lock_directory = _directory(directory, "raw/.locks")
        descriptors.append(lock_directory)
        try:
            lock = os.open("acquisition.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=lock_directory)
        except FileExistsError:
            lock = os.open("acquisition.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=lock_directory)
        descriptors.append(lock)
        with owner("_workspace_locks").descriptor_lock(lock):
            view.finish()
            _check_lock(lock_directory, lock)
            parent = _directory(directory, str(Path(path).parent))
            descriptors.append(parent)
            sidecar_name = Path(path).name + ".provenance.yml"
            metadata_bytes = canonical(sidecar)
            present = []
            for name in (Path(path).name, sidecar_name):
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                    present.append(name)
                except FileNotFoundError:
                    pass
            if present:
                if (len(present) != 2 or read_file(parent, Path(path).name) != content
                        or read_file(parent, sidecar_name) != metadata_bytes):
                    refuse("capture_target_exists_or_incomplete", "ONBOARDING_TARGET_CONFLICT")
                status = "already_present"
            else:
                _publish(parent, Path(path).name, content)
                _publish(parent, sidecar_name, metadata_bytes)
                status = "delivered"
            _check_lock(lock_directory, lock)
            capture.inspect_record(view.root, record)
            view.finish()
            return {"schema_version": "evidence-host-delivery-result/v1", "status": status, "path": path,
                "source_id": owner("source_inventory").stable_id(path), "capture_id": profile["capture_id"],
                "content_sha256": profile["content_sha256"], "content_bytes": len(content),
                "inventory": "not_run", "extraction": "not_run", "research_ready": False,
                "provider_enabled": False, "request_fulfilled": False, "authority": "caller_declared_capture_with_checked_bytes"}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def deliver_local(*, target, input, path, source_id, question_ids):
    """Copy a selected observed original, preserving bytes and local provenance."""
    from ._pack_io import identity
    from .planning_contracts import digest

    relative_path(path)
    view = SourceView(target)
    roots = view.config.get("raw", {}).get("source_roots", [])
    if (view.workspace != "present" or not any(path.startswith(root.rstrip("/") + "/") for root in roots)
            or path.startswith("raw/links/") or input["state"] != "present"):
        refuse("local_delivery_scope_invalid")
    root = Path(input["root"])
    if identity(root) != input["root_identity"]:
        refuse("local_delivery_root_changed", "ONBOARDING_PLAN_STALE")
    raw = read_file(root, input["path"], 16_777_216)
    if len(raw) != input["bytes"] or hashlib.sha256(raw).hexdigest() != input["sha256"]:
        refuse("local_delivery_input_changed", "ONBOARDING_PLAN_STALE")
    sidecar = {"checksum": "sha256:" + input["sha256"], "retrieved_by": "local_setup",
               "title": Path(input["path"]).name, "source_type": "local_file",
               "setup_input": {"source_id": source_id, "question_ids": question_ids,
                               "input_identity": digest(input), "origin": str(root / input["path"])},
               "terms_note": "Caller-selected local original; rights and semantic adequacy are not verified."}
    record = {"raw_paths": [path], "provenance": sidecar}
    owner("_usage_gate").require_host_intake(view.config, [record])
    directory = os.open(view.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptors = [directory]
    try:
        _outside_assets(directory)
        if identity(directory) != view.root_identity:
            refuse("local_delivery_target_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
        locks = _directory(directory, "raw/.locks")
        descriptors.append(locks)
        lock = os.open("acquisition.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=locks)
        descriptors.append(lock)
        with owner("_workspace_locks").descriptor_lock(lock, timeout_seconds=0):
            _check_lock(locks, lock)
            view.finish()
            parent = _directory(directory, str(Path(path).parent))
            descriptors.append(parent)
            name = Path(path).name
            # No adoption of an unexplained existing pair, even with identical bytes.
            _publish(parent, name, raw)
            _publish(parent, name + ".provenance.yml", canonical(sidecar))
            _check_lock(locks, lock)
            view.finish()
            if read_file(root, input["path"], 16_777_216) != raw or identity(root) != input["root_identity"]:
                refuse("local_delivery_input_changed", "ONBOARDING_PLAN_STALE")
        return {"source_id": source_id, "question_ids": question_ids, "path": path,
                "sha256": input["sha256"], "bytes": len(raw), "provenance": "caller_selected_local_original"}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
