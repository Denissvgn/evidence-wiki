#!/usr/bin/env python3
"""Coherent, bounded original-byte closures for inert evidence records."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Any

from _evidence_authority import EvidenceInvalid, bounded_list, digest, exact_object
from _evidence_revision import content_id, observation, read_observed_file
from _native_fs import os
from _qualified_packet import IntakeInvalid, open_directory, portable_path, strict_json
from _script_errors import ScriptRefusal

MAX_FILES = 256
MAX_ENTRIES = 512
MAX_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 32


def artifact_path(value: Any) -> str:
    try:
        return portable_path(value)
    except IntakeInvalid as exc:
        raise EvidenceInvalid("unsafe_artifact_path") from exc


def observe_artifacts(root: Path, relative: str) -> dict[str, tuple[int, ...]]:
    observed: dict[str, tuple[int, ...]] = {}
    entries_seen = files_seen = bytes_seen = 0

    def visit(fd: int, prefix: str, depth: int) -> None:
        nonlocal entries_seen, files_seen, bytes_seen
        if depth > MAX_DEPTH:
            raise EvidenceInvalid("artifact_bound_exceeded")
        observed[prefix] = observation(os.fstat(fd))
        names: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_ENTRIES:
                    raise EvidenceInvalid("artifact_bound_exceeded")
                names.append(entry.name)
        folded: set[str] = set()
        for filename in sorted(names):
            artifact_path(filename)
            if filename.casefold() in folded:
                raise EvidenceInvalid("artifact_path_collision")
            folded.add(filename.casefold())
            path = f"{prefix}/{filename}"
            info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(filename, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if observation(os.fstat(child)) != observation(info):
                        raise EvidenceInvalid("artifact_revision_changed")
                    visit(child, path, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                files_seen += 1
                bytes_seen += info.st_size
                if files_seen > MAX_FILES or info.st_size > MAX_FILE_BYTES or bytes_seen > MAX_BYTES:
                    raise EvidenceInvalid("artifact_bound_exceeded")
                observed[path] = observation(info)
            else:
                raise EvidenceInvalid("unsafe_artifact_entry")

    try:
        with open_directory(root, artifact_path(relative)) as fd:
            visit(fd, relative, 0)
    except IntakeInvalid as exc:
        raise EvidenceInvalid("artifact_capture_unsupported") from exc
    return observed


def capture_artifacts(root: Path, relative: str) -> dict[str, bytes]:
    for _attempt in range(3):
        try:
            before = observe_artifacts(root, relative)
            files = {path[len(relative) + 1:]: read_observed_file(root, path, identity)
                     for path, identity in before.items() if stat.S_ISREG(identity[2])}
            if before == observe_artifacts(root, relative):
                return files
        except ScriptRefusal as exc:
            if exc.error_code != "EVIDENCE_REVISION_CHANGED":
                raise EvidenceInvalid("artifact_capture_refused") from exc
        except OSError as exc:
            raise EvidenceInvalid("artifact_unreadable") from exc
    raise EvidenceInvalid("artifact_revision_changed")


def file_binding(data: bytes) -> dict[str, Any]:
    return {"content_hash": "sha256:" + hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def closure_identity(files: dict[str, bytes]) -> str:
    return content_id("evidence-artifact-closure/v1", {path: file_binding(data) for path, data in sorted(files.items())})


def json_document(data: bytes) -> dict[str, Any]:
    try:
        result = strict_json(data)
    except ValueError as exc:
        raise EvidenceInvalid("invalid_evidence_json") from exc
    if not isinstance(result, dict):
        raise EvidenceInvalid("invalid_evidence_document")
    return result


def validate_file_bounds(files: dict[str, bytes]) -> None:
    """Apply the same closure limits to captured and caller-supplied bytes."""
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise EvidenceInvalid("artifact_bound_exceeded")
    total = 0
    entries: set[str] = set()
    folded: dict[str, str] = {}
    for path, data in files.items():
        parts = artifact_path(path).split("/")
        if len(parts) > MAX_DEPTH + 1 or not isinstance(data, bytes) or len(data) > MAX_FILE_BYTES:
            raise EvidenceInvalid("artifact_bound_exceeded")
        total += len(data)
        if total > MAX_BYTES:
            raise EvidenceInvalid("artifact_bound_exceeded")
        for end in range(1, len(parts) + 1):
            entry = "/".join(parts[:end])
            if entry.casefold() in folded and folded[entry.casefold()] != entry:
                raise EvidenceInvalid("artifact_path_collision")
            if end < len(parts) and entry in files:
                raise EvidenceInvalid("artifact_path_collision")
            entries.add(entry)
            folded[entry.casefold()] = entry
            if len(entries) > MAX_ENTRIES:
                raise EvidenceInvalid("artifact_bound_exceeded")


def validate_members(declared: Any, files: dict[str, bytes], record_name: str) -> dict[str, dict[str, Any]]:
    validate_file_bounds(files)
    members: dict[str, dict[str, Any]] = {}
    for member in bounded_list(declared, maximum=MAX_FILES - 1):
        exact_object(member, {"path", "content_hash", "size_bytes", "role"})
        path = artifact_path(member["path"])
        digest(member["content_hash"])
        if type(member["size_bytes"]) is not int or not 0 <= member["size_bytes"] <= MAX_FILE_BYTES:
            raise EvidenceInvalid("invalid_artifact_size")
        if member["role"] not in {"input", "patch", "environment", "dependency", "tool", "suite", "log", "output", "context", "context-packet", "provenance"}:
            raise EvidenceInvalid("invalid_artifact_role")
        if path == record_name or path in members or path not in files:
            raise EvidenceInvalid("artifact_membership_mismatch")
        if file_binding(files[path]) != {key: member[key] for key in ("content_hash", "size_bytes")}:
            raise EvidenceInvalid("artifact_content_mismatch")
        members[path] = member
    if set(files) != {record_name, *members}:
        raise EvidenceInvalid("artifact_closure_incomplete")
    return members
