"""Caller-local draft containers and immutable records outside distributable trees."""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path

from ._filesystem import os
from ._pack_io import canonical, capture_pack, identity, read_file, relative_path
from .pack_authoring_contracts import DRAFT, MAX_BYTES, checked, decode, digest, refuse
from .pack_catalog import _outside_assets, _writer_flags
from .pack_discovery import owner


def protect(path):
    from .source_commands import _no_secret_values

    if isinstance(path, Path):
        _no_secret_values(str(path))
    _outside_assets(path, additional_roots=(Path(__file__).parent,))
    if isinstance(path, Path):
        for ancestor in path.parents:
            if (ancestor / "workspace-system.yml").is_file() and (ancestor / "research.yml").is_file():
                if (ancestor / "domain-packs") in path.parents or path == ancestor / "domain-packs":
                    refuse("authoring_installed_workspace_pack_forbidden")
                break


def publish(directory, name, raw):
    """Publish immutable bytes atomically, with exact replay and no replacement."""
    relative_path(name)
    if "/" in name or len(raw) > MAX_BYTES:
        refuse("authoring_record_bound")
    protect(directory)
    temporary = ".authoring-" + uuid.uuid4().hex
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            if read_file(directory, name) != raw:
                refuse("authoring_immutable_record_conflict", "ONBOARDING_TARGET_CONFLICT")
        os.fsync(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def child(root, relative):
    descriptor = os.dup(root)
    try:
        for part in relative_path(relative).split("/"):
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            nested = os.open(part, _writer_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = nested
        protect(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create(output, name, files, draft):
    draft = checked(draft, DRAFT)
    target = Path(output).expanduser().absolute()
    protect(target)
    parent = target.parent.resolve(strict=True)
    target = parent / target.name
    relative_path(target.name)
    parent_id = identity(parent)
    parent_fd = os.open(parent, _writer_flags())
    descriptor = None
    try:
        protect(parent_fd)
        held_parent = os.fstat(parent_fd)
        if {"device": str(held_parent.st_dev), "inode": str(held_parent.st_ino)} != parent_id:
            refuse("authoring_parent_changed", "ONBOARDING_PLAN_STALE")
        try:
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            refuse("authoring_output_exists", "ONBOARDING_TARGET_CONFLICT")
        descriptor = os.open(target.name, _writer_flags(), dir_fd=parent_fd)
        held_target = os.fstat(descriptor)
        target_id = {"device": str(held_target.st_dev), "inode": str(held_target.st_ino)}
        if identity(target) != target_id:
            refuse("authoring_output_changed", "ONBOARDING_PLAN_STALE")
        publish(descriptor, "authoring.lock", b"")
        records = child(descriptor, "records")
        try:
            publish(records, "specification.json", canonical(draft["specification"]))
            for relative, raw in sorted(files.items()):
                path = Path("packs") / name / relative
                parent_child = child(descriptor, path.parent.as_posix())
                try:
                    publish(parent_child, path.name, raw)
                finally:
                    os.close(parent_child)
            observed = capture_pack(target / "packs" / name)
            if observed.tree_sha256 != draft["initial_tree_sha256"]:
                refuse("authoring_candidate_changed", "ONBOARDING_PLAN_STALE")
            if identity(parent) != parent_id or identity(target) != target_id:
                refuse("authoring_output_changed", "ONBOARDING_PLAN_STALE")
            publish(records, "draft.json", canonical(draft))
        finally:
            os.close(records)
        return {"schema_version": "evidence-pack-authoring-result/v1", "status": "draft_created", "root": str(target),
            "candidate": str(target / "packs" / name), "draft_id": digest(draft), "identity": {"tree_sha256": observed.tree_sha256},
            "unresolved": draft["unresolved"], "validation": "canonical_preflight_passed_receipt_not_recorded", "semantic_adequacy": "not_certified", "workspace_changed": False}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def load_draft(root):
    root = Path(root).expanduser().absolute()
    root = root.parent.resolve(strict=True) / root.name
    protect(root)
    draft = decode(read_file(root, "records/draft.json"), DRAFT)
    spec = read_file(root, "records/specification.json")
    if digest(draft["specification"]) != draft["specification_sha256"] or spec != canonical(draft["specification"]):
        refuse("authoring_specification_changed", "ONBOARDING_PLAN_STALE")
    if draft["pack_relative"] != "packs/" + draft["name"]:
        refuse("authoring_candidate_path_invalid")
    candidate = root / draft["pack_relative"]
    if candidate.parent.is_symlink() or candidate.is_symlink():
        refuse("authoring_candidate_symlink")
    return root, candidate, draft


def record(root, name, value):
    root, _candidate, draft = load_draft(root)
    descriptor = os.open(root, _writer_flags())
    lock = None
    try:
        lock = os.open("authoring.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        before = identity(root)
        held = os.fstat(descriptor)
        if before != {"device": str(held.st_dev), "inode": str(held.st_ino)}:
            refuse("authoring_root_changed", "ONBOARDING_PLAN_STALE")
        with owner("_workspace_locks").descriptor_lock(lock):
            if read_file(descriptor, "records/draft.json") != canonical(draft):
                refuse("authoring_draft_changed", "ONBOARDING_PLAN_STALE")
            observed = os.stat("authoring.lock", dir_fd=descriptor, follow_symlinks=False)
            held = os.fstat(lock)
            if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
                refuse("authoring_lock_changed")
            records = os.open("records", _writer_flags(), dir_fd=descriptor)
            try:
                publish(records, name, canonical(value))
            finally:
                os.close(records)
            if identity(root) != before:
                refuse("authoring_root_changed", "ONBOARDING_PLAN_STALE")
    finally:
        if lock is not None:
            os.close(lock)
        os.close(descriptor)
    return {"path": str(root / "records" / name), "sha256": digest(value)}
