"""Bounded, byte-addressed workspace captures for local evidence operations.

Readers evaluate a private materialization of one validated capture. No script
from that materialization is executed. File and directory observations bracket
the read, including ctime/inode identity, so concurrent edits are retried rather
than splicing independently read inputs into a purported revision.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from _script_errors import ScriptRefusal

SCHEMA_VERSION = "evidence-workspace-revision/v1"
MAX_FILES = 10_000
MAX_ENTRIES = 20_000
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ATTEMPTS = 3
MAX_DEPTH = 64
EXCLUDED_ROOTS = frozenset({".git", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"})


def refuse(code: str, message: str, **details: Any) -> ScriptRefusal:
    return ScriptRefusal(
        code,
        message,
        exit_code=2,
        recoverable=code == "EVIDENCE_REVISION_CHANGED",
        remediation=(
            "Retry after workspace writers finish."
            if code == "EVIDENCE_REVISION_CHANGED"
            else "Use a bounded workspace of local regular files and retry with its current revision."
        ),
        details=details,
    )


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def content_id(domain: str, value: Any) -> str:
    return "sha256:" + hashlib.sha256(domain.encode("utf-8") + b"\x00" + canonical_bytes(value)).hexdigest()


def observation(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def excluded(relative: str) -> bool:
    parts = Path(relative).parts
    return bool(parts and (parts[0] in EXCLUDED_ROOTS or "__pycache__" in parts))


def observe_tree(root: Path) -> dict[str, tuple[int, ...]]:
    """Record directory membership and regular file identities without following links."""
    result: dict[str, tuple[int, ...]] = {}
    pending = [(root, "", 0)]
    file_count = 0
    byte_count = 0
    entry_count = 0
    while pending:
        directory, prefix, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise refuse("EVIDENCE_REVISION_LIMIT", "Workspace nesting exceeds the capture bound.", max_depth=MAX_DEPTH)
        directory_info = directory.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or directory.is_symlink():
            raise refuse("EVIDENCE_REVISION_UNSAFE", "Capture cannot traverse a linked directory.", path=prefix or ".")
        result[prefix or "."] = observation(directory_info)
        with os.scandir(directory) as entries:
            names = []
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_ENTRIES:
                    raise refuse("EVIDENCE_REVISION_LIMIT", "Workspace has too many directory entries.", max_entries=MAX_ENTRIES)
                names.append(entry.name)
            names.sort()
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            if excluded(relative):
                continue
            if any(character in name for character in '\\:<>"|?*') or name.endswith((".", " ")) or any(ord(character) < 32 for character in name):
                raise refuse("EVIDENCE_REVISION_UNSAFE", "Capture path is not portable.", path=relative)
            path = directory / name
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append((path, relative, depth + 1))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                file_count += 1
                byte_count += info.st_size
                if file_count > MAX_FILES or byte_count > MAX_BYTES or info.st_size > MAX_FILE_BYTES:
                    raise refuse(
                        "EVIDENCE_REVISION_LIMIT", "Workspace exceeds the capture bound.",
                        max_files=MAX_FILES, max_bytes=MAX_BYTES, max_file_bytes=MAX_FILE_BYTES,
                    )
                result[relative] = observation(info)
            else:
                raise refuse("EVIDENCE_REVISION_UNSAFE", "Capture requires unlinked regular files.", path=relative)
    return result


def read_observed_file(root: Path, relative: str, expected: tuple[int, ...]) -> bytes:
    """Read through no-follow directory descriptors where the platform supports them."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise refuse(
            "EVIDENCE_REVISION_UNSUPPORTED",
            "This platform lacks the no-follow descriptor operations required for a coherent capture.",
        )
    descriptors: list[int] = []
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open(root, directory_flags)
        descriptors.append(current)
        parts = Path(relative).parts
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
        descriptors.append(descriptor)
        if observation(os.fstat(descriptor)) != expected:
            raise refuse("EVIDENCE_REVISION_CHANGED", "Workspace changed during capture.", path=relative)
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, MAX_FILE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise refuse("EVIDENCE_REVISION_LIMIT", "A file grew beyond the capture bound.", path=relative)
        if observation(os.fstat(descriptor)) != expected or size != expected[4]:
            raise refuse("EVIDENCE_REVISION_CHANGED", "Workspace changed during capture.", path=relative)
        return b"".join(chunks)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@dataclass(frozen=True)
class WorkspaceRevision:
    """Immutable captured bytes and the canonical input identity they establish."""

    files: Mapping[str, bytes]
    directories: tuple[str, ...]

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "files": [
                {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                for name, data in sorted(self.files.items())
            ],
            "directories": list(self.directories),
            "excluded_roots": sorted(EXCLUDED_ROOTS),
            "excluded_directory_names": ["__pycache__"],
        }

    @property
    def revision_id(self) -> str:
        return content_id(SCHEMA_VERSION, self.manifest())

    def describe(self) -> dict[str, Any]:
        return {
            **self.manifest(),
            "revision_id": self.revision_id,
            "coverage": "all_workspace_files_except_declared_runtime_caches",
            "complete": True,
            "bounds": {"max_files": MAX_FILES, "max_entries": MAX_ENTRIES, "max_bytes": MAX_BYTES, "max_file_bytes": MAX_FILE_BYTES, "max_depth": MAX_DEPTH},
        }

    @contextmanager
    def materialize(self) -> Iterator[Path]:
        """Expose a private read-only tree, removing it when the operation finishes."""
        with tempfile.TemporaryDirectory(prefix="evidence-revision-") as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir(mode=0o700)
            for directory in self.directories:
                if directory != ".":
                    (root / directory).mkdir(parents=True, exist_ok=True)
            for name, data in self.files.items():
                path = root / name
                path.write_bytes(data)
                path.chmod(0o400)
            paths = [root / name for name in self.directories if name != "."]
            try:
                for directory in sorted(paths, key=lambda path: len(path.parts), reverse=True):
                    directory.chmod(0o500)
                root.chmod(0o500)
                yield root
            finally:
                root.chmod(0o700)
                for directory in sorted(paths, key=lambda path: len(path.parts)):
                    directory.chmod(0o700)


def capture_workspace(root: str | Path, *, max_attempts: int = MAX_ATTEMPTS) -> WorkspaceRevision:
    """Capture a local workspace without creating locks, caches, or other live files."""
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise refuse("EVIDENCE_REVISION_LIMIT", "Capture attempts must be between one and three.")
    root = Path(root).expanduser().resolve()
    for attempt in range(max_attempts):
        try:
            before = observe_tree(root)
            payloads = {
                relative: read_observed_file(root, relative, info)
                for relative, info in sorted(before.items()) if stat.S_ISREG(info[2])
            }
            if observe_tree(root) != before:
                raise refuse("EVIDENCE_REVISION_CHANGED", "Workspace changed during capture.")
            directories = tuple(sorted(relative for relative, info in before.items() if stat.S_ISDIR(info[2])))
            return WorkspaceRevision(MappingProxyType(payloads), directories)
        except (FileNotFoundError, NotADirectoryError):
            error = refuse("EVIDENCE_REVISION_CHANGED", "Workspace paths changed during capture.")
        except ScriptRefusal as error:
            if error.error_code != "EVIDENCE_REVISION_CHANGED":
                raise
            if attempt + 1 == max_attempts:
                raise
            continue
        except OSError as error:
            raise refuse("EVIDENCE_REVISION_UNSAFE", "Workspace could not be captured safely.", reason=error.strerror) from error
        if attempt + 1 == max_attempts:
            raise error
    raise AssertionError("bounded capture loop must return or refuse")
