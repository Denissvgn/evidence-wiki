"""Private host-owned atomic evidence state, independent of workspace rollback.

The host provisions the state directory. Reads never create files. Protected
payloads enter only an authenticated transaction supplied by the usage layer.
No workspace path, fallback lock, or single-writer escape hatch is accepted.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from _evidence_authority import EvidenceInvalid
from _evidence_revision import observation

try:
    import fcntl
except ImportError:  # pragma: no cover - explicit unsupported-platform refusal
    fcntl = None

STATE_ENV = "EVIDENCE_WIKI_STATE_DIR"
STATE_FILE = "evidence-state.json"
LOCK_FILE = "evidence-state.lock"
MAX_STATE_BYTES = 64 * 1024 * 1024


def private_entry(info: os.stat_result, *, directory: bool = False) -> None:
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not kind or info.st_mode & 0o077 or info.st_uid != os.getuid()
            or not directory and info.st_nlink != 1):
        raise EvidenceInvalid("unsafe_host_state_entry")


@contextmanager
def host_directory(project_root: Path) -> Iterator[int]:
    raw = os.environ.get(STATE_ENV)
    if not raw:
        raise EvidenceInvalid("host_state_unavailable")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or path.resolve().is_relative_to(project_root.resolve()):
        raise EvidenceInvalid("unsafe_host_state_path")
    if (fcntl is None or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "getuid")
            or os.open not in os.supports_dir_fd or os.rename not in os.supports_dir_fd):
        raise EvidenceInvalid("host_state_unsupported")
    descriptors: list[int] = []
    try:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in path.parts[1:]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        private_entry(os.fstat(fd), directory=True)
        yield fd
        # Check every anchored entry; an ancestor replacement must also refuse.
        for parent, child, part in zip(descriptors[:-1], descriptors[1:], path.parts[1:], strict=True):
            if observation(os.fstat(child))[:2] != observation(
                os.stat(part, dir_fd=parent, follow_symlinks=False)
            )[:2]:
                raise EvidenceInvalid("host_state_directory_changed")
        private_entry(os.fstat(descriptors[-1]), directory=True)
    except OSError as exc:
        raise EvidenceInvalid("host_state_unreadable") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def read_state(directory: int, *, allow_missing: bool = False) -> bytes | None:
    try:
        fd = os.open(STATE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise EvidenceInvalid("host_state_not_initialized") from None
    try:
        before = os.fstat(fd)
        private_entry(before)
        if before.st_size > MAX_STATE_BYTES:
            raise EvidenceInvalid("host_state_bound_exceeded")
        data = bytearray()
        while True:
            chunk = os.read(fd, min(65536, MAX_STATE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_STATE_BYTES:
                raise EvidenceInvalid("host_state_bound_exceeded")
        after = os.fstat(fd)
        entry = os.stat(STATE_FILE, dir_fd=directory, follow_symlinks=False)
        if observation(before) != observation(after) or observation(after) != observation(entry):
            raise EvidenceInvalid("host_state_changed")
        return bytes(data)
    finally:
        os.close(fd)


def write_state(directory: int, data: bytes) -> None:
    """Publish one complete event generation, retaining the old one on failure."""
    if not isinstance(data, bytes) or len(data) > MAX_STATE_BYTES:
        raise EvidenceInvalid("host_state_bound_exceeded")
    temporary = ".evidence-state-" + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    published = False
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:offset + 65536])
            if written <= 0:
                raise EvidenceInvalid("host_state_write_incomplete")
            offset += written
        os.fsync(fd)
        os.rename(temporary, STATE_FILE, src_dir_fd=directory, dst_dir_fd=directory)
        published = True
        os.fsync(directory)
    finally:
        os.close(fd)
        if not published:
            os.unlink(temporary, dir_fd=directory)


@contextmanager
def locked_state(project_root: Path, *, write: bool = False, initialize: bool = False) -> Iterator[int]:
    with host_directory(project_root) as directory:
        if initialize and not write:
            raise EvidenceInvalid("invalid_host_state_lock_mode")
        flags = os.O_RDWR if write else os.O_RDONLY
        if initialize:
            try:
                lock = os.open(LOCK_FILE, flags | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=directory)
            except FileExistsError:
                lock = os.open(LOCK_FILE, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        else:
            lock = os.open(LOCK_FILE, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        acquired = False
        try:
            before = os.fstat(lock)
            private_entry(before)
            if before.st_size:
                raise EvidenceInvalid("unsafe_host_state_lock")
            try:
                fcntl.flock(lock, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise EvidenceInvalid("host_state_lock_unavailable") from exc
            if observation(before) != observation(os.stat(LOCK_FILE, dir_fd=directory, follow_symlinks=False)):
                raise EvidenceInvalid("host_state_lock_changed")
            yield directory
            if observation(before) != observation(os.stat(LOCK_FILE, dir_fd=directory, follow_symlinks=False)):
                raise EvidenceInvalid("host_state_lock_changed")
        finally:
            if acquired:
                fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
