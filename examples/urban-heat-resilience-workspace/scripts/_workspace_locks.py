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
available. It bypasses refusal but reports an unlocked handle to callers.
"""

from __future__ import annotations

import errno
import os
import secrets
import threading
import time
from collections.abc import Iterator
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
    """Raised when a workspace mutation lock cannot be established."""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object] | None = None,
        remediation: str = LOCK_REMEDIATION,
    ) -> None:
        super().__init__(message)
        self.error_code = LOCK_UNAVAILABLE
        self.details = details or {}
        self.remediation = remediation


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
                    raise LockUnavailableError(f"Timed out acquiring workspace lock for {lock_path}") from exc
                _sleep_until(deadline, poll_interval_seconds)
            except OSError as exc:
                if exc.errno in _CONTENDED_ERRNOS:
                    if time.monotonic() >= deadline:
                        raise LockUnavailableError(f"Timed out acquiring workspace lock for {lock_path}") from exc
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
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return _AcquiredBackend("msvcrt", handle=handle, path=lock_path)
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise LockUnavailableError(f"Timed out acquiring workspace lock for {lock_path}") from exc
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
            if time.monotonic() >= deadline:
                raise LockUnavailableError(f"Timed out acquiring workspace lock for {lock_path}") from exc
            if not already_attempted_stale_recovery:
                observation = _stale_exclusive_lock_observation(path, stale_after_seconds)
                if observation is not None:
                    # Confirm that this is the same unchanged lock after a
                    # bounded renewal grace period. This limits stale recovery
                    # to a best-effort fallback without deleting a lock whose
                    # owner just renewed it.
                    already_attempted_stale_recovery = True
                    grace_seconds = _stale_recovery_grace_seconds(stale_after_seconds)
                    if _wait_for_stale_recheck(deadline, grace_seconds):
                        confirmation = _stale_exclusive_lock_observation(path, stale_after_seconds)
                        if confirmation == observation and time.monotonic() < deadline:
                            _break_stale_exclusive_lock(
                                path,
                                observation.ownership_token,
                                stale_after_seconds,
                                expected_mtime_ns=observation.mtime_ns,
                                deadline=deadline,
                            )
                    continue
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
    raise LockUnavailableError(
        f"No workspace lock backend is available for {lock_path}",
        details={"unsupported_backends": unsupported},
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
) -> Iterator[WorkspaceLockHandle]:
    """Acquire an exclusive workspace mutation lock.

    The yielded handle exposes ``locked=False`` when the explicit single-writer
    escape hatch was used. Callers that emit machine JSON may include that fact
    in warnings. ``stale_exclusive_after_seconds`` only affects the last-resort
    exclusive-create backend; see its module-level default for rationale.
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
    except LockUnavailableError:
        if os.environ.get("EVIDENCE_WIKI_SINGLE_WRITER") == "1":
            yield WorkspaceLockHandle(
                path=normalized,
                purpose=purpose,
                backend="single_writer",
                locked=False,
                single_writer=True,
            )
            return
        raise

    try:
        yield WorkspaceLockHandle(path=normalized, purpose=purpose, backend=acquired.name)
    finally:
        _release_backend(acquired)
