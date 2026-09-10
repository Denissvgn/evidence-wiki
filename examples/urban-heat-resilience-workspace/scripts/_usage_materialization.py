#!/usr/bin/env python3
"""Materialize exact host-approved normalized bytes through anchored directories."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from _evidence_authority import EvidenceInvalid, digest
from _evidence_revision import observation
from _evidence_usage import current_view
from _record_artifacts import artifact_path
from _usage_gate import normalized_relative


def file_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def materialize(root: Path, config: dict[str, Any], revision: str,
                expected_content_hash: str | None = None) -> dict[str, Any]:
    digest(revision)
    if expected_content_hash is not None:
        digest(expected_content_hash)
    with current_view(root, config, exclusive=True) as view:
        verdict = view.check(revision, uses=["retrieval"], purpose="research", consumer="evidence-wiki")
        if not verdict["eligible"]:
            raise EvidenceInvalid(verdict["reasons"][0])
        record = view.state.revisions.get(revision)
        if record is None or record["descriptor"]["normalized_path"] is None:
            raise EvidenceInvalid("normalized_revision_missing")
        data = record["files"][record["descriptor"]["normalized_path"]]
        try:
            text = data.decode("utf-8")
            pieces = text.split("---", 2)
            frontmatter = yaml.safe_load(pieces[1]) if len(pieces) == 3 and pieces[0] == "" else None
        except (ValueError, yaml.YAMLError) as exc:
            raise EvidenceInvalid("normalized_revision_frontmatter_invalid") from exc
        if not isinstance(frontmatter, dict) or frontmatter.get("source_id") != record["source_id"]:
            raise EvidenceInvalid("normalized_revision_source_mismatch")
        relative = normalized_relative(config, record["source_id"])
        if not relative.startswith("sources/"):
            raise EvidenceInvalid("normalized_destination_outside_sources")
        changed = publish_file(root, relative, data, expected_content_hash,
                               before_publish=lambda: view.revalidate(root, config))
        return {"schema_version": "evidence-usage-materialization/v1", "source_revision": revision,
                "path": relative, "content_hash": file_digest(data), "changed": changed,
                "checkpoint": view.state.checkpoint, "usage": verdict}


def publish_file(root: Path, relative: str, data: bytes, expected: str | None, *,
                 before_publish: Callable[[], None] | None = None) -> bool:
    artifact_path(relative)
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd or os.rename not in os.supports_dir_fd:
        raise EvidenceInvalid("usage_materialization_unsupported")
    descriptors = []
    temporary = None
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        parts = Path(relative).parts
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        try:
            previous = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1 or previous.st_size > 16 * 1024 * 1024:
                raise EvidenceInvalid("unsafe_normalized_destination")
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            try:
                if observation(os.fstat(descriptor)) != observation(previous):
                    raise EvidenceInvalid("normalized_destination_changed")
                old = bytearray()
                while chunk := os.read(descriptor, 65536):
                    old.extend(chunk)
                    if len(old) > 16 * 1024 * 1024:
                        raise EvidenceInvalid("normalized_destination_changed")
                if observation(os.fstat(descriptor)) != observation(previous):
                    raise EvidenceInvalid("normalized_destination_changed")
            finally:
                os.close(descriptor)
            check_destination(root, descriptors, parts, previous)
            if bytes(old) == data:
                return False
            if expected is None or file_digest(bytes(old)) != expected:
                raise EvidenceInvalid("normalized_destination_conflict")
        elif expected is not None:
            raise EvidenceInvalid("normalized_destination_conflict")
        temporary = ".normalized-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=current)
        try:
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:offset + 65536])
                if written <= 0:
                    raise EvidenceInvalid("normalized_write_incomplete")
                offset += written
            os.fsync(descriptor)
            published = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        check_destination(root, descriptors, parts, previous)
        if before_publish is not None:
            before_publish()
        os.rename(temporary, parts[-1], src_dir_fd=current, dst_dir_fd=current)
        temporary = None
        os.fsync(current)
        latest = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns, latest.st_mode, latest.st_nlink) != (
            published.st_dev, published.st_ino, published.st_size, published.st_mtime_ns, published.st_mode, published.st_nlink
        ):
            raise EvidenceInvalid("normalized_destination_changed")
        check_destination(root, descriptors, parts, latest)
        return True
    except OSError as exc:
        raise EvidenceInvalid("usage_materialization_failed") from exc
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=descriptors[-1])
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def check_destination(root: Path, descriptors: list[int], parts: tuple[str, ...], previous: os.stat_result | None) -> None:
    try:
        latest = os.stat(parts[-1], dir_fd=descriptors[-1], follow_symlinks=False)
    except FileNotFoundError:
        latest = None
    if (None if latest is None else observation(latest)) != (None if previous is None else observation(previous)):
        raise EvidenceInvalid("normalized_destination_changed")
    if observation(os.fstat(descriptors[0]))[:2] != observation(root.lstat())[:2]:
        raise EvidenceInvalid("normalized_destination_changed")
    for parent, child, part in zip(descriptors[:-1], descriptors[1:], parts[:-1], strict=True):
        if observation(os.fstat(child))[:2] != observation(os.stat(part, dir_fd=parent, follow_symlinks=False))[:2]:
            raise EvidenceInvalid("normalized_destination_changed")
