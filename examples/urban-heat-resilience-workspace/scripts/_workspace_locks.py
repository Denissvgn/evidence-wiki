#!/usr/bin/env python3
"""Cross-platform workspace mutation locks.

Mutating workspace scripts should use :func:`workspace_lock` around the full
read-validate-write sequence. The helper uses ``fcntl.flock`` where available,
``msvcrt.locking`` on Windows, and an ownership-token exclusive-create lockfile
fallback when no native mechanism can be established. The fallback coordinates
processes on filesystems that honor atomic exclusive creation, but it is not
reported as having the same owner-death guarantees as a native advisory lock.
If no lock can be acquired, mutation refuses with ``LOCK_UNAVAILABLE``.

``EVIDENCE_WIKI_SINGLE_WRITER=1`` is a development-only escape hatch for
operator-controlled single-writer runs on filesystems where no lock primitive is
available. It bypasses refusal but reports an unlocked handle to callers, and no
holder sidecar is written on that path because it holds nothing to publish.

Its scope is exactly the condition it names: *no backend could be established*.
It does **not** swallow contention. A refusal that reports ``contended`` means a
peer holds this lock right now, which is the concurrent mutation callers use this
module to prevent, so it is raised even under the hatch. That keeps the hatch a
statement about the filesystem rather than a blanket opt-out of locking, and
keeps callers that translate contention into a refusal — such as the
orchestration controller's driver-busy check — working wherever a backend exists.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:  # pragma: no cover - platform dependent import
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # pragma: no cover - platform dependent import
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None


LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
LOCK_REMEDIATION = (
    "Wait for the active writer to finish and retry with bounded timeout. If the owner crashed, "
    "inspect the retained lock metadata before using the documented stale-lock recovery; do not delete raw evidence."
)
LOCK_BACKENDS = ("fcntl", "msvcrt", "exclusive")
_CONTENDED_ERRNOS = {errno.EACCES, errno.EAGAIN}

# Errnos that mean the filesystem cannot support this lock, never that a peer
# holds it. Retrying any of them is futile, so a backend that sees one steps
# aside for the next rather than reporting contention a host would retry.
_PERMANENT_LOCK_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("ENOSPC", "EROFS", "EBADF", "EINVAL", "EIO", "ENODEV", "EPERM", "EFBIG", "EDQUOT")
    if hasattr(errno, name)
)

# Optional holder metadata is published beside the lock file rather than inside
# it. The native backends cannot portably carry a payload in the locked file
# (msvcrt locks a byte range over a sentinel byte), and the exclusive fallback's
# own payload is load-bearing for stale recovery, so neither may grow a
# diagnostic field. A sidecar keeps holder reporting backend-agnostic and keeps
# the lock files themselves byte-identical for callers that pass no holder.
LOCK_HOLDER_SUFFIX = ".holder.json"

# Cap on the sidecar a *peer* wrote, applied when reading rather than writing.
# The blocks this module publishes are a few hundred bytes; the bound exists
# because the file is read on a refusal path, where a document large enough to
# exhaust memory would replace a truthful refusal with a crash. Sized to leave
# room for a caller's own holder shape to grow without ever approaching a size
# worth streaming.
MAX_LOCK_HOLDER_BYTES = 64 * 1024

# The exclusive-create backend is the last resort, used only when neither
# fcntl nor msvcrt is available (for example, some network filesystems). It
# has no OS-level owner-death notification, so a holder that crashes leaves
# the lock file behind. Breaking a lock file older than this age is best-effort:
# ownership tokens and a removal guard prevent stale breakers or former owners
# from deleting a known successor, but correctness still depends on atomic
# exclusive create, directory create, and filesystem timestamps. A breaker
# that crashes while holding the removal guard requires operator inspection.
# Real advisory-lock backends (fcntl, msvcrt) are never treated as stale.
DEFAULT_STALE_EXCLUSIVE_LOCK_SECONDS = 900.0


class LockUnavailableError(RuntimeError):
    """Raised when a workspace mutation lock cannot be established.

    ``contended`` separates the two situations this one exception reports.
    ``True`` means a backend worked and someone else holds the lock: the caller
    lost a race with a live writer and retrying can succeed. ``False`` means no
    backend could be established at all (or the raiser did not know), so the
    filesystem, not a peer, is the problem and retrying is pointless.

    It is a keyword-only flag on the existing class rather than a subclass on
    purpose. Workspace scripts load sibling modules by file path, so several
    copies of this module — and therefore several distinct ``LockUnavailableError``
    classes — coexist in one interpreter; ``fetch_sources`` already recognises a
    sibling's refusal by shape (``error_code``) instead of by class identity. A
    subclass would be invisible to those ``isinstance`` checks across copies,
    while an attribute survives them: consumers read ``getattr(exc, "contended",
    False)``, which also degrades safely (to "not contended") against an older
    vendored copy of this module. Every existing ``except LockUnavailableError``
    site keeps catching exactly what it caught before.
    """

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object] | None = None,
        remediation: str = LOCK_REMEDIATION,
        contended: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = LOCK_UNAVAILABLE
        self.details = details or {}
        self.remediation = remediation
        self.contended = contended


@dataclass(frozen=True)
class WorkspaceLockHandle:
    path: Path
    purpose: str
    backend: str
    locked: bool = True
    single_writer: bool = False


@dataclass
class _AcquiredBackend:
    name: str
    handle: object | None = None
    path: Path | None = None
    ownership_token: str | None = None
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None


class _BackendUnsupported(Exception):
    pass


class _ExclusiveHeartbeatOutcome(Enum):
    """Result of one fallback-lock heartbeat attempt.

    A removal guard can be held briefly by a competing stale-recovery attempt.
    That contention does not prove that this owner lost its lock, so callers
    must retry rather than stopping the heartbeat permanently.
    """

    RENEWED = "renewed"
    RETRY = "retry"
    OWNERSHIP_LOST = "ownership_lost"


@dataclass(frozen=True)
class _ExclusiveLockObservation:
    ownership_token: str
    mtime_ns: int


def available_lock_backends() -> tuple[str, ...]:
    """Return configured process-safe backends usable by this interpreter.

    This is a runtime capability report, not a claim that the test suite has
    exercised another operating system.  Callers and tests should use it
    instead of probing private platform imports such as ``fcntl``.
    """
    available: list[str] = []
    for backend in LOCK_BACKENDS:
        if backend == "fcntl" and fcntl is not None:
            available.append(backend)
        elif backend == "msvcrt" and msvcrt is not None:
            available.append(backend)
        elif backend == "exclusive":
            available.append(backend)
    return tuple(available)


def lock_capability() -> dict[str, object]:
    """Describe native guarantees separately from fallback coordination."""
    backends = available_lock_backends()
    native_backends = [backend for backend in backends if backend != "exclusive"]
    fallback_available = "exclusive" in backends
    return {
        "multiprocess_safe": bool(native_backends),
        "multiprocess_coordination_available": bool(backends),
        "available_backends": list(backends),
        "native_backends": native_backends,
        "fallback_backend": "exclusive" if fallback_available else None,
        "fallback_guarantee": (
            "atomic-exclusive-create with ownership-token guarded stale recovery; "
            "owner-death detection remains best-effort"
            if fallback_available
            else None
        ),
    }


def multiprocess_lock_supported() -> bool:
    """Return whether a native advisory multi-process backend is available."""
    return bool(lock_capability()["native_backends"])


def lock_holder_path(lock_path: Path) -> Path:
    """Return the sidecar path that carries optional holder metadata."""
    normalized = Path(lock_path)
    return normalized.with_name(f"{normalized.name}{LOCK_HOLDER_SUFFIX}")


def _write_lock_holder(lock_path: Path, holder: dict[str, object]) -> tuple[int, int] | None:
    """Publish holder metadata beside a lock this process already holds.

    The holder block is opaque to this module: it is serialized as given, with
    no required keys, so callers can evolve their own shape without changing
    the lock. Serialization happens before any filesystem effect, so a caller
    that passes a non-JSON-serializable holder fails loudly and deterministically
    instead of leaving a half-written or misleading sidecar.

    The write is temp-file + ``replace`` under a unique temp name so a
    concurrent writer — a stale-recovery attempt, or a successor that acquired
    the lock after a crash — cannot steal this writer's temp file. A failed
    write removes the sidecar rather than leaving whatever was there before:
    reporting *no* holder is honest, while leaving a predecessor's block would
    attribute this lock to a process that no longer owns it. The lock itself is
    already held at this point, so no I/O failure here is allowed to turn a
    successful acquisition into a refusal.

    The identity of the file this publishes is returned so the matching removal
    can prove it is deleting *its own* sidecar. Without that, a holder whose
    lock was stale-broken while it was stopped would, on resuming, delete the
    sidecar of the live successor that replaced it -- leaving a genuinely held
    lock reporting no holder at all.
    """
    holder_path = lock_holder_path(lock_path)
    payload = json.dumps(holder, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    tmp_path = holder_path.with_name(f".{holder_path.name}.{secrets.token_hex(16)}.tmp")
    published: tuple[int, int] | None = None
    try:
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:  # pragma: no cover - fsync can be unavailable on unusual filesystems
                    pass
            # Identity is read from the temp file *before* the rename: after it,
            # the name may already belong to a successor, and statting the
            # destination could adopt that successor's inode as this writer's.
            metadata = tmp_path.stat()
            published = (metadata.st_dev, metadata.st_ino)
            tmp_path.replace(holder_path)
        except OSError:  # pragma: no cover - best effort advisory metadata
            published = None
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    return published


def _unlink_lock_holder(lock_path: Path, published: tuple[int, int] | None) -> None:
    """Remove a holder sidecar this process published, tolerating a refusal.

    ``published`` is the ``(st_dev, st_ino)`` of the file this process wrote.
    The sidecar is removed only when the path still resolves to that exact
    inode, which is the same ownership discipline
    ``_remove_exclusive_lock_if_owned`` applies to the lock file itself: a
    predecessor whose lock was broken must not delete its successor's block.
    ``None`` means nothing was successfully published, so there is nothing this
    process is entitled to remove.
    """
    if published is None:
        return
    try:
        holder_path = lock_holder_path(lock_path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(holder_path, flags)
        try:
            metadata = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (metadata.st_dev, metadata.st_ino) != published:
            return
        holder_path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort cleanup
        pass


def read_lock_holder(lock_path: Path) -> dict[str, object] | None:
    """Return the holder block published beside ``lock_path``, if any.

    Advisory and best-effort by construction: this never raises, and ``None``
    means "no holder could be read", not "no holder exists". A missing file, an
    unreadable or truncated one, a partially written one from a crashed writer,
    and JSON that is valid but not an object all report ``None`` so that a
    caller rendering a refusal can fall back to an "unrecorded holder" message
    instead of failing while it explains a failure.

    A holder read after a *successful* acquisition would be meaningless, because
    a lock this process holds carries this process's own sidecar. Callers
    consult it only after losing the lock, and even then the answer is a hint:
    the winner writes its sidecar just after acquiring, so a loser that is
    refused inside that window legitimately sees nothing.

    The file is peer-written, so it is read the way every other untrusted
    document in this package is: opened ``O_NOFOLLOW`` so a symlink planted at
    the sidecar path cannot redirect the read at an arbitrary file, rejected
    unless it is a regular file so a FIFO cannot block the refusal forever, and
    capped at ``MAX_LOCK_HOLDER_BYTES`` so an oversized document cannot exhaust
    memory while a caller is composing a failure. Each of those is a ``None``,
    not an exception -- the promise that this never raises is what lets the
    refusal path depend on it.
    """
    try:
        holder_path = lock_holder_path(lock_path)
    except ValueError:
        # A lock path with no filename component, which ``with_name`` rejects.
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(holder_path, flags)
    except OSError:
        return None
    try:
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                # A FIFO would make the read below block until some writer
                # appeared, hanging a refusal that must always complete.
                return None
            # One byte over the cap is read so a document at exactly the limit
            # is still accepted while an oversized one is detected, never read
            # whole, and reported as unreadable.
            payload = os.read(descriptor, MAX_LOCK_HOLDER_BYTES + 1)
        except OSError:
            return None
        finally:
            os.close(descriptor)
    except OSError:  # pragma: no cover - close failing is not the caller's problem
        return None
    if len(payload) > MAX_LOCK_HOLDER_BYTES:
        return None
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and the UnicodeDecodeError from
        # bytes that are not valid UTF-8; RecursionError covers a deeply nested
        # document written by a buggy or hostile peer. Neither may escape into a
        # caller's refusal path.
        return None
    return parsed if isinstance(parsed, dict) else None


def _deadline(timeout_seconds: float) -> float:
    return time.monotonic() + max(timeout_seconds, 0.0)


def _sleep_until(deadline: float, poll_interval_seconds: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return
    time.sleep(min(max(poll_interval_seconds, 0.001), remaining))


def _acquire_fcntl(lock_path: Path, deadline: float, poll_interval_seconds: float) -> _AcquiredBackend:
    if fcntl is None:
        raise _BackendUnsupported("fcntl unavailable")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return _AcquiredBackend("fcntl", handle=handle, path=lock_path)
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise LockUnavailableError(
                        f"Timed out acquiring workspace lock for {lock_path}",
                        contended=True,
                    ) from exc
                _sleep_until(deadline, poll_interval_seconds)
            except OSError as exc:
                if exc.errno in _CONTENDED_ERRNOS:
                    if time.monotonic() >= deadline:
                        raise LockUnavailableError(
                            f"Timed out acquiring workspace lock for {lock_path}",
                            contended=True,
                        ) from exc
                    _sleep_until(deadline, poll_interval_seconds)
                    continue
                raise _BackendUnsupported(str(exc)) from exc
    except Exception:
        handle.close()
        raise


def _release_fcntl(acquired: _AcquiredBackend) -> None:
    handle = acquired.handle
    if handle is None or fcntl is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _acquire_msvcrt(lock_path: Path, deadline: float, poll_interval_seconds: float) -> _AcquiredBackend:
    if msvcrt is None:
        raise _BackendUnsupported("msvcrt unavailable")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep this handle unbuffered. A buffered ``a+b`` handle can defer the
    # first sentinel-byte write until ``flush()``. If another process acquires
    # byte zero between the write and that flush, Windows reports a transient
    # sharing/permission error outside the normal ``msvcrt.locking`` retry
    # loop. An unbuffered write makes initialization safely retryable too.
    handle = lock_path.open("a+b", buffering=0)
    try:
        while True:
            try:
                # msvcrt locks byte ranges, so seed a persistent byte before
                # locking byte zero. Concurrent initializers either append
                # the same harmless sentinel or receive a transient sharing
                # error; both cases use this bounded retry loop.
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return _AcquiredBackend("msvcrt", handle=handle, path=lock_path)
            except OSError as exc:
                # This loop can raise for two unrelated reasons, and only one of
                # them is contention. A byte-range lock refusal or the transient
                # sharing violation a concurrent initializer causes are
                # peer-induced, and retrying is the right answer. The sentinel
                # ``handle.write`` in the same block can also fail for reasons no
                # amount of retrying fixes -- a full disk, a read-only share, a
                # revoked handle -- and reporting those as contention would hand a
                # host a ``recoverable`` refusal naming a driver that does not
                # exist, to be retried forever. Those fall through to the next
                # backend instead, exactly as ``_acquire_fcntl`` does for an errno
                # outside ``_CONTENDED_ERRNOS``.
                #
                # Deny-list rather than allow-list because on Windows ``EACCES``
                # is genuinely ambiguous -- both a locked region and a permission
                # failure report it -- so an unrecognized errno keeps the
                # retry-as-contention behavior rather than silently disabling the
                # backend.
                if exc.errno in _PERMANENT_LOCK_ERRNOS:
                    raise _BackendUnsupported(str(exc)) from exc
                if time.monotonic() >= deadline:
                    raise LockUnavailableError(
                        f"Timed out acquiring workspace lock for {lock_path}",
                        contended=True,
                    ) from exc
                _sleep_until(deadline, poll_interval_seconds)
    except Exception:
        handle.close()
        raise


def _release_msvcrt(acquired: _AcquiredBackend) -> None:
    handle = acquired.handle
    if handle is None or msvcrt is None:
        return
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def _exclusive_lock_path(lock_path: Path) -> Path:
    return lock_path.with_name(f"{lock_path.name}.exclusive")


def _is_stale_exclusive_mtime(mtime_ns: int, stale_after_seconds: float) -> bool:
    stale_after_ns = int(max(stale_after_seconds, 0.0) * 1_000_000_000)
    return time.time_ns() - mtime_ns >= stale_after_ns


def _is_stale_exclusive_lock(path: Path, stale_after_seconds: float) -> bool:
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return False
    return _is_stale_exclusive_mtime(mtime_ns, stale_after_seconds)


def _ownership_token_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key == "ownership_token" and value:
            return value
    return None


def _read_exclusive_ownership_token(path: Path) -> str | None:
    return _ownership_token_from_lines(path.read_text(encoding="utf-8").splitlines())


def _exclusive_ownership_token(path: Path) -> str | None:
    try:
        return _read_exclusive_ownership_token(path)
    except (OSError, UnicodeError):
        return None


def _exclusive_lock_observation(path: Path) -> _ExclusiveLockObservation | None:
    """Read a stable token/mtime pair, or refuse to reason about the file."""
    try:
        before_mtime_ns = path.stat().st_mtime_ns
        ownership_token = _read_exclusive_ownership_token(path)
        after_mtime_ns = path.stat().st_mtime_ns
    except (OSError, UnicodeError):
        return None
    if ownership_token is None or before_mtime_ns != after_mtime_ns:
        return None
    return _ExclusiveLockObservation(ownership_token=ownership_token, mtime_ns=after_mtime_ns)


def _stale_exclusive_lock_observation(
    path: Path,
    stale_after_seconds: float,
) -> _ExclusiveLockObservation | None:
    observation = _exclusive_lock_observation(path)
    if observation is None or not _is_stale_exclusive_mtime(observation.mtime_ns, stale_after_seconds):
        return None
    return observation


def _exclusive_removal_guard_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.removal-guard")


def _remove_exclusive_lock_if_owned(
    path: Path,
    expected_token: str | None,
    *,
    require_stale_after_seconds: float | None = None,
    expected_mtime_ns: int | None = None,
    deadline: float | None = None,
) -> bool:
    if expected_token is None:
        return False
    if deadline is not None and time.monotonic() >= deadline:
        return False
    guard = _exclusive_removal_guard_path(path)
    try:
        guard.mkdir()
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        observation = _exclusive_lock_observation(path)
        if observation is None or observation.ownership_token != expected_token:
            return False
        if expected_mtime_ns is not None and observation.mtime_ns != expected_mtime_ns:
            return False
        if require_stale_after_seconds is not None and not _is_stale_exclusive_mtime(
            observation.mtime_ns,
            require_stale_after_seconds,
        ):
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False
    finally:
        try:
            guard.rmdir()
        except OSError:
            pass


def _break_stale_exclusive_lock(
    path: Path,
    expected_token: str | None,
    stale_after_seconds: float = DEFAULT_STALE_EXCLUSIVE_LOCK_SECONDS,
    *,
    expected_mtime_ns: int | None = None,
    deadline: float | None = None,
) -> bool:
    # Stale breakers and owner release serialize through a separate removal
    # guard, then re-read the token and timestamp. A successor can be created
    # after unlink, but no remover holding the guard performs a second unlink.
    return _remove_exclusive_lock_if_owned(
        path,
        expected_token,
        require_stale_after_seconds=stale_after_seconds,
        expected_mtime_ns=expected_mtime_ns,
        deadline=deadline,
    )


def _touch_exclusive_lock_if_owned(
    path: Path,
    expected_token: str,
) -> _ExclusiveHeartbeatOutcome:
    guard = _exclusive_removal_guard_path(path)
    try:
        guard.mkdir()
    except FileExistsError:
        return _ExclusiveHeartbeatOutcome.RETRY
    except OSError:
        return _ExclusiveHeartbeatOutcome.RETRY
    try:
        try:
            observed_token = _read_exclusive_ownership_token(path)
        except FileNotFoundError:
            return _ExclusiveHeartbeatOutcome.OWNERSHIP_LOST
        except (IsADirectoryError, NotADirectoryError, UnicodeError):
            return _ExclusiveHeartbeatOutcome.OWNERSHIP_LOST
        except OSError:
            return _ExclusiveHeartbeatOutcome.RETRY
        if observed_token != expected_token:
            return _ExclusiveHeartbeatOutcome.OWNERSHIP_LOST
        try:
            os.utime(path, None)
        except FileNotFoundError:
            return _ExclusiveHeartbeatOutcome.OWNERSHIP_LOST
        except OSError:
            return _ExclusiveHeartbeatOutcome.RETRY
        return _ExclusiveHeartbeatOutcome.RENEWED
    finally:
        try:
            guard.rmdir()
        except OSError:
            pass


def _exclusive_heartbeat(path: Path, ownership_token: str, stop: threading.Event, interval_seconds: float) -> None:
    retry_interval_seconds = max(0.01, min(interval_seconds, 0.05))
    wait_seconds = interval_seconds
    while not stop.wait(wait_seconds):
        outcome = _touch_exclusive_lock_if_owned(path, ownership_token)
        if outcome is _ExclusiveHeartbeatOutcome.OWNERSHIP_LOST:
            return
        wait_seconds = interval_seconds if outcome is _ExclusiveHeartbeatOutcome.RENEWED else retry_interval_seconds


def _exclusive_heartbeat_interval(stale_after_seconds: float) -> float:
    return max(0.01, min(60.0, stale_after_seconds / 3.0))


def _stale_recovery_grace_seconds(stale_after_seconds: float) -> float:
    """Give a live fallback owner one bounded chance to renew its heartbeat."""
    return min(0.5, _exclusive_heartbeat_interval(stale_after_seconds))


def _wait_for_stale_recheck(deadline: float, grace_seconds: float) -> bool:
    """Wait for a renewal grace period without extending an acquire timeout."""
    if time.monotonic() + grace_seconds >= deadline:
        return False
    time.sleep(grace_seconds)
    return time.monotonic() < deadline


def _acquire_exclusive(
    lock_path: Path,
    deadline: float,
    poll_interval_seconds: float,
    *,
    stale_after_seconds: float = DEFAULT_STALE_EXCLUSIVE_LOCK_SECONDS,
) -> _AcquiredBackend:
    path = _exclusive_lock_path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ownership_token = secrets.token_hex(16)
    payload = f"pid={os.getpid()}\ncreated_at={time.time():.6f}\nownership_token={ownership_token}\n"
    encoded = payload.encode("utf-8")
    already_attempted_stale_recovery = False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                path.unlink(missing_ok=True)
                raise
            heartbeat_stop = threading.Event()
            heartbeat_interval = _exclusive_heartbeat_interval(stale_after_seconds)
            heartbeat_thread = threading.Thread(
                target=_exclusive_heartbeat,
                args=(path, ownership_token, heartbeat_stop, heartbeat_interval),
                name="evidence-wiki-lock-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            return _AcquiredBackend(
                "exclusive",
                path=path,
                ownership_token=ownership_token,
                heartbeat_stop=heartbeat_stop,
                heartbeat_thread=heartbeat_thread,
            )
        except FileExistsError as exc:
            # Stale recovery is attempted *before* the deadline check, and on a
            # budget of its own. Reaping a lock whose owner has provably stopped
            # renewing it is orthogonal to how long this caller is willing to
            # queue behind a *live* peer: with ``timeout_seconds=0`` -- the
            # orchestration driver lock's default -- a deadline-first ordering
            # made recovery unreachable, so a crashed holder wedged the lock
            # permanently and every successor was refused as though a live
            # driver held it. A live owner's lock is never observed stale, so a
            # zero-timeout caller facing one still pays nothing and is refused
            # immediately; only an already-abandoned lock costs the grace.
            if not already_attempted_stale_recovery:
                observation = _stale_exclusive_lock_observation(path, stale_after_seconds)
                if observation is not None:
                    # Confirm that this is the same unchanged lock after a
                    # bounded renewal grace period. This limits stale recovery
                    # to a best-effort fallback without deleting a lock whose
                    # owner just renewed it.
                    already_attempted_stale_recovery = True
                    grace_seconds = _stale_recovery_grace_seconds(stale_after_seconds)
                    recovery_deadline = max(deadline, time.monotonic() + 2 * grace_seconds)
                    if _wait_for_stale_recheck(recovery_deadline, grace_seconds):
                        confirmation = _stale_exclusive_lock_observation(path, stale_after_seconds)
                        if confirmation == observation and time.monotonic() < recovery_deadline:
                            _break_stale_exclusive_lock(
                                path,
                                observation.ownership_token,
                                stale_after_seconds,
                                expected_mtime_ns=observation.mtime_ns,
                                deadline=recovery_deadline,
                            )
                    continue
            if time.monotonic() >= deadline:
                raise LockUnavailableError(
                    f"Timed out acquiring workspace lock for {lock_path}",
                    contended=True,
                ) from exc
            _sleep_until(deadline, poll_interval_seconds)
        except OSError as exc:
            raise _BackendUnsupported(str(exc)) from exc


def _release_exclusive(acquired: _AcquiredBackend) -> None:
    if acquired.path is None or acquired.ownership_token is None:
        return
    if acquired.heartbeat_stop is not None:
        acquired.heartbeat_stop.set()
    if acquired.heartbeat_thread is not None:
        acquired.heartbeat_thread.join(timeout=1.0)
    _remove_exclusive_lock_if_owned(acquired.path, acquired.ownership_token)


def _acquire_backend(
    lock_path: Path,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    stale_exclusive_after_seconds: float,
) -> _AcquiredBackend:
    unsupported: list[str] = []
    deadline = _deadline(timeout_seconds)
    for backend in LOCK_BACKENDS:
        try:
            if backend == "fcntl":
                return _acquire_fcntl(lock_path, deadline, poll_interval_seconds)
            if backend == "msvcrt":
                return _acquire_msvcrt(lock_path, deadline, poll_interval_seconds)
            if backend == "exclusive":
                return _acquire_exclusive(
                    lock_path,
                    deadline,
                    poll_interval_seconds,
                    stale_after_seconds=stale_exclusive_after_seconds,
                )
            unsupported.append(f"{backend}: unknown backend")
        except _BackendUnsupported as exc:
            unsupported.append(f"{backend}: {exc}")
            continue
    # Not contention: every configured backend refused to work at all, so there
    # is no peer to name and no retry that would help. A backend that *did*
    # work and lost the race raises with contended=True from inside its own
    # acquire loop, which propagates past this loop untouched.
    raise LockUnavailableError(
        f"No workspace lock backend is available for {lock_path}",
        details={"unsupported_backends": unsupported},
        contended=False,
    )


def _release_backend(acquired: _AcquiredBackend) -> None:
    if acquired.name == "fcntl":
        _release_fcntl(acquired)
    elif acquired.name == "msvcrt":
        _release_msvcrt(acquired)
    elif acquired.name == "exclusive":
        _release_exclusive(acquired)


@contextmanager
def workspace_lock(
    lock_path: Path,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.05,
    purpose: str = "workspace mutation",
    stale_exclusive_after_seconds: float = DEFAULT_STALE_EXCLUSIVE_LOCK_SECONDS,
    holder: dict[str, object] | Callable[[], dict[str, object]] | None = None,
) -> Iterator[WorkspaceLockHandle]:
    """Acquire an exclusive workspace mutation lock.

    The yielded handle exposes ``locked=False`` when the explicit single-writer
    escape hatch was used. Callers that emit machine JSON may include that fact
    in warnings. ``stale_exclusive_after_seconds`` only affects the last-resort
    exclusive-create backend; see its module-level default for rationale.

    ``holder`` is optional, opaque, JSON-serializable metadata describing who is
    taking the lock. When given, it is published to
    ``<lock_path>.holder.json`` after acquisition so that a peer refused by
    contention can name the holder instead of reporting an anonymous timeout.
    Passing no holder leaves every artifact this function touches
    byte-identical to before: no sidecar is written, read, or removed.

    Two ordering guarantees make the sidecar useful rather than misleading:

    * It is written only *after* the lock is held, so a published holder always
      described a real owner at the moment it was written.
    * It is removed *before* the backend is released, so the window in which a
      reader can see a released holder's leftovers is the release itself rather
      than the whole time between release and the next acquisition.

    A holder that crashes leaves its sidecar behind; this is accepted rather
    than defended against, because the next successful acquirer overwrites it
    and readers consult it only after failing to acquire — that is, only while
    *someone* holds the lock. Under ``EVIDENCE_WIKI_SINGLE_WRITER=1`` no sidecar
    is written at all: that path yields without holding anything, so it has no
    ownership to publish.
    """
    normalized = Path(lock_path)
    acquired: _AcquiredBackend | None = None
    try:
        acquired = _acquire_backend(
            normalized,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            stale_exclusive_after_seconds=stale_exclusive_after_seconds,
        )
    except LockUnavailableError as error:
        # The hatch exists for a workspace whose filesystem offers no lock
        # primitive at all. Contention is the opposite situation: a peer holds
        # this lock right now, and proceeding anyway is precisely the concurrent
        # mutation every caller of this function is asking to be protected from.
        # Swallowing it would silently reintroduce that, so a contended refusal
        # is re-raised even here.
        if os.environ.get("EVIDENCE_WIKI_SINGLE_WRITER") == "1" and not error.contended:
            yield WorkspaceLockHandle(
                path=normalized,
                purpose=purpose,
                backend="single_writer",
                locked=False,
                single_writer=True,
            )
            return
        raise

    published: tuple[int, int] | None = None
    try:
        if holder is not None:
            # A callable is resolved here, after acquisition, so a block can
            # record when the lock was actually taken rather than when it was
            # first attempted -- the two differ by the whole wait for a caller
            # that queued. Resolving inside this ``try`` keeps a raising callable
            # from leaking the backend.
            published = _write_lock_holder(normalized, holder() if callable(holder) else holder)
        yield WorkspaceLockHandle(path=normalized, purpose=purpose, backend=acquired.name)
    finally:
        # Nested so that nothing on the sidecar path — including a caller error
        # that made the holder unserializable — can leave the backend held.
        try:
            _unlink_lock_holder(normalized, published)
        finally:
            _release_backend(acquired)
