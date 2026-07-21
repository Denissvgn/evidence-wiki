#!/usr/bin/env python3
"""Manage durable, provider-aware parent orchestration sessions.

The controller is deliberately model-neutral.  It never launches an LLM or a
network transport.  Instead it derives one bounded work order from durable
workspace artifacts, persists it under ``runs/orchestrations/<id>/``, and
accepts a small structured result after independently checking the workspace.
The package-side host runner is responsible for handing work orders to Codex,
Claude, or another compatible agent process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import urlparse

# Workspace sibling modules are protected static inputs.  Prevent controller
# imports from creating ``scripts/__pycache__`` entries that would mutate the
# very tree fingerprinted for pending-action integrity.
sys.dont_write_bytecode = True

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to manage research orchestration") from exc

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _provider_registry import ProviderListError, validate_provider_ids
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError, workspace_lock
from _workspace_module_loader import load_workspace_module

SCHEMA_VERSION = "1.0"
SESSION_ARTIFACT_TYPE = "orchestration_session"
WORK_ORDER_ARTIFACT_TYPE = "orchestration_work_order"
RESULT_ARTIFACT_TYPE = "orchestration_result"

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_BLOCKED = 3
EXIT_PAUSED = 4

DEFAULT_MAX_ACTIONS = 12
DEFAULT_ACTION_TIMEOUT_SECONDS = 30 * 60
DEFAULT_TOTAL_TIMEOUT_SECONDS = 2 * 60 * 60
MAX_RESULT_BYTES = 64 * 1024
MAX_SUMMARY_LENGTH = 4000
MAX_ARTIFACTS = 256
MAX_ARTIFACT_PATH_LENGTH = 512
MAX_TRUSTED_STATIC_INPUT_BYTES = 32 * 1024 * 1024
MAX_TRUSTED_STATIC_INPUT_ENTRIES = 10_000
MAX_TRUSTED_STATIC_FINGERPRINT_BYTES = 8 * 1024 * 1024
MAX_TRUSTED_STATIC_PATH_LENGTH = 1024
MAX_TRUSTED_STATIC_INPUT_DIFFERENCES = 50
MAX_JSON_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_VERIFICATION_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_RAW_TREE_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024
MAX_RAW_TREE_SNAPSHOT_ENTRIES = 10_000

SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.jsonl"
ANSWERS_FILENAME = "answers.json"
WORK_ORDERS_DIR = "work-orders"
WORK_RESULTS_DIR = "work-results"
TRUSTED_INPUTS_DIR = "trusted-inputs"
CONTROL_REPAIR_GUARDS_DIR = "orchestration-guards"

TRUSTED_STATIC_FILE_PATHS = (
    "research.yml",
    "workspace-system.yml",
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    ".gitignore",
)
TRUSTED_STATIC_TREE_PATHS = ("scripts", "skills", "docs")
TRUSTED_STATIC_EXCLUDED_SUBTREES: frozenset[str] = frozenset()

RECOVERY_NONE = "none"
RECOVERY_RECONCILE = "reconcile_required"
RECOVERY_FINALIZING = "finalizing_submission"
RECOVERY_STATES = frozenset({RECOVERY_NONE, RECOVERY_RECONCILE, RECOVERY_FINALIZING})

ACTIVE_STATUS = "active"
PAUSED_STATUS = "paused"
TERMINAL_STATUSES = frozenset({"complete", "blocked_on_sources", "no_ship", "failed"})
RESULT_OUTCOMES = frozenset({"completed", "blocked", "failed"})
PHASES = frozenset(
    {
        "planning",
        "research",
        "discovery",
        "candidate_review",
        "acquisition",
        "verification",
        "complete",
        "blocked_on_sources",
        "no_ship",
        "failed",
        "paused",
    }
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

_SIBLING_CACHE: dict[str, ModuleType] = {}


class OrchestrationControllerError(Exception):
    """A refused orchestration operation with a stable machine error code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        exit_code: int = EXIT_INVALID,
        recoverable: bool | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.exit_code = exit_code
        self.recoverable = recoverable
        self.remediation = remediation
        self.details = details


def load_sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage durable EvidenceWiki orchestration sessions.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Create one parent orchestration session.")
    start.add_argument("--orchestration-id", default=None)
    start.add_argument("--agent-id", required=True)
    start.add_argument("--max-actions", type=parse_positive_int, default=DEFAULT_MAX_ACTIONS)
    start.add_argument(
        "--action-timeout-seconds",
        type=parse_positive_int,
        default=DEFAULT_ACTION_TIMEOUT_SECONDS,
    )
    start.add_argument(
        "--total-timeout-seconds",
        type=parse_positive_int,
        default=DEFAULT_TOTAL_TIMEOUT_SECONDS,
    )
    start.add_argument("--format", choices=("text", "json"), default="text")

    next_parser = subparsers.add_parser("next", help="Issue or replay one persisted work order.")
    next_parser.add_argument("--orchestration-id", required=True)
    next_parser.add_argument("--agent-id", default=None)
    next_parser.add_argument("--resume", action="store_true")
    next_parser.add_argument("--format", choices=("text", "json"), default="text")

    submit = subparsers.add_parser("submit", help="Submit one structured work result.")
    submit.add_argument("--orchestration-id", required=True)
    submit.add_argument("--action-id", required=True)
    submit.add_argument("--result-file", required=True)
    submit.add_argument("--agent-id", default=None)
    submit.add_argument("--format", choices=("text", "json"), default="text")

    status = subparsers.add_parser("status", help="Read a parent orchestration session.")
    status.add_argument("--orchestration-id", default=None)
    status.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(argv)


def require_safe_id(value: Any, label: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or not SAFE_ID_RE.fullmatch(normalized) or ".." in normalized:
        raise OrchestrationControllerError(
            "ORCHESTRATION_ID_INVALID" if label == "orchestration_id" else "ACTION_ID_INVALID",
            f"{label} must be a filename-safe identifier",
            details={label: value},
        )
    return normalized


def require_agent_id(value: Any) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > 160 or "\x00" in normalized:
        raise OrchestrationControllerError("AGENT_ID_INVALID", "--agent-id must be a non-empty string")
    return normalized


def generated_orchestration_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    return f"orch-{stamp}-{uuid.uuid4().hex[:8]}"


def orchestration_root(project_root: Path) -> Path:
    return project_root / "runs" / "orchestrations"


def session_dir(project_root: Path, orchestration_id: str) -> Path:
    return orchestration_root(project_root) / orchestration_id


def session_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / SESSION_FILENAME


def events_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / EVENTS_FILENAME


def session_lock_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / ".locks" / "session.lock"


def work_order_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / WORK_ORDERS_DIR / f"{action_id}.json"


def work_result_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / WORK_RESULTS_DIR / f"{action_id}.json"


def trusted_static_input_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / TRUSTED_INPUTS_DIR / f"{action_id}.json"


def control_repair_path(project_root: Path, orchestration_id: str) -> Path:
    return project_root / "runs" / CONTROL_REPAIR_GUARDS_DIR / f"{orchestration_id}.json"


def answers_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / ANSWERS_FILENAME


def default_recovery_state() -> dict[str, Any]:
    return {
        "state": RECOVERY_NONE,
        "action_id": None,
        "attempt": None,
        "reason_code": None,
        "recorded_at": None,
    }


def result_digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def bounded_regular_bytes(
    path: Path,
    *,
    max_bytes: int,
    error_code: str,
    label: str,
    missing_ok: bool = False,
    containment_root: Path | None = None,
) -> bytes | None:
    """Read one bounded, singly linked regular file without following links."""
    if containment_root is not None:
        root = containment_root.resolve()
        absolute = Path(os.path.abspath(path))
        try:
            relative = absolute.relative_to(root)
        except ValueError as exc:
            raise OrchestrationControllerError(error_code, f"{label} escapes the workspace") from exc
        current = root
        for part in relative.parts[:-1]:
            current /= part
            try:
                ancestor = current.lstat()
            except OSError as exc:
                raise OrchestrationControllerError(error_code, f"could not inspect {label} ancestor: {current}") from exc
            if not stat.S_ISDIR(ancestor.st_mode) or path_is_link_like(current, ancestor):
                raise OrchestrationControllerError(error_code, f"{label} ancestor is not a real directory: {current}")
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise OrchestrationControllerError(error_code, f"{label} is missing: {path}") from None
    except OSError as exc:
        raise OrchestrationControllerError(error_code, f"could not inspect {label}: {path}: {exc}") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or path_is_link_like(path, before)
        or int(getattr(before, "st_nlink", 1) or 1) != 1
    ):
        raise OrchestrationControllerError(error_code, f"{label} is not a singly linked regular file: {path}")
    if before.st_size > max_bytes:
        raise OrchestrationControllerError(error_code, f"{label} exceeds the {max_bytes}-byte limit: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(getattr(opened, "st_nlink", 1) or 1) != 1
                or (before.st_dev, before.st_ino, before.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_size)
            ):
                raise OSError(f"{label} changed while it was opened")
            chunks: list[bytes] = []
            observed = 0
            while observed <= max_bytes:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - observed))
                if not chunk:
                    break
                chunks.append(chunk)
                observed += len(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OrchestrationControllerError(error_code, f"could not safely read {label}: {path}: {exc}") from exc
    if (
        observed > max_bytes
        or observed != before.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size)
    ):
        raise OrchestrationControllerError(error_code, f"{label} changed while it was read: {path}")
    return b"".join(chunks)


def file_digest(
    path: Path,
    *,
    max_bytes: int = MAX_VERIFICATION_ARTIFACT_BYTES,
    containment_root: Path | None = None,
) -> str | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"could not inspect verification artifact: {path}",
        ) from exc
    if containment_root is not None:
        root = containment_root.resolve()
        absolute = Path(os.path.abspath(path))
        try:
            relative = absolute.relative_to(root)
        except ValueError as exc:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                "verification artifact escapes the workspace",
            ) from exc
        current = root
        for part in relative.parts[:-1]:
            current /= part
            try:
                ancestor = current.lstat()
            except OSError as exc:
                raise OrchestrationControllerError(
                    "ORCHESTRATION_POSTCONDITION_FAILED",
                    f"could not inspect verification artifact ancestor: {current}",
                ) from exc
            if not stat.S_ISDIR(ancestor.st_mode) or path_is_link_like(current, ancestor):
                raise OrchestrationControllerError(
                    "ORCHESTRATION_POSTCONDITION_FAILED",
                    f"verification artifact ancestor is not a real directory: {current}",
                )
    if (
        not stat.S_ISREG(before.st_mode)
        or path_is_link_like(path, before)
        or int(getattr(before, "st_nlink", 1) or 1) != 1
        or before.st_size > max_bytes
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"verification artifact is unsafe or exceeds the {max_bytes}-byte limit: {path}",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(getattr(opened, "st_nlink", 1) or 1) != 1
                or (before.st_dev, before.st_ino, before.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_size)
            ):
                raise OSError("verification artifact changed while it was opened")
            digest = hashlib.sha256()
            observed = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > max_bytes:
                    raise OSError("verification artifact exceeded its size limit while being read")
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"could not safely hash verification artifact: {path}: {exc}",
        ) from exc
    if (
        observed != before.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size)
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"verification artifact changed while it was hashed: {path}",
        )
    return f"sha256:{digest.hexdigest()}"


def trusted_static_input_error(message: str, *, details: dict[str, Any] | None = None) -> OrchestrationControllerError:
    return OrchestrationControllerError(
        "ORCHESTRATION_TRUSTED_INPUT_UNSAFE",
        message,
        recoverable=True,
        remediation=(
            "Replace links or special files with bounded regular files/directories under the trusted workspace "
            "inputs, then retry. Keep generated research output under its documented writable paths."
        ),
        details=details,
    )


def path_is_link_like(path: Path, metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return True


def portable_mode(metadata: os.stat_result) -> int:
    """Return only portable rwx permission bits for semantic comparisons."""
    return stat.S_IMODE(metadata.st_mode) & 0o777


def require_real_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise trusted_static_input_error(f"cannot inspect trusted input ancestor {label}: {exc}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path_is_link_like(path, metadata):
        raise trusted_static_input_error(f"trusted input ancestor {label} is not a real directory")


def validate_trusted_static_ancestors(project_root: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(project_root)
    except ValueError as exc:  # pragma: no cover - paths are assembled internally
        raise trusted_static_input_error(f"trusted input {label} escapes the workspace") from exc
    require_real_directory(project_root, ".")
    current = project_root
    for part in relative.parts[:-1]:
        current /= part
        require_real_directory(current, current.relative_to(project_root).as_posix())


def validate_trusted_static_carveouts(project_root: Path) -> None:
    """Reject unsafe entries in writable control-tree carveouts without fingerprinting their contents."""
    inspected = 0

    def visit(path: Path, label: str) -> None:
        nonlocal inspected
        inspected += 1
        if inspected > MAX_TRUSTED_STATIC_INPUT_ENTRIES:
            raise trusted_static_input_error(
                f"writable trusted-input carveouts exceed {MAX_TRUSTED_STATIC_INPUT_ENTRIES} entries"
            )
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise trusted_static_input_error(f"cannot inspect writable trusted-input carveout {label}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise trusted_static_input_error(f"writable trusted-input carveout {label} is a symbolic link or junction")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise trusted_static_input_error(
                    f"cannot enumerate writable trusted-input carveout {label}: {exc}"
                ) from exc
            for child in children:
                visit(child, f"{label}/{child.name}")
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise trusted_static_input_error(f"writable trusted-input carveout {label} contains a special file")
        if metadata.st_nlink > 1:
            raise trusted_static_input_error(f"writable trusted-input carveout {label} is multiply linked")

    for relative in TRUSTED_STATIC_EXCLUDED_SUBTREES:
        path = project_root.joinpath(*PurePosixPath(relative).parts)
        validate_trusted_static_ancestors(project_root, path, relative)
        visit(path, relative)


def trusted_static_input_fingerprint(project_root: Path) -> dict[str, Any]:
    """Capture a bounded, deterministic semantic fingerprint of trusted static workspace inputs."""
    project_root = project_root.resolve()
    validate_trusted_static_carveouts(project_root)
    entries: list[dict[str, Any]] = []
    total_bytes = 0

    def visit(path: Path, relative: PurePosixPath) -> None:
        nonlocal total_bytes
        label = relative.as_posix()
        if label in TRUSTED_STATIC_EXCLUDED_SUBTREES:
            return
        if len(entries) >= MAX_TRUSTED_STATIC_INPUT_ENTRIES:
            raise trusted_static_input_error(
                f"trusted static inputs exceed {MAX_TRUSTED_STATIC_INPUT_ENTRIES} entries"
            )
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            entries.append({"path": label, "kind": "missing", "mode": 0, "size": 0, "sha256": None})
            return
        except OSError as exc:
            raise trusted_static_input_error(f"cannot inspect trusted static input {label}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise trusted_static_input_error(f"trusted static input {label} is a symbolic link or junction")
        mode = portable_mode(metadata)
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": label, "kind": "directory", "mode": mode, "size": 0, "sha256": None})
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise trusted_static_input_error(f"cannot enumerate trusted static input {label}: {exc}") from exc
            for child in children:
                visit(child, relative / child.name)
            return
        if not stat.S_ISREG(metadata.st_mode):
            raise trusted_static_input_error(f"trusted static input {label} is not a regular file or directory")
        if metadata.st_nlink > 1:
            raise trusted_static_input_error(f"trusted static input {label} is multiply linked")
        declared_size = int(metadata.st_size)
        if declared_size > MAX_TRUSTED_STATIC_INPUT_BYTES - total_bytes:
            raise trusted_static_input_error(
                f"trusted static inputs exceed the {MAX_TRUSTED_STATIC_INPUT_BYTES}-byte limit"
            )
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink > 1
                    or (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise trusted_static_input_error(f"trusted static input {label} changed while it was opened")
                digest = hashlib.sha256()
                observed_size = 0
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    observed_size += len(chunk)
                    if observed_size > declared_size:
                        raise trusted_static_input_error(f"trusted static input {label} changed while it was read")
                    digest.update(chunk)
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        except OrchestrationControllerError:
            raise
        except OSError as exc:
            raise trusted_static_input_error(f"cannot read trusted static input {label}: {exc}") from exc
        if (
            observed_size != declared_size
            or (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size)
            or portable_mode(after) != mode
        ):
            raise trusted_static_input_error(f"trusted static input {label} changed while it was inspected")
        total_bytes += observed_size
        entries.append(
            {
                "path": label,
                "kind": "file",
                "mode": mode,
                "size": observed_size,
                "sha256": f"sha256:{digest.hexdigest()}",
            }
        )

    roots = (*TRUSTED_STATIC_FILE_PATHS, *TRUSTED_STATIC_TREE_PATHS)
    for relative in roots:
        path = project_root.joinpath(*PurePosixPath(relative).parts)
        validate_trusted_static_ancestors(project_root, path, relative)
        visit(path, PurePosixPath(relative))
    entries.sort(key=lambda item: item["path"])
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_TRUSTED_STATIC_FINGERPRINT_BYTES:
        raise trusted_static_input_error(
            f"trusted static fingerprint exceeds the {MAX_TRUSTED_STATIC_FINGERPRINT_BYTES}-byte limit"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "sha256",
        "fingerprint": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        "entry_count": len(entries),
        "total_bytes": total_bytes,
        "entries": entries,
    }


def valid_trusted_static_input_fingerprint(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "algorithm",
        "fingerprint",
        "entry_count",
        "total_bytes",
        "entries",
    }:
        return False
    entries = value.get("entries")
    entry_count = value.get("entry_count")
    total_bytes = value.get("total_bytes")
    if (
        not isinstance(entries, list)
        or not isinstance(entry_count, int)
        or isinstance(entry_count, bool)
        or entry_count != len(entries)
        or entry_count < 0
        or entry_count > MAX_TRUSTED_STATIC_INPUT_ENTRIES
        or not isinstance(total_bytes, int)
        or isinstance(total_bytes, bool)
        or total_bytes < 0
        or total_bytes > MAX_TRUSTED_STATIC_INPUT_BYTES
    ):
        return False
    seen_paths: set[str] = set()
    observed_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "kind", "mode", "size", "sha256"}:
            return False
        path_value = entry.get("path")
        if (
            not isinstance(path_value, str)
            or not path_value
            or "\x00" in path_value
            or len(path_value) > MAX_TRUSTED_STATIC_PATH_LENGTH
        ):
            return False
        relative = PurePosixPath(path_value)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in path_value
            or relative.as_posix() != path_value
            or path_value in seen_paths
        ):
            return False
        allowed = path_value in TRUSTED_STATIC_FILE_PATHS or relative.parts[:1] in {
            (root,) for root in TRUSTED_STATIC_TREE_PATHS
        }
        if not allowed:
            return False
        seen_paths.add(path_value)
        kind = entry.get("kind")
        mode = entry.get("mode")
        size = entry.get("size")
        digest = entry.get("sha256")
        if kind not in {"missing", "directory", "file"}:
            return False
        if not isinstance(mode, int) or isinstance(mode, bool) or mode < 0 or mode > 0o777:
            return False
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return False
        if kind == "file":
            if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                return False
            observed_bytes += size
        elif size != 0 or digest is not None:
            return False
        if kind == "missing" and path_value not in {*TRUSTED_STATIC_FILE_PATHS, *TRUSTED_STATIC_TREE_PATHS}:
            return False
    if [entry["path"] for entry in entries] != sorted(seen_paths) or observed_bytes != total_bytes:
        return False
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_TRUSTED_STATIC_FINGERPRINT_BYTES:
        return False
    expected_digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    return (
        value.get("schema_version") == SCHEMA_VERSION
        and value.get("algorithm") == "sha256"
        and value.get("fingerprint") == expected_digest
    )


def valid_pending_trusted_static_inputs(value: Any) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"action_id", "fingerprint", "entry_count", "total_bytes"}
        and isinstance(value.get("action_id"), str)
        and bool(value["action_id"])
        and isinstance(value.get("fingerprint"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["fingerprint"]) is not None
        and isinstance(value.get("entry_count"), int)
        and not isinstance(value.get("entry_count"), bool)
        and 0 <= value["entry_count"] <= MAX_TRUSTED_STATIC_INPUT_ENTRIES
        and isinstance(value.get("total_bytes"), int)
        and not isinstance(value.get("total_bytes"), bool)
        and 0 <= value["total_bytes"] <= MAX_TRUSTED_STATIC_INPUT_BYTES
    )


def trusted_static_input_differences(expected: dict[str, Any], current: dict[str, Any]) -> list[str]:
    before = {entry["path"]: entry for entry in expected.get("entries", []) if isinstance(entry, dict)}
    after = {entry["path"]: entry for entry in current.get("entries", []) if isinstance(entry, dict)}
    differences: list[str] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        if old is None:
            reason = "created"
        elif new is None:
            reason = "removed"
        else:
            changed: list[str] = []
            if old.get("kind") != new.get("kind"):
                changed.append(f"kind {old.get('kind')}->{new.get('kind')}")
            if old.get("mode") != new.get("mode"):
                changed.append(f"mode {old.get('mode'):03o}->{new.get('mode'):03o}")
            if old.get("size") != new.get("size") or old.get("sha256") != new.get("sha256"):
                changed.append("content")
            reason = ", ".join(changed) or "semantic state"
        differences.append(f"{path} [{reason}]")
    return differences


def verify_pending_trusted_static_inputs(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    *,
    allow_legacy_unbound: bool = False,
) -> None:
    """Fail closed on static-input drift and explicitly migrate legacy pending actions."""
    if session.get("pending_action_id") != work_order.get("action_id"):
        return
    if "pending_trusted_static_inputs" not in session:
        # Version 0.2.0 sessions predate controller-owned static fingerprints.
        if allow_legacy_unbound:
            return
        raise OrchestrationControllerError(
            "ORCHESTRATION_LEGACY_ACTION_UNBOUND",
            "legacy pending action has not yet been bound to the current trusted static inputs",
            recoverable=True,
            remediation=(
                "Replay the pending action with evidence-wiki orchestrate next --resume, or use managed "
                "evidence-wiki orchestrate resume, before submitting a result. The replay binds a controller-owned "
                "fingerprint before any worker is launched."
            ),
            details={"action_id": work_order.get("action_id")},
        )
    retained = session.get("pending_trusted_static_inputs")
    if not valid_pending_trusted_static_inputs(retained) or retained is None:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "new pending action is missing its trusted static-input fingerprint",
            recoverable=False,
        )
    if retained.get("action_id") != work_order.get("action_id"):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "trusted static-input fingerprint does not belong to the pending action",
            recoverable=False,
        )
    snapshot_path = trusted_static_input_path(
        project_root,
        session["orchestration_id"],
        work_order["action_id"],
    )
    expected = load_json_object(
        snapshot_path,
        error_code="ORCHESTRATION_STATE_INVALID",
        label="trusted static-input fingerprint",
    )
    if not valid_trusted_static_input_fingerprint(expected):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "persisted trusted static-input fingerprint is invalid",
            recoverable=False,
        )
    if (
        retained.get("fingerprint") != expected.get("fingerprint")
        or retained.get("entry_count") != expected.get("entry_count")
        or retained.get("total_bytes") != expected.get("total_bytes")
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "parent session does not match its trusted static-input fingerprint",
            recoverable=False,
        )
    current = trusted_static_input_fingerprint(project_root)
    if expected.get("fingerprint") == current.get("fingerprint"):
        return
    differences = trusted_static_input_differences(expected, current)
    shown = differences[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES]
    omitted = max(0, len(differences) - len(shown))
    raise OrchestrationControllerError(
        "ORCHESTRATION_TRUSTED_INPUT_CHANGED",
        "trusted static workspace inputs changed after the action was issued",
        recoverable=True,
        remediation=(
            "Restore the issued static inputs and retry the same pending action. If the change was intentional, "
            "start a new orchestration session from the updated workspace instead of editing parent state."
        ),
        details={
            "action_id": work_order.get("action_id"),
            "expected_fingerprint": expected.get("fingerprint"),
            "current_fingerprint": current.get("fingerprint"),
            "changed_paths": shown,
            "omitted_changed_path_count": omitted,
        },
    )


def relative_workspace_path(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError as exc:
        raise OrchestrationControllerError(
            "ARTIFACT_PATH_INVALID",
            f"path escapes the workspace: {path}",
        ) from exc


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_WRITE_FAILED",
            f"could not persist {path}: {exc}",
            recoverable=True,
            remediation="Restore workspace write access or free space, then retry the idempotent command.",
        ) from exc


def load_json_object(
    path: Path,
    *,
    error_code: str,
    label: str,
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
    containment_root: Path | None = None,
) -> dict[str, Any]:
    try:
        content = bounded_regular_bytes(
            path,
            max_bytes=max_bytes,
            error_code=error_code,
            label=label,
            containment_root=containment_root,
        )
        if content is None:  # pragma: no cover - missing_ok is false
            raise OrchestrationControllerError(error_code, f"{label} is missing: {path}")
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OrchestrationControllerError(error_code, f"invalid JSON in {label}: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise OrchestrationControllerError(error_code, f"{label} must contain a JSON object: {path}")
    return document


def enforce_control_repair_gate(project_root: Path, orchestration_id: str) -> None:
    """Prevent protocol replay/submission while the host repair marker is required."""
    path = control_repair_path(project_root, orchestration_id)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            f"could not inspect control-repair marker: {exc}",
            recoverable=False,
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or path_is_link_like(path, metadata) or metadata.st_nlink > 1:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "control-repair marker is not a singly linked regular file",
            recoverable=False,
        )
    marker = load_json_object(
        path,
        error_code="ORCHESTRATION_STATE_INVALID",
        label="control-repair marker",
        max_bytes=MAX_RESULT_BYTES,
        containment_root=project_root,
    )
    required_keys = {
        "schema_version",
        "artifact_type",
        "orchestration_id",
        "status",
        "reason_code",
        "detected_at",
        "acknowledged_at",
        "attempt_ids",
        "expected_control_fingerprint",
    }
    attempt_ids = marker.get("attempt_ids")
    if (
        set(marker) != required_keys
        or marker.get("schema_version") != SCHEMA_VERSION
        or marker.get("artifact_type") != "orchestration_control_repair"
        or marker.get("orchestration_id") != orchestration_id
        or marker.get("status") not in {"required", "acknowledged"}
        or marker.get("reason_code") != "CONTROL_ARTIFACT_TAMPERED"
        or not isinstance(marker.get("detected_at"), str)
        or len(marker["detected_at"]) > 64
        or not isinstance(attempt_ids, list)
        or not attempt_ids
        or len(attempt_ids) > 64
        or len(attempt_ids) != len(set(attempt_ids))
        or any(not isinstance(value, str) or SAFE_ID_RE.fullmatch(value) is None for value in attempt_ids)
        or not isinstance(marker.get("expected_control_fingerprint"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", marker["expected_control_fingerprint"]) is None
        or (
            marker["status"] == "required"
            and marker.get("acknowledged_at") is not None
        )
        or (
            marker["status"] == "acknowledged"
            and (
                not isinstance(marker.get("acknowledged_at"), str)
                or not marker["acknowledged_at"]
                or len(marker["acknowledged_at"]) > 64
            )
        )
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "control-repair marker is invalid",
            recoverable=False,
        )
    if marker["status"] == "required":
        raise OrchestrationControllerError(
            "ORCHESTRATION_CONTROL_REPAIR_REQUIRED",
            "managed control drift must be repaired and acknowledged before replay or submission",
            recoverable=True,
            remediation=(
                "Inspect the retained attempt and quarantine, restore the issued state, then run managed "
                "orchestrate resume with --acknowledge-control-repair."
            ),
            details={"attempt_ids": attempt_ids},
        )


def valid_pending_submission(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "action_id",
        "accepted_at",
        "result",
        "result_digest",
        "next_phase",
        "completion_reason",
    }:
        return False
    result = value.get("result")
    return (
        isinstance(value.get("action_id"), str)
        and bool(value["action_id"])
        and isinstance(value.get("accepted_at"), str)
        and bool(value["accepted_at"])
        and isinstance(result, dict)
        and valid_stored_result_shape(result, value["action_id"])
        and isinstance(value.get("result_digest"), str)
        and value["result_digest"] == result_digest(result)
        and (value.get("next_phase") is None or value["next_phase"] in PHASES)
        and (value.get("completion_reason") is None or isinstance(value["completion_reason"], str))
    )


def valid_recovery_state(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "state",
        "action_id",
        "attempt",
        "reason_code",
        "recorded_at",
    }:
        return False
    return (
        value.get("state") in RECOVERY_STATES
        and (value.get("action_id") is None or isinstance(value["action_id"], str))
        and (
            value.get("attempt") is None
            or (isinstance(value["attempt"], int) and not isinstance(value["attempt"], bool) and value["attempt"] > 0)
        )
        and (value.get("reason_code") is None or isinstance(value["reason_code"], str))
        and (value.get("recorded_at") is None or isinstance(value["recorded_at"], str))
    )


def load_session(project_root: Path, orchestration_id: str) -> dict[str, Any]:
    path = session_path(project_root, orchestration_id)
    if not path.is_file():
        raise OrchestrationControllerError(
            "ORCHESTRATION_UNKNOWN",
            f"unknown orchestration id: {orchestration_id}",
            details={"orchestration_id": orchestration_id},
        )
    document = load_json_object(path, error_code="ORCHESTRATION_STATE_INVALID", label="orchestration session")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("artifact_type") != SESSION_ARTIFACT_TYPE
        or document.get("orchestration_id") != orchestration_id
        or document.get("status") not in {ACTIVE_STATUS, PAUSED_STATUS, *TERMINAL_STATUSES}
        or document.get("phase") not in PHASES
        or not isinstance(document.get("child_run_ids"), list)
        or not isinstance(document.get("limits"), dict)
        or not valid_pending_submission(document.get("pending_submission"))
        or not valid_recovery_state(document.get("recovery"))
        or (
            "pending_trusted_static_inputs" in document
            and not valid_pending_trusted_static_inputs(document.get("pending_trusted_static_inputs"))
        )
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            f"invalid orchestration session shape: {relative_workspace_path(project_root, path)}",
            recoverable=False,
        )
    return document


def compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def append_event(project_root: Path, orchestration_id: str, event: dict[str, Any]) -> None:
    path = events_path(project_root, orchestration_id)
    existing: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OrchestrationControllerError(
                    "ORCHESTRATION_EVENTS_INVALID",
                    f"invalid retained orchestration event JSON: {exc}",
                    recoverable=False,
                ) from exc
            if not isinstance(item, dict):
                raise OrchestrationControllerError(
                    "ORCHESTRATION_EVENTS_INVALID",
                    "retained orchestration event is not a JSON object",
                    recoverable=False,
                )
            existing.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    event["event_id"] = f"evt-{len(existing) + 1:04d}"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text("".join(compact_json(item) + "\n" for item in [*existing, event]), encoding="utf-8")
    temporary.replace(path)


def record_event(
    project_root: Path,
    session: dict[str, Any],
    event_type: str,
    message: str,
    *,
    action_id: str | None = None,
    data: dict[str, Any] | None = None,
    event_key: str | None = None,
) -> None:
    event = {
        "schema_version": SCHEMA_VERSION,
        "orchestration_id": session["orchestration_id"],
        "occurred_at": timestamp_utc(),
        "agent_id": session["agent_id"],
        "event_type": event_type,
        "action_id": action_id,
        "phase": session["phase"],
        "message": message,
        "data": data or {},
    }
    if event_key is not None:
        event["event_key"] = event_key
    append_event(
        project_root,
        session["orchestration_id"],
        event,
    )


def event_key_exists(
    project_root: Path,
    orchestration_id: str,
    event_key: str,
    *,
    event_type: str,
    action_id: str | None,
) -> bool:
    path = events_path(project_root, orchestration_id)
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_EVENTS_INVALID",
            f"could not read retained orchestration events: {exc}",
            recoverable=False,
        ) from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OrchestrationControllerError(
                "ORCHESTRATION_EVENTS_INVALID",
                f"invalid retained orchestration event JSON: {exc}",
                recoverable=False,
            ) from exc
        if isinstance(event, dict):
            if event.get("event_key") == event_key:
                return True
            if event.get("event_type") == event_type and event.get("action_id") == action_id:
                return True
    return False


def record_event_once(
    project_root: Path,
    session: dict[str, Any],
    event_type: str,
    message: str,
    *,
    action_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    event_key = f"{event_type}:{action_id or 'session'}"
    if event_key_exists(
        project_root,
        session["orchestration_id"],
        event_key,
        event_type=event_type,
        action_id=action_id,
    ):
        return
    record_event(
        project_root,
        session,
        event_type,
        message,
        action_id=action_id,
        data=data,
        event_key=event_key,
    )


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        raise OrchestrationControllerError("CONFIG_MISSING", f"Missing research.yml: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise OrchestrationControllerError("CONFIG_INVALID", f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise OrchestrationControllerError("CONFIG_INVALID", f"research.yml must contain a mapping: {path}")
    return document


def provider_policy(config: dict[str, Any]) -> dict[str, Any]:
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    policy: dict[str, Any] = {}
    for phase in ("discovery", "acquisition"):
        block = integrations.get(phase) if isinstance(integrations.get(phase), dict) else {}
        try:
            validated = validate_provider_ids(block.get("providers"), phase=phase)
        except ProviderListError as exc:
            raise OrchestrationControllerError(
                "CONFIG_INVALID",
                f"research.yml integrations.{phase}.providers {exc}",
                recoverable=False,
            ) from exc
        providers = sorted(validated.providers)
        policy[phase] = {
            # Strategy aliases remain readable for one compatibility release,
            # but only concrete providers grant permission to contact a
            # transport.  An alias-only list therefore has no effective route.
            "enabled": block.get("enabled") is True and bool(providers),
            "providers": providers,
        }
    return policy


def verify_runtime_guards(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-check mutable safety and authorization state before replay/submit."""
    # The workspace status and configuration readers load code from the
    # workspace.  Detect drift in those trusted inputs before importing or
    # executing any of them.
    if work_order is not None:
        verify_pending_trusted_static_inputs(
            project_root,
            session,
            work_order,
        )
    status = fresh_workspace_status(project_root)
    health = status.get("workspace_health") if isinstance(status.get("workspace_health"), dict) else {}
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    if not health.get("materially_valid", False) or readiness.get("verdict") == "attention_required":
        raise OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            "workspace health or HIGH validation findings changed after the work order was issued",
            recoverable=False,
            remediation="Repair the reported workspace findings before replaying or submitting this action.",
            details={
                "workspace_health": health,
                "readiness_verdict": readiness.get("verdict"),
                "readiness_reasons": readiness.get("reasons", []),
            },
        )
    verify_provider_policy_unchanged(project_root, session, work_order)
    return status


def verify_provider_policy_unchanged(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any] | None = None,
) -> None:
    """Safely compare YAML provider authorization without executing workspace code."""
    current = provider_policy(load_config(project_root))
    expected = work_order.get("provider_policy") if isinstance(work_order, dict) else session.get("provider_policy")
    expected = expected if isinstance(expected, dict) else {}
    removed: dict[str, list[str]] = {}
    for phase in ("discovery", "acquisition"):
        expected_phase = expected.get(phase) if isinstance(expected.get(phase), dict) else {}
        current_phase = current.get(phase) if isinstance(current.get(phase), dict) else {}
        expected_providers = {
            value for value in expected_phase.get("providers", []) if isinstance(value, str) and value
        }
        current_providers = {
            value for value in current_phase.get("providers", []) if isinstance(value, str) and value
        }
        missing = sorted(expected_providers - current_providers)
        if expected_phase.get("enabled") is True and current_phase.get("enabled") is not True:
            missing = sorted(expected_providers or {"<phase-disabled>"})
        if missing:
            removed[phase] = missing
    if removed:
        raise OrchestrationControllerError(
            "ORCHESTRATION_PROVIDER_POLICY_CHANGED",
            "provider authorization was narrowed after the work order was issued",
            recoverable=False,
            remediation="Restore the work order's explicit provider allow-list or start a new orchestration session.",
            details={"removed_providers": removed, "current_provider_policy": current},
        )


def bind_legacy_pending_trusted_inputs(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
) -> None:
    """Bind one pre-0.2.1 pending action before workspace code is executed."""
    if "pending_trusted_static_inputs" in session:
        return
    action_id = require_safe_id(work_order.get("action_id"), "action_id")
    if session.get("pending_action_id") != action_id:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "legacy trusted-input binding does not match the pending action",
            recoverable=False,
        )
    fingerprint_path = trusted_static_input_path(project_root, session["orchestration_id"], action_id)
    if fingerprint_path.exists():
        fingerprint = load_json_object(
            fingerprint_path,
            error_code="ORCHESTRATION_STATE_INVALID",
            label="legacy trusted static-input fingerprint",
        )
        if not valid_trusted_static_input_fingerprint(fingerprint):
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "legacy trusted static-input fingerprint is invalid",
                recoverable=False,
            )
        current = trusted_static_input_fingerprint(project_root)
        if fingerprint.get("fingerprint") != current.get("fingerprint"):
            differences = trusted_static_input_differences(fingerprint, current)
            shown = differences[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES]
            raise OrchestrationControllerError(
                "ORCHESTRATION_TRUSTED_INPUT_CHANGED",
                "trusted static workspace inputs changed while legacy binding was being finalized",
                recoverable=True,
                remediation=(
                    "Restore the static inputs recorded by the retained fingerprint, then replay the same action."
                ),
                details={
                    "action_id": action_id,
                    "expected_fingerprint": fingerprint.get("fingerprint"),
                    "current_fingerprint": current.get("fingerprint"),
                    "changed_paths": shown,
                    "omitted_changed_path_count": max(0, len(differences) - len(shown)),
                },
            )
    else:
        fingerprint = trusted_static_input_fingerprint(project_root)
        write_json_atomic(fingerprint_path, fingerprint)
    session["pending_trusted_static_inputs"] = {
        "action_id": action_id,
        "fingerprint": fingerprint["fingerprint"],
        "entry_count": fingerprint["entry_count"],
        "total_bytes": fingerprint["total_bytes"],
    }
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event_once(
        project_root,
        session,
        "trusted_inputs_bound",
        "Bound a legacy pending action to controller-owned trusted static inputs.",
        action_id=action_id,
    )


def fresh_workspace_status(project_root: Path) -> dict[str, Any]:
    status = load_sibling_module("workspace_status")
    return status.build_status_document(project_root)


def open_requests(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    source_requests = load_sibling_module("source_requests")
    try:
        records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit as exc:
        raise OrchestrationControllerError("SOURCE_REQUESTS_INVALID", str(exc)) from exc
    selected = [record for record in records if isinstance(record, dict) and record.get("status") == "open"]
    return sorted(
        selected,
        key=lambda item: (
            PRIORITY_ORDER.get(str(item.get("priority", "medium")), 1),
            str(item.get("created_at") or ""),
            str(item.get("request_id") or ""),
        ),
    )


def candidate_store_path(project_root: Path, config: dict[str, Any]) -> Path:
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    discovery = integrations.get("discovery") if isinstance(integrations.get("discovery"), dict) else {}
    value = discovery.get("candidate_store_path", "sources/discovery/candidates.jsonl")
    if not isinstance(value, str) or not value.strip():
        value = "sources/discovery/candidates.jsonl"
    path = PurePosixPath(value.strip().replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("sources",):
        raise OrchestrationControllerError(
            "CONFIG_INVALID",
            "integrations.discovery.candidate_store_path must be workspace-relative under sources/",
        )
    return project_root / path.as_posix()


def raw_tree_snapshot(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded content fingerprint for configured immutable raw roots."""
    raw = config.get("raw") if isinstance(config.get("raw"), dict) else {}
    configured = raw.get("source_roots") if isinstance(raw.get("source_roots"), list) else []
    roots: list[Path] = []
    for value in configured:
        if not isinstance(value, str) or not value.strip():
            continue
        relative = PurePosixPath(value.strip().replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("raw",):
            raise OrchestrationControllerError(
                "CONFIG_INVALID",
                "raw.source_roots must contain workspace-relative paths under raw/",
            )
        roots.append(project_root / relative.as_posix())
    records: list[str] = []
    total_bytes = 0
    seen: set[str] = set()

    def raw_error(message: str) -> OrchestrationControllerError:
        return OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            message,
            recoverable=True,
            remediation="Replace links or special files in raw/ and keep the immutable evidence tree bounded.",
        )

    def visit(path: Path) -> None:
        nonlocal total_bytes
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise raw_error(f"could not inspect immutable raw evidence: {path}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise raw_error(f"immutable raw evidence contains a symbolic link or junction: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                raise raw_error(f"could not enumerate immutable raw evidence: {path}: {exc}") from exc
            for child in children:
                visit(child)
            return
        if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1) or 1) != 1:
            raise raw_error(f"immutable raw evidence is not a singly linked regular file: {path}")
        relative = relative_workspace_path(project_root, path)
        if relative in seen:
            return
        seen.add(relative)
        if len(records) >= MAX_RAW_TREE_SNAPSHOT_ENTRIES:
            raise raw_error(f"immutable raw evidence exceeds {MAX_RAW_TREE_SNAPSHOT_ENTRIES} files")
        declared_size = int(metadata.st_size)
        if declared_size > MAX_RAW_TREE_SNAPSHOT_BYTES - total_bytes:
            raise raw_error(f"immutable raw evidence exceeds {MAX_RAW_TREE_SNAPSHOT_BYTES} bytes")
        try:
            digest = file_digest(
                path,
                max_bytes=declared_size,
                containment_root=project_root,
            )
        except OrchestrationControllerError as exc:
            raise raw_error(f"could not fingerprint immutable raw evidence: {relative}: {exc}") from exc
        if digest is None:
            raise raw_error(f"immutable raw evidence changed while it was fingerprinted: {relative}")
        total_bytes += declared_size
        records.append(f"{relative}\0{declared_size}\0{digest}")

    for root in sorted(set(roots), key=lambda path: path.as_posix()):
        try:
            relative_root = root.relative_to(project_root)
        except ValueError as exc:  # pragma: no cover - roots are assembled above
            raise raw_error(f"immutable raw root escapes the workspace: {root}") from exc
        current = project_root
        root_exists = True
        for part in relative_root.parts:
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                root_exists = False
                break
            except OSError as exc:
                raise raw_error(f"could not inspect immutable raw root: {current}: {exc}") from exc
            if not stat.S_ISDIR(metadata.st_mode) or path_is_link_like(current, metadata):
                raise raw_error(f"immutable raw root ancestor is not a real directory: {current}")
        if root_exists:
            visit(root)
    records.sort()
    digest = hashlib.sha256("\n".join(records).encode()).hexdigest()
    return {
        "algorithm": "sha256-content-v1",
        "file_count": len(records),
        "total_bytes": total_bytes,
        "fingerprint": f"sha256:{digest}",
    }


def evidence_manifest_digest(project_root: Path) -> str | None:
    return file_digest(
        project_root / "sources" / "manifest.jsonl",
        max_bytes=MAX_MANIFEST_SNAPSHOT_BYTES,
        containment_root=project_root,
    )


def bind_legacy_immutability_postconditions(project_root: Path, work_order: dict[str, Any]) -> None:
    """Upgrade pre-0.2.1 pending discovery/review guards before worker replay."""
    if work_order.get("phase") not in {"discovery", "candidate_review"}:
        return
    config = load_config(project_root)
    changed = False
    for item in work_order.get("required_postconditions", []):
        if not isinstance(item, dict):
            continue
        check = item.get("check")
        if check == "raw_tree_unchanged":
            before = item.get("before")
            if not isinstance(before, dict) or before.get("algorithm") != "sha256-content-v1":
                item["before"] = raw_tree_snapshot(project_root, config)
                changed = True
        elif check in {"discovery_never_fetches", "selection_does_not_fetch"}:
            if "manifest_digest_before" not in item:
                item["manifest_digest_before"] = evidence_manifest_digest(project_root)
                changed = True
    if changed:
        write_json_atomic(
            work_order_path(project_root, work_order["orchestration_id"], work_order["action_id"]),
            work_order,
        )


def load_candidates(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    path = candidate_store_path(project_root, config)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                f"invalid candidate JSONL at line {line_number}: {exc}",
            ) from exc
        if isinstance(item, dict):
            records.append(item)
    return records


def candidate_request_id(candidate: dict[str, Any]) -> str | None:
    for field in ("source_request_id", "selected_for_request_id", "selected_request_id", "request_id"):
        value = candidate.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_state(candidate: dict[str, Any]) -> str:
    value = candidate.get("lifecycle_state") or candidate.get("status") or "new"
    return value.strip().lower() if isinstance(value, str) and value.strip() else "new"


def candidate_provider(candidate: dict[str, Any]) -> str | None:
    value = candidate.get("provider")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    paper = candidate.get("paper") if isinstance(candidate.get("paper"), dict) else {}
    value = paper.get("provider")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def acquisition_route(candidate: dict[str, Any], enabled: set[str]) -> str | None:
    provider = candidate_provider(candidate)
    paper = candidate.get("paper") if isinstance(candidate.get("paper"), dict) else {}
    provider_ids = paper.get("provider_ids") if isinstance(paper.get("provider_ids"), dict) else {}
    arxiv_id = provider_ids.get("arxiv") or paper.get("arxiv_id")
    openalex_id = provider_ids.get("openalex") or paper.get("openalex_id")
    doi = provider_ids.get("doi") or paper.get("doi")
    # Academic discovery deduplicates provider records into one neutral paper
    # candidate. Route by retained identities, not only by the primary/top-level
    # discovery provider, so either explicitly enabled academic adapter can be
    # used when the merged record supports it.
    if provider in enabled:
        return provider
    if "arxiv" in enabled and isinstance(arxiv_id, str) and arxiv_id.strip():
        return "arxiv"
    if "openalex" in enabled and any(
        isinstance(value, str) and value.strip() for value in (openalex_id, doi)
    ):
        return "openalex"
    url = candidate.get("url") if isinstance(candidate.get("url"), str) else ""
    host = urlparse(url).hostname or ""
    if "github" in enabled and (host == "github.com" or host.endswith(".github.com")):
        return "github"
    if provider == "search" or (isinstance(provider, str) and provider.startswith("standards")):
        return "web" if "web" in enabled else None
    source_type = str(candidate.get("source_type") or "")
    if source_type in {"web_page", "official_document", "standards_registry_entry", "dataset"}:
        return "web" if "web" in enabled else None
    return None


def composable_discovery_providers(policy: dict[str, Any]) -> list[str]:
    discovery = policy.get("discovery") if isinstance(policy.get("discovery"), dict) else {}
    acquisition = policy.get("acquisition") if isinstance(policy.get("acquisition"), dict) else {}
    if discovery.get("enabled") is not True or acquisition.get("enabled") is not True:
        return []
    acquisition_ids = {
        value for value in acquisition.get("providers", []) if isinstance(value, str) and value
    }
    composable: list[str] = []
    for provider in discovery.get("providers", []):
        if not isinstance(provider, str):
            continue
        if provider in {"arxiv", "openalex"} and acquisition_ids & {"arxiv", "openalex"}:
            composable.append(provider)
        elif provider == "github" and "github" in acquisition_ids:
            composable.append(provider)
        elif provider == "search" and acquisition_ids:
            # Generic search can propose provider-neutral academic, GitHub, or
            # web candidates. Candidate-level postconditions decide whether a
            # returned record actually composes with the enabled acquisition
            # adapters.
            composable.append(provider)
        elif (provider == "standards" or provider.startswith("standards:")) and "web" in acquisition_ids:
            composable.append(provider)
    return sorted(set(composable))


def request_candidates(candidates: list[dict[str, Any]], request_id: str) -> list[dict[str, Any]]:
    return [candidate for candidate in candidates if candidate_request_id(candidate) == request_id]


def safe_relative_artifact(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ARTIFACT_PATH_LENGTH or "\x00" in value:
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts contain an invalid path")
    normalized = value.replace("\\", "/")
    if normalized.startswith(("/", "\\")) or WINDOWS_ABSOLUTE_RE.match(value):
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts must be workspace-relative")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts must not escape the workspace")
    return path.as_posix()


def is_parent_orchestration_artifact(value: str) -> bool:
    parts = tuple(part.casefold().rstrip(" .") for part in PurePosixPath(value.replace("\\", "/")).parts)
    return len(parts) >= 2 and parts[:2] == ("runs", "orchestrations")


def valid_stored_result_shape(document: dict[str, Any], action_id: str) -> bool:
    expected_fields = {"schema_version", "action_id", "outcome", "summary", "artifacts"}
    if set(document) != expected_fields:
        return False
    if document.get("schema_version") != SCHEMA_VERSION or document.get("action_id") != action_id:
        return False
    if document.get("outcome") not in RESULT_OUTCOMES:
        return False
    summary = document.get("summary")
    if (
        not isinstance(summary, str)
        or summary != summary.strip()
        or not summary
        or len(summary) > MAX_SUMMARY_LENGTH
    ):
        return False
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        return False
    try:
        normalized_artifacts = [safe_relative_artifact(value) for value in artifacts]
    except OrchestrationControllerError:
        return False
    return (
        artifacts == normalized_artifacts
        and len(normalized_artifacts) == len(set(normalized_artifacts))
        and not any(is_parent_orchestration_artifact(value) for value in normalized_artifacts)
    )


def load_result(path: Path, action_id: str, project_root: Path) -> dict[str, Any]:
    try:
        path.lstat()
    except OSError as exc:
        raise OrchestrationControllerError("RESULT_UNREADABLE", f"could not read result file: {path}") from exc
    document = load_json_object(
        path,
        error_code="RESULT_INVALID",
        label="orchestration result",
        max_bytes=MAX_RESULT_BYTES,
    )
    expected_fields = {"schema_version", "action_id", "outcome", "summary", "artifacts"}
    if set(document) != expected_fields:
        raise OrchestrationControllerError(
            "RESULT_INVALID",
            "result fields must be exactly schema_version, action_id, outcome, summary, artifacts",
            details={
                "missing": sorted(expected_fields - set(document)),
                "unsupported": sorted(set(document) - expected_fields),
            },
        )
    if document.get("schema_version") != SCHEMA_VERSION or document.get("action_id") != action_id:
        raise OrchestrationControllerError("RESULT_INVALID", "result schema_version or action_id does not match")
    if document.get("outcome") not in RESULT_OUTCOMES:
        raise OrchestrationControllerError("RESULT_INVALID", "result outcome must be completed, blocked, or failed")
    summary = document.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY_LENGTH:
        raise OrchestrationControllerError("RESULT_INVALID", "result summary must contain 1 to 4000 characters")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
        raise OrchestrationControllerError("RESULT_INVALID", "result artifacts must be a bounded list")
    normalized_artifacts = [safe_relative_artifact(value) for value in artifacts]
    if len(normalized_artifacts) != len(set(normalized_artifacts)):
        raise OrchestrationControllerError("RESULT_INVALID", "result artifact paths must be unique")
    if any(is_parent_orchestration_artifact(value) for value in normalized_artifacts):
        raise OrchestrationControllerError(
            "RESULT_INVALID",
            "result artifacts may not reference controller-owned runs/orchestrations state",
        )
    missing = [value for value in normalized_artifacts if not (project_root / value).exists()]
    if missing:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            "reported result artifacts do not exist in the workspace",
            recoverable=True,
            details={"missing_artifacts": missing},
        )
    return {**document, "summary": summary.strip(), "artifacts": normalized_artifacts}


def child_args(
    run_id: str,
    agent_id: str,
    *,
    to_state: str | None = None,
    final_verdict: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        agent_id=agent_id,
        to_state=to_state,
        final_verdict=final_verdict,
        reason=f"Orchestration advanced child run to {to_state or final_verdict}.",
        questions_processed_this_run=None,
        source_requests_opened_this_run=None,
        releases_this_run=None,
        discovery_results_this_run=None,
        acquisition_downloads_this_run=None,
        github_archive_bytes_this_run=None,
        academic_provider_requests_this_run=None,
        web_downloads_this_run=None,
        manual_url_deliveries_this_run=None,
    )


def new_child_run(project_root: Path, session: dict[str, Any]) -> dict[str, Any]:
    controller = load_sibling_module("run_controller")
    sequence = len(session["child_run_ids"]) + 1
    run_id = require_safe_id(f"run-{session['orchestration_id']}-{sequence:03d}", "action_id")
    session["child_run_ids"].append(run_id)
    session["active_run_id"] = run_id
    session["updated_at"] = timestamp_utc()
    # Persist intent before creating the child. If the process stops on either
    # side of run_start, active_child can deterministically create/reload this
    # exact id instead of minting an orphaned second child.
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    return controller.run_start(project_root, child_args(run_id, session["agent_id"]))


def active_child(project_root: Path, session: dict[str, Any]) -> dict[str, Any] | None:
    run_id = session.get("active_run_id")
    if not isinstance(run_id, str):
        return None
    controller = load_sibling_module("run_controller")
    try:
        document = controller.load_run_state(project_root, run_id)
    except Exception as exc:
        if getattr(exc, "error_code", None) == "RUN_UNKNOWN":
            # Recover a parent-persisted child creation intent after a crash
            # between the session write and run_controller.run_start.
            return controller.run_start(project_root, child_args(run_id, session["agent_id"]))
        raise
    if document.get("state", {}).get("current") in controller.TERMINAL_STATES:
        session["active_run_id"] = None
        return None
    return document


def advance_child(project_root: Path, session: dict[str, Any], desired_state: str) -> str:
    controller = load_sibling_module("run_controller")
    document = active_child(project_root, session)
    paths = {
        "discovering": ["planned", "discovering"],
        "candidates_ready": ["planned", "discovering", "candidates_ready"],
        "fetching": ["planned", "discovering", "candidates_ready", "fetch_planned", "fetching"],
        "answering": ["planned", "answering"],
        "verifying": ["planned", "answering", "verifying"],
    }
    if desired_state not in paths:
        raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"unsupported child target: {desired_state}")
    if document is None:
        document = new_child_run(project_root, session)
    current = document["state"]["current"]
    target_path = paths[desired_state]
    if current == desired_state:
        return document["run_id"]
    if current == "evidence_ready" and desired_state in {"answering", "verifying"}:
        target_path = ["answering"] + (["verifying"] if desired_state == "verifying" else [])
    elif current in target_path:
        target_path = target_path[target_path.index(current) + 1 :]
    elif current == "initialized":
        pass
    else:
        # An active child in an unrelated forward-only branch is retained but
        # cannot be repurposed. Close it honestly and create a fresh child.
        allowed = set(document.get("state", {}).get("allowed_next_states") or [])
        final = "blocked_on_sources" if "blocked_on_sources" in allowed else "failed"
        controller.run_finish(project_root, child_args(document["run_id"], session["agent_id"], final_verdict=final))
        session["active_run_id"] = None
        document = new_child_run(project_root, session)
        current = "initialized"
    for state in target_path:
        if document["state"]["current"] == state:
            continue
        document = controller.run_transition(
            project_root,
            child_args(document["run_id"], session["agent_id"], to_state=state),
        )
    return document["run_id"]


def finish_active_child(project_root: Path, session: dict[str, Any], verdict: str) -> None:
    document = active_child(project_root, session)
    if document is None:
        return
    allowed = set(document.get("state", {}).get("allowed_next_states") or [])
    chosen = verdict if verdict in allowed else "failed" if "failed" in allowed else None
    if chosen is None:
        return
    controller = load_sibling_module("run_controller")
    controller.run_finish(
        project_root,
        child_args(document["run_id"], session["agent_id"], final_verdict=chosen),
    )
    session["active_run_id"] = None


def work_order_budgets(status: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    run = status.get("run") if isinstance(status.get("run"), dict) else {}
    allowed = {
        key: value
        for key, value in run.items()
        if key.startswith("max_") and isinstance(value, int) and not isinstance(value, bool)
    }
    allowed["action_timeout_seconds"] = int(session["limits"]["action_timeout_seconds"])
    return allowed


def choose_route(
    project_root: Path,
    session: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    config = load_config(project_root)
    policy = provider_policy(config)
    session["provider_policy"] = policy
    status = fresh_workspace_status(project_root)
    health = status.get("workspace_health") if isinstance(status.get("workspace_health"), dict) else {}
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    verdict = readiness.get("verdict")

    if not health.get("materially_valid", False) or verdict == "attention_required":
        return None, {
            "terminal_status": "no_ship",
            "reason": "Workspace health or HIGH validation findings require operator attention.",
            "workspace_status": status,
        }

    if verdict == "in_progress":
        questions = status.get("questions") if isinstance(status.get("questions"), dict) else {}
        slugs = questions.get("actionable_slugs") if isinstance(questions.get("actionable_slugs"), list) else []
        return "research", {
            "status": status,
            "scope": {"question_slugs": [str(value) for value in slugs[:25]], "request_ids": [], "candidate_ids": []},
        }

    if verdict == "blocked_on_sources":
        requests = open_requests(project_root, config)
        candidates = load_candidates(project_root, config)
        acquisition = policy["acquisition"]
        acquisition_providers = set(acquisition["providers"]) if acquisition["enabled"] else set()
        discovery_providers = composable_discovery_providers(policy)
        route_failures: list[dict[str, Any]] = []
        for request in requests:
            request_id = str(request.get("request_id") or "")
            if not request_id:
                continue
            scoped = request_candidates(candidates, request_id)
            routable = [
                candidate
                for candidate in scoped
                if candidate_state(candidate) == "selected" and acquisition_route(candidate, acquisition_providers)
            ]
            if routable:
                return "acquisition", {
                    "status": status,
                    "scope": {
                        "question_slugs": [
                            str(value) for value in request.get("question_slugs", []) if isinstance(value, str)
                        ],
                        "request_ids": [request_id],
                        "candidate_ids": [
                            str(item.get("candidate_id")) for item in routable if item.get("candidate_id")
                        ],
                    },
                    "request": request,
                }
            reviewable = [
                candidate
                for candidate in scoped
                if candidate_state(candidate) in {"new", "proposed", "discovered", "reviewed", "deferred"}
                and acquisition_route(candidate, acquisition_providers) is not None
            ]
            if reviewable:
                return "candidate_review", {
                    "status": status,
                    "scope": {
                        "question_slugs": [
                            str(value) for value in request.get("question_slugs", []) if isinstance(value, str)
                        ],
                        "request_ids": [request_id],
                        "candidate_ids": [
                            str(item.get("candidate_id")) for item in reviewable if item.get("candidate_id")
                        ],
                    },
                    "request": request,
                }
            if scoped:
                route_failures.append(
                    {
                        "request_id": request_id,
                        "reason": "existing candidates have no remaining explicitly enabled acquisition route",
                        "candidate_ids": [
                            str(item.get("candidate_id")) for item in scoped if item.get("candidate_id")
                        ],
                    }
                )
                continue
            if discovery_providers:
                return "discovery", {
                    "status": status,
                    "scope": {
                        "question_slugs": [
                            str(value) for value in request.get("question_slugs", []) if isinstance(value, str)
                        ],
                        "request_ids": [request_id],
                        "candidate_ids": [],
                    },
                    "request": request,
                    "candidate_count_before": 0,
                    "discovery_providers": discovery_providers,
                }
            route_failures.append(
                {
                    "request_id": request_id,
                    "reason": "no discovery provider composes with an enabled acquisition provider",
                    "discovery_providers": policy["discovery"]["providers"],
                    "acquisition_providers": policy["acquisition"]["providers"],
                }
            )
        return None, {
            "terminal_status": "blocked_on_sources",
            "reason": (
                "No permitted end-to-end provider route can satisfy the open source requests. Enable a composable "
                "discovery/acquisition pair in research.yml, refine or replace exhausted candidates, or deliver "
                "reviewed evidence manually. "
                f"Effective discovery providers: {', '.join(policy['discovery']['providers']) or 'none'}; "
                f"effective acquisition providers: {', '.join(policy['acquisition']['providers']) or 'none'}; "
                f"blocked request ids: {', '.join(str(item.get('request_id')) for item in route_failures) or 'none'}."
            ),
            "workspace_status": status,
            "route_failures": route_failures,
        }

    if verdict == "complete":
        return "verification", {
            "status": status,
            "scope": {"question_slugs": [], "request_ids": [], "candidate_ids": []},
        }

    return None, {
        "terminal_status": "failed",
        "reason": f"Unsupported workspace readiness verdict: {verdict!r}.",
        "workspace_status": status,
    }


def action_spec(
    project_root: Path,
    session: dict[str, Any],
    route: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    status = context["status"]
    scope = context["scope"]
    config = load_config(project_root)
    candidates_input = relative_workspace_path(project_root, candidate_store_path(project_root, config))
    effective_policy = session["provider_policy"]
    run_id: str | None
    skill: str
    inputs = ["research.yml", "AGENTS.md"]
    postconditions: list[dict[str, Any]]
    if route == "research":
        run_id = advance_child(project_root, session, "answering")
        skill = "research-run"
        inputs.extend(["wiki/questions", "sources/normalized", f"runs/{run_id}/run-state.json"])
        postconditions = [
            {
                "check": "workspace_readiness_changed",
                "allowed_verdicts": ["in_progress", "blocked_on_sources", "complete"],
            },
            {"check": "child_run_state", "expected": "answering"},
        ]
    elif route == "discovery":
        run_id = advance_child(project_root, session, "discovering")
        skill = "research-discover"
        inputs.extend(["sources/source-requests.jsonl", candidates_input])
        permitted = list(context.get("discovery_providers") or [])
        effective_policy = {
            "discovery": {"enabled": bool(permitted), "providers": permitted},
            "acquisition": dict(session["provider_policy"]["acquisition"]),
        }
        postconditions = [
            {
                "check": "request_scoped_candidates_increased",
                "before": int(context.get("candidate_count_before", 0) or 0),
            },
            {
                "check": "discovery_never_fetches",
                "manifest_records_before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "manifest_digest_before": evidence_manifest_digest(project_root),
            },
            {"check": "raw_tree_unchanged", "before": raw_tree_snapshot(project_root, config)},
        ]
    elif route == "candidate_review":
        run_id = advance_child(project_root, session, "candidates_ready")
        skill = "research-discover"
        inputs.extend(["sources/source-requests.jsonl", candidates_input])
        postconditions = [
            {"check": "selected_candidate_for_request", "selected_before": 0},
            {
                "check": "selection_does_not_fetch",
                "manifest_records_before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "manifest_digest_before": evidence_manifest_digest(project_root),
            },
            {"check": "raw_tree_unchanged", "before": raw_tree_snapshot(project_root, config)},
        ]
    elif route == "acquisition":
        run_id = advance_child(project_root, session, "fetching")
        skill = "research-acquire"
        inputs.extend(["sources/source-requests.jsonl", candidates_input, "sources/manifest.jsonl"])
        postconditions = [
            {"check": "request_fulfilled_with_normalized_source"},
            {"check": "linked_blocked_questions_reopened"},
            {
                "check": "manifest_records_increased",
                "before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
            },
        ]
    elif route == "verification":
        run_id = advance_child(project_root, session, "verifying")
        skill = "research-verify"
        inputs.extend(["wiki/questions", "sources/normalized", "sources/manifest.jsonl"])
        evaluation_root = f"runs/{run_id}/evaluation"
        verification_paths = [
            f"{evaluation_root}/citation-verification.json",
            f"{evaluation_root}/export.json",
            f"{evaluation_root}/lint.json",
            f"{evaluation_root}/publication-readiness.json",
        ]
        postconditions = [
            {
                "check": "fresh_verification_bundle",
                "paths": verification_paths,
                "before": {
                    path: file_digest(project_root / path, containment_root=project_root)
                    for path in verification_paths
                },
            },
            {"check": "publication_readiness", "expected": "ship"},
        ]
    else:  # pragma: no cover - internal guard
        raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"unknown route: {route}")
    return {
        "phase": route,
        "skill": skill,
        "run_id": run_id,
        "scope": scope,
        "provider_policy": effective_policy,
        "budgets": work_order_budgets(status, session),
        "inputs": sorted(set(inputs)),
        "required_postconditions": postconditions,
    }


def issue_work_order(project_root: Path, session: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    action_number = int(session["action_count"]) + 1
    action_id = f"action-{action_number:04d}"
    lease_seconds = int(session["limits"]["action_timeout_seconds"])
    work_order = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": WORK_ORDER_ARTIFACT_TYPE,
        "orchestration_id": session["orchestration_id"],
        "action_id": action_id,
        "issued_at": format_timestamp(now),
        "phase": spec["phase"],
        "skill": spec["skill"],
        "run_id": spec["run_id"],
        "agent_id": session["agent_id"],
        "scope": spec["scope"],
        "provider_policy": spec["provider_policy"],
        "budgets": spec["budgets"],
        "inputs": spec["inputs"],
        "required_postconditions": spec["required_postconditions"],
        "lease": {
            "duration_seconds": lease_seconds,
            "expires_at": format_timestamp(now + timedelta(seconds=lease_seconds)),
            "attempt": 1,
        },
    }
    static_fingerprint = trusted_static_input_fingerprint(project_root)
    write_json_atomic(
        trusted_static_input_path(project_root, session["orchestration_id"], action_id),
        static_fingerprint,
    )
    write_json_atomic(work_order_path(project_root, session["orchestration_id"], action_id), work_order)
    session["phase"] = spec["phase"]
    session["pending_action_id"] = action_id
    session["pending_submission"] = None
    session["pending_trusted_static_inputs"] = {
        "action_id": action_id,
        "fingerprint": static_fingerprint["fingerprint"],
        "entry_count": static_fingerprint["entry_count"],
        "total_bytes": static_fingerprint["total_bytes"],
    }
    session["recovery"] = default_recovery_state()
    session["action_count"] = action_number
    session["window_action_count"] = int(session.get("window_action_count", 0)) + 1
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(project_root, session, "action_issued", f"Issued {spec['phase']} work order.", action_id=action_id)
    return work_order


def replay_work_order(
    project_root: Path,
    session: dict[str, Any],
    *,
    resume: bool,
    retained_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_id = require_safe_id(session.get("pending_action_id"), "action_id")
    path = work_order_path(project_root, session["orchestration_id"], action_id)
    if retained_order is None:
        work_order = load_json_object(path, error_code="WORK_ORDER_INVALID", label="work order")
        verify_runtime_guards(project_root, session, work_order)
    else:
        work_order = retained_order
    lease = work_order.get("lease") if isinstance(work_order.get("lease"), dict) else {}
    expires_at = parse_timestamp(lease.get("expires_at"))
    if resume and expires_at is not None and expires_at <= datetime.now(timezone.utc):
        now = datetime.now(timezone.utc)
        duration = int(lease.get("duration_seconds", session["limits"]["action_timeout_seconds"]) or 0)
        attempt = int(lease.get("attempt", 1) or 1) + 1
        work_order["issued_at"] = format_timestamp(now)
        work_order["lease"] = {
            "duration_seconds": duration,
            "expires_at": format_timestamp(now + timedelta(seconds=duration)),
            "attempt": attempt,
        }
        write_json_atomic(path, work_order)
        session["recovery"] = {
            "state": RECOVERY_RECONCILE,
            "action_id": action_id,
            "attempt": attempt,
            "reason_code": "result_absent_after_interruption",
            "recorded_at": timestamp_utc(),
        }
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
        record_event(
            project_root,
            session,
            "action_reissued",
            "Reissued expired work order for same-action reconciliation.",
            action_id=action_id,
            data={"lease_attempt": attempt, "recovery_mode": "reconcile"},
        )
    return work_order


def pause_if_limited(project_root: Path, session: dict[str, Any]) -> bool:
    limits = session["limits"]
    reason: str | None = None
    if int(session.get("window_action_count", 0)) >= int(limits["max_actions"]):
        reason = "max_actions reached for this orchestration window"
    started = parse_timestamp(session.get("window_started_at")) or datetime.now(timezone.utc)
    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    if elapsed >= int(limits["total_timeout_seconds"]):
        reason = "total_timeout_seconds reached for this orchestration window"
    if reason is None:
        return False
    session["status"] = PAUSED_STATUS
    session["phase"] = "paused"
    session["verdict"] = "paused"
    session["pause_reason"] = reason
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(project_root, session, "session_paused", reason)
    return True


def resume_session(project_root: Path, session: dict[str, Any]) -> None:
    if session["status"] != PAUSED_STATUS:
        return
    session["status"] = ACTIVE_STATUS
    session["phase"] = "planning"
    session["verdict"] = None
    session["pause_reason"] = None
    session["window_action_count"] = 0
    session["window_started_at"] = timestamp_utc()
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(project_root, session, "session_resumed", "Started a fresh bounded orchestration window.")


def finish_session(
    project_root: Path,
    session: dict[str, Any],
    status: str,
    reason: str,
) -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"invalid terminal status: {status}")
    if status in {"blocked_on_sources", "no_ship", "failed"}:
        child_verdict = "blocked_on_sources" if status == "blocked_on_sources" else status
        finish_active_child(project_root, session, child_verdict)
    session["status"] = status
    session["phase"] = status
    session["verdict"] = status
    session["pending_action_id"] = None
    if "pending_trusted_static_inputs" in session:
        session["pending_trusted_static_inputs"] = None
    session["pause_reason"] = reason if status != "complete" else None
    session["updated_at"] = timestamp_utc()
    session["completed_at"] = session["updated_at"]
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    record_event(project_root, session, "session_finished", reason)
    return session


def start_session(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    agent_id = require_agent_id(args.agent_id)
    orchestration_id = require_safe_id(args.orchestration_id or generated_orchestration_id(), "orchestration_id")
    # Refuse before creating durable state when the workspace contract cannot be read.
    status = fresh_workspace_status(project_root)
    health = status.get("workspace_health") if isinstance(status.get("workspace_health"), dict) else {}
    if not health.get("materially_valid", False):
        raise OrchestrationControllerError(
            "WORKSPACE_UNREADABLE",
            "workspace health rejected the research contract",
            details={"findings": health.get("findings", [])},
        )
    with workspace_lock(session_lock_path(project_root, orchestration_id), purpose=f"orchestration {orchestration_id}"):
        path = session_path(project_root, orchestration_id)
        if path.exists():
            raise OrchestrationControllerError(
                "ORCHESTRATION_EXISTS",
                f"orchestration already exists: {orchestration_id}",
            )
        now = timestamp_utc()
        config = load_config(project_root)
        project = config.get("project") if isinstance(config.get("project"), dict) else {}
        handoff = project.get("handoff") if isinstance(project.get("handoff"), dict) else None
        session: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "artifact_type": SESSION_ARTIFACT_TYPE,
            "orchestration_id": orchestration_id,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "agent_id": agent_id,
            "handoff": handoff,
            "status": ACTIVE_STATUS,
            "phase": "planning",
            "verdict": None,
            "pause_reason": None,
            "pending_action_id": None,
            "pending_submission": None,
            "pending_trusted_static_inputs": None,
            "recovery": default_recovery_state(),
            "last_completed_action_id": None,
            "active_run_id": None,
            "child_run_ids": [],
            "action_count": 0,
            "completed_action_count": 0,
            "window_action_count": 0,
            "window_started_at": now,
            "limits": {
                "max_actions": args.max_actions,
                "action_timeout_seconds": args.action_timeout_seconds,
                "total_timeout_seconds": args.total_timeout_seconds,
            },
            "provider_policy": provider_policy(config),
            "failure_records": [],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / WORK_ORDERS_DIR).mkdir(parents=True, exist_ok=True)
        (path.parent / WORK_RESULTS_DIR).mkdir(parents=True, exist_ok=True)
        (path.parent / TRUSTED_INPUTS_DIR).mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, session)
        record_event(project_root, session, "session_started", "Orchestration session created.")
        return session


def next_work(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    orchestration_id = require_safe_id(args.orchestration_id, "orchestration_id")
    with workspace_lock(session_lock_path(project_root, orchestration_id), purpose=f"orchestration {orchestration_id}"):
        session = load_session(project_root, orchestration_id)
        enforce_control_repair_gate(project_root, orchestration_id)
        if args.agent_id is not None and require_agent_id(args.agent_id) != session["agent_id"]:
            raise OrchestrationControllerError(
                "ORCHESTRATION_OWNER_MISMATCH",
                "--agent-id does not own this orchestration session",
                recoverable=False,
            )
        pending_submission = session.get("pending_submission")
        if pending_submission is not None:
            action_id = require_safe_id(pending_submission.get("action_id"), "action_id")
            order = load_json_object(
                work_order_path(project_root, orchestration_id, action_id),
                error_code="WORK_ORDER_INVALID",
                label="work order",
            )
            verify_runtime_guards(project_root, session, order)
            session = finalize_pending_submission(project_root, session, order)
        repair_last_completion_events(project_root, session)
        if session["status"] in TERMINAL_STATUSES:
            return session
        if args.resume:
            resume_session(project_root, session)
        elif session["status"] == PAUSED_STATUS:
            return session
        if session.get("pending_action_id"):
            action_id = require_safe_id(session["pending_action_id"], "action_id")
            order = load_json_object(
                work_order_path(project_root, orchestration_id, action_id),
                error_code="WORK_ORDER_INVALID",
                label="work order",
            )
            legacy_unbound = "pending_trusted_static_inputs" not in session
            if legacy_unbound:
                # Only parse the declarative YAML authorization before the
                # migration snapshot exists. Bind all trusted workspace code
                # before fresh_workspace_status imports or executes it.
                verify_provider_policy_unchanged(project_root, session, order)
                bind_legacy_pending_trusted_inputs(project_root, session, order)
            verify_runtime_guards(project_root, session, order)
            bind_legacy_immutability_postconditions(project_root, order)
            return replay_work_order(project_root, session, resume=args.resume, retained_order=order)
        if pause_if_limited(project_root, session):
            return session
        route, context = choose_route(project_root, session)
        if route is None:
            return finish_session(project_root, session, context["terminal_status"], context["reason"])
        spec = action_spec(project_root, session, route, context)
        return issue_work_order(project_root, session, spec)


def selected_candidates_for_scope(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in load_candidates(project_root, config)
        if candidate_request_id(candidate) in set(request_ids) and candidate_state(candidate) == "selected"
    ]


def strip_generated_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_generated_timestamps(child)
            for key, child in value.items()
            if key != "generated_at"
        }
    if isinstance(value, list):
        return [strip_generated_timestamps(child) for child in value]
    return value


def citation_results_by_source(citation: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    selected: dict[str, list[dict[str, Any]]] = {}
    results = citation.get("results") if isinstance(citation.get("results"), list) else []
    for result in results:
        if not isinstance(result, dict):
            continue
        source_id = result.get("source_id")
        if isinstance(source_id, str) and source_id.strip():
            selected.setdefault(source_id.strip(), []).append(dict(result))
    return selected


def export_with_authoritative_citations(
    export: dict[str, Any],
    citation: dict[str, Any],
) -> dict[str, Any]:
    normalized = json.loads(json.dumps(export))
    by_source = citation_results_by_source(citation)
    questions = normalized.get("questions") if isinstance(normalized.get("questions"), list) else []
    for question in questions:
        if not isinstance(question, dict):
            continue
        source_ids = question.get("source_ids") if isinstance(question.get("source_ids"), list) else []
        question["citation_verification"] = [
            dict(result)
            for source_id in source_ids
            if isinstance(source_id, str)
            for result in by_source.get(source_id, [])
        ]
    return normalized


def build_authoritative_verification(
    project_root: Path,
    run_id: str,
) -> dict[str, dict[str, Any]]:
    """Recompute all publication inputs in memory without trusting or writing worker JSON."""
    try:
        config = load_config(project_root)
        status_module = load_sibling_module("workspace_status")
        lint_module = load_sibling_module("lint")
        export_module = load_sibling_module("export_answers")
        citation_module = load_sibling_module("verify_citations")
        readiness_module = load_sibling_module("publication_readiness")

        run_state = project_root / "runs" / run_id / "run-state.json"
        status = status_module.build_status_document(project_root, run_id=run_id if run_state.is_file() else None)
        lint_report = lint_module.run_checks(project_root, config)
        citation = citation_module.build_report(
            project_root,
            SimpleNamespace(source_id=None, live=False, provider=None),
        )
        authoritative_by_source = citation_results_by_source(citation)
        original_loader = export_module.load_citation_verification_by_source

        def load_authoritative_citations(_root: Path, _warnings: list[str]) -> dict[str, list[dict[str, Any]]]:
            return authoritative_by_source

        export_module.load_citation_verification_by_source = load_authoritative_citations
        try:
            export = export_module.build_export(project_root, None)
        finally:
            export_module.load_citation_verification_by_source = original_loader
        export = export_with_authoritative_citations(export, citation)
        publication = readiness_module.build_readiness_document(
            project_root,
            embedded_inputs={
                "status": status,
                "lint": lint_report,
                "export": export,
                "citation_verification": citation,
            },
        )
    except OrchestrationControllerError:
        raise
    except (Exception, SystemExit) as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"could not recompute authoritative verification inputs: {exc}",
            recoverable=True,
            remediation="Repair the workspace artifacts, regenerate the verification bundle, and retry the action.",
        ) from exc
    return {
        "citation-verification.json": citation,
        "lint.json": lint_report,
        "export.json": export,
        "publication-readiness.json": publication,
    }


def verification_semantic_value(
    name: str,
    document: dict[str, Any],
    citation: dict[str, Any],
) -> Any:
    value: Any = document
    if name == "export.json":
        value = export_with_authoritative_citations(document, citation)
    elif name == "publication-readiness.json":
        value = {
            key: child
            for key, child in document.items()
            if key not in {"generated_at", "workspace_status"}
        }
    return strip_generated_timestamps(value)


def verify_action_postconditions(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    *,
    apply_effects: bool = False,
) -> tuple[str | None, str | None]:
    phase = work_order.get("phase")
    scope = work_order.get("scope") if isinstance(work_order.get("scope"), dict) else {}
    request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
    config = load_config(project_root)
    status = fresh_workspace_status(project_root)
    verdict = status.get("readiness", {}).get("verdict")
    controller = load_sibling_module("run_controller")
    run_id = work_order.get("run_id")
    run_state = controller.load_run_state(project_root, run_id) if isinstance(run_id, str) else None
    current = run_state.get("state", {}).get("current") if isinstance(run_state, dict) else None

    def require(
        condition: bool,
        message: str,
        details: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> None:
        if not condition:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                message,
                recoverable=True,
                remediation=remediation or "Complete the persisted work order and resubmit the same action id.",
                details=details,
            )

    def recorded_postcondition(check: str) -> dict[str, Any]:
        return next(
            (
                item
                for item in work_order.get("required_postconditions", [])
                if isinstance(item, dict) and item.get("check") == check
            ),
            {},
        )

    def require_raw_unchanged() -> None:
        expected = recorded_postcondition("raw_tree_unchanged").get("before")
        actual = raw_tree_snapshot(project_root, config)
        require(
            isinstance(expected, dict) and actual == expected,
            f"{phase} changed the immutable raw evidence tree",
            {"before": expected, "after": actual},
            "Restore raw/ to its pre-action state; discovery and review may only mutate the candidate store.",
        )

    if phase == "research":
        require(
            verdict in {"in_progress", "blocked_on_sources", "complete"},
            "research produced an invalid readiness verdict",
        )
        if verdict == "blocked_on_sources":
            require(
                current in {"answering", "blocked_on_sources"},
                "research child run is not answering or durably blocked",
            )
            if apply_effects and current == "answering":
                finish_active_child(project_root, session, "blocked_on_sources")
            elif apply_effects:
                session["active_run_id"] = None
            return "planning", None
        if verdict == "complete":
            require(current in {"answering", "verifying"}, "research child run cannot advance to verification")
            if apply_effects and current == "answering":
                controller.run_transition(project_root, child_args(run_id, session["agent_id"], to_state="verifying"))
            return "verification", None
        require(current == "answering", "research child run is no longer in answering state")
        return "research", None

    if phase == "discovery":
        candidates = [
            candidate
            for candidate in load_candidates(project_root, config)
            if candidate_request_id(candidate) in set(request_ids)
        ]
        before_candidates = int(recorded_postcondition("request_scoped_candidates_increased").get("before", 0) or 0)
        require(
            len(candidates) > before_candidates,
            "discovery produced no new request-scoped candidate",
            {"request_ids": request_ids, "before": before_candidates, "after": len(candidates)},
            "Refine the source request or enable a different composable discovery/acquisition provider pair.",
        )
        before_manifest = int(recorded_postcondition("discovery_never_fetches").get("manifest_records_before", 0) or 0)
        current_manifest = int(status.get("sources", {}).get("manifest_records", 0) or 0)
        require(current_manifest == before_manifest, "discovery changed the evidence manifest")
        before_manifest_digest = recorded_postcondition("discovery_never_fetches").get("manifest_digest_before")
        require(
            evidence_manifest_digest(project_root) == before_manifest_digest,
            "discovery changed existing evidence-manifest content",
        )
        require_raw_unchanged()
        acquisition = work_order.get("provider_policy", {}).get("acquisition", {})
        enabled = set(acquisition.get("providers", [])) if acquisition.get("enabled") is True else set()
        routable = [candidate for candidate in candidates if acquisition_route(candidate, enabled) is not None]
        require(
            bool(routable),
            "discovery candidates have no route through the work order's acquisition policy",
            {"request_ids": request_ids, "enabled_acquisition_providers": sorted(enabled)},
            "Enable a matching acquisition provider, refine discovery, or deliver reviewed evidence manually.",
        )
        require(current in {"discovering", "candidates_ready"}, "discovery child run is in an invalid state")
        if apply_effects and current == "discovering":
            controller.run_transition(project_root, child_args(run_id, session["agent_id"], to_state="candidates_ready"))
        return "candidate_review", None

    if phase == "candidate_review":
        before_manifest = int(recorded_postcondition("selection_does_not_fetch").get("manifest_records_before", 0) or 0)
        current_manifest = int(status.get("sources", {}).get("manifest_records", 0) or 0)
        require(current_manifest == before_manifest, "candidate review changed the evidence manifest")
        before_manifest_digest = recorded_postcondition("selection_does_not_fetch").get("manifest_digest_before")
        require(
            evidence_manifest_digest(project_root) == before_manifest_digest,
            "candidate review changed existing evidence-manifest content",
        )
        require_raw_unchanged()
        require(current == "candidates_ready", "candidate-review child run is not in candidates_ready state")
        selected = selected_candidates_for_scope(project_root, config, request_ids)
        require(bool(selected), "candidate review did not select a candidate for the request")
        policy = provider_policy(config)
        enabled = set(policy["acquisition"]["providers"]) if policy["acquisition"]["enabled"] else set()
        routable = [candidate for candidate in selected if acquisition_route(candidate, enabled) is not None]
        require(
            bool(routable),
            "selected candidates have no explicitly enabled acquisition route",
            {"request_ids": request_ids, "enabled_acquisition_providers": sorted(enabled)},
        )
        return "acquisition", None

    if phase == "acquisition":
        requests = open_requests(project_root, config)
        still_open = {str(item.get("request_id")) for item in requests}
        require(not set(request_ids) & still_open, "acquisition did not fulfill the scoped source request")
        source_requests = load_sibling_module("source_requests")
        all_requests = source_requests.load_requests(source_requests.requests_path(project_root, config))
        fulfilled = [
            item
            for item in all_requests
            if item.get("request_id") in set(request_ids) and item.get("status") == "fulfilled"
        ]
        require(
            bool(fulfilled) and all(item.get("source_id") for item in fulfilled),
            "fulfilled request lacks a manifest source id",
        )
        normalize_sources = load_sibling_module("normalize_sources")
        manifest_relative, normalized_relative = normalize_sources.source_paths(config)
        manifest_records = normalize_sources.load_manifest(project_root / manifest_relative)
        by_source_id = normalize_sources.records_by_source_id(manifest_records)
        normalized_root = project_root / normalized_relative
        missing_normalized: list[str] = []
        for request in fulfilled:
            source_id = str(request.get("source_id") or "")
            record = by_source_id.get(source_id)
            if record is None or not normalize_sources.normalized_output_path_for_record(record, normalized_root).is_file():
                missing_normalized.append(source_id)
        require(
            not missing_normalized,
            "fulfilled source requests do not have normalized evidence",
            {"source_ids": missing_normalized},
        )
        linked_question_slugs = {
            str(slug)
            for request in fulfilled
            for slug in request.get("question_slugs", [])
            if isinstance(slug, str) and slug
        }
        blocked_slugs = set(status.get("questions", {}).get("blocked_slugs", []))
        require(
            not linked_question_slugs & blocked_slugs,
            "questions linked to fulfilled evidence remain blocked",
            {"question_slugs": sorted(linked_question_slugs & blocked_slugs)},
        )
        sources = status.get("sources") if isinstance(status.get("sources"), dict) else {}
        before_manifest = 0
        for item in work_order.get("required_postconditions", []):
            if isinstance(item, dict) and item.get("check") == "manifest_records_increased":
                before_manifest = int(item.get("before", 0) or 0)
        require(
            int(sources.get("manifest_records", 0) or 0) > before_manifest,
            "acquisition did not add manifest evidence",
        )
        require(current in {"fetching", "evidence_ready"}, "acquisition child run is in an invalid state")
        if apply_effects and current == "fetching":
            controller.run_transition(project_root, child_args(run_id, session["agent_id"], to_state="evidence_ready"))
        return "research", None

    if phase == "verification":
        require(current in {"verifying", "complete"}, "verification child run is in an invalid state")
        evaluation_dir = project_root / "runs" / str(run_id) / "evaluation"
        expected_relative_paths = [
            f"runs/{run_id}/evaluation/citation-verification.json",
            f"runs/{run_id}/evaluation/export.json",
            f"runs/{run_id}/evaluation/lint.json",
            f"runs/{run_id}/evaluation/publication-readiness.json",
        ]
        bundle_postcondition = recorded_postcondition("fresh_verification_bundle")
        recorded_paths = bundle_postcondition.get("paths")
        require(
            isinstance(recorded_paths, list) and recorded_paths == expected_relative_paths,
            "verification work order does not name the canonical bundle artifacts",
        )
        actual_digests = {
            path: file_digest(project_root / path, containment_root=project_root)
            for path in expected_relative_paths
        }
        missing_bundle = [path for path, digest in actual_digests.items() if digest is None]
        require(
            not missing_bundle,
            "fresh verification bundle is incomplete",
            {"missing_artifacts": missing_bundle},
        )
        before_digests = bundle_postcondition.get("before")
        if isinstance(before_digests, dict) and any(value is not None for value in before_digests.values()):
            require(
                any(actual_digests.get(path) != before_digests.get(path) for path in expected_relative_paths),
                "verification bundle was not refreshed after the work order was issued",
                {"paths": expected_relative_paths},
            )
        labels = {
            "citation-verification.json": "fresh citation verification",
            "export.json": "fresh answer export",
            "lint.json": "fresh lint report",
            "publication-readiness.json": "fresh publication readiness",
        }
        worker_documents = {
            name: load_json_object(
                evaluation_dir / name,
                error_code="ORCHESTRATION_POSTCONDITION_FAILED",
                label=label,
                max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
                containment_root=project_root,
            )
            for name, label in labels.items()
        }
        authoritative = build_authoritative_verification(project_root, str(run_id))
        authoritative_citation = authoritative["citation-verification.json"]
        for name in labels:
            worker_semantics = verification_semantic_value(
                name,
                worker_documents[name],
                authoritative_citation,
            )
            authoritative_semantics = verification_semantic_value(
                name,
                authoritative[name],
                authoritative_citation,
            )
            require(
                worker_semantics == authoritative_semantics,
                f"worker verification artifact does not match authoritative recomputation: {name}",
                {"artifact": f"runs/{run_id}/evaluation/{name}"},
                "Regenerate the deterministic verification bundle from current workspace artifacts and retry.",
            )
        citation = authoritative_citation
        lint = authoritative["lint.json"]
        export = authoritative["export.json"]
        readiness = authoritative["publication-readiness.json"]
        answered_slugs = [
            str(question.get("slug"))
            for question in export.get("questions", [])
            if isinstance(question, dict)
            and question.get("status") == "answered"
            and isinstance(question.get("slug"), str)
            and isinstance(question.get("grounding"), list)
            and bool(question["grounding"])
        ]
        if answered_slugs:
            quotes = load_sibling_module("verify_quotes").build_report(
                project_root,
                SimpleNamespace(slug=answered_slugs),
            )
        else:
            quotes = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": timestamp_utc(),
                "network_io_executed": False,
                "questions": [],
                "counts": {"questions": 0, "grounding_entries": 0, "verified": 0, "failed": 0, "missing_grounding": 0},
                "overall_result": "verified",
            }
        coverage = status.get("coverage") if isinstance(status.get("coverage"), dict) else {}
        coverage_report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": timestamp_utc(),
            "network_io_executed": False,
            "coverage": coverage,
        }
        citation_counts = citation.get("counts") if isinstance(citation.get("counts"), dict) else {}
        citation_total = int(citation_counts.get("total", 0) or 0)
        require(
            citation_total == 0 or citation.get("overall_result") == "verified",
            "fresh citation verification did not verify every selected academic source",
            {"overall_result": citation.get("overall_result"), "counts": citation_counts},
        )
        lint_counts = lint.get("stats", {}).get("issue_counts", {}) if isinstance(lint.get("stats"), dict) else {}
        require(int(lint_counts.get("HIGH", 0) or 0) == 0, "fresh lint report contains HIGH findings")
        required_coverage = coverage.get("required_question_counts")
        required_coverage = required_coverage if isinstance(required_coverage, dict) else {}
        require(
            all(int(required_coverage.get(key, 0) or 0) == 0 for key in ("blocked", "pending", "missing", "invalid")),
            "fresh coverage summary contains unresolved required coverage",
            {"required_question_counts": required_coverage},
        )
        require(quotes.get("overall_result") == "verified", "fresh quote verification did not pass")
        require(
            readiness.get("verdict") == "ship",
            "fresh publication readiness is not ship",
            {"verdict": readiness.get("verdict")},
        )
        if apply_effects:
            for name in labels:
                write_json_atomic(evaluation_dir / name, authoritative[name])
            write_json_atomic(evaluation_dir / "quote-verification.json", quotes)
            write_json_atomic(evaluation_dir / "coverage-summary.json", coverage_report)
            write_json_atomic(answers_path(project_root, session["orchestration_id"]), export)
            if current == "verifying":
                controller.run_finish(project_root, child_args(run_id, session["agent_id"], final_verdict="complete"))
            session["active_run_id"] = None
        return "complete", "Fresh publication readiness returned ship and answers were exported."

    raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"unsupported submitted phase: {phase}")


def prepare_submission(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if result["outcome"] == "completed":
        next_phase, completion_reason = verify_action_postconditions(
            project_root,
            session,
            work_order,
            apply_effects=False,
        )
    elif result["outcome"] == "blocked":
        next_phase, completion_reason = "blocked_on_sources", result["summary"]
    else:
        next_phase, completion_reason = "failed", result["summary"]
    pending = {
        "action_id": result["action_id"],
        "accepted_at": timestamp_utc(),
        "result": result,
        "result_digest": result_digest(result),
        "next_phase": next_phase,
        "completion_reason": completion_reason,
    }
    session["pending_submission"] = pending
    lease = work_order.get("lease") if isinstance(work_order.get("lease"), dict) else {}
    session["recovery"] = {
        "state": RECOVERY_FINALIZING,
        "action_id": result["action_id"],
        "attempt": int(lease.get("attempt", 1) or 1),
        "reason_code": "accepted_result_pending_finalization",
        "recorded_at": timestamp_utc(),
    }
    session["updated_at"] = timestamp_utc()
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    return pending


def retained_result(
    project_root: Path,
    orchestration_id: str,
    action_id: str,
) -> dict[str, Any] | None:
    path = work_result_path(project_root, orchestration_id, action_id)
    if not path.is_file():
        return None
    return load_result(path, action_id, project_root)


def ensure_completion_events(
    project_root: Path,
    session: dict[str, Any],
    result: dict[str, Any],
) -> None:
    action_id = result["action_id"]
    record_event_once(
        project_root,
        session,
        "action_completed",
        result["summary"],
        action_id=action_id,
        data={"artifacts": result["artifacts"], "outcome": result["outcome"]},
    )
    if session.get("status") in TERMINAL_STATUSES:
        reason = session.get("pause_reason") or result["summary"]
        record_event_once(
            project_root,
            session,
            "session_finished",
            str(reason),
            data={"status": session["status"]},
        )


def finalize_pending_submission(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
) -> dict[str, Any]:
    pending = session.get("pending_submission")
    if not valid_pending_submission(pending) or pending is None:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "parent session does not contain a valid accepted submission",
            recoverable=False,
        )
    action_id = require_safe_id(pending["action_id"], "action_id")
    result = pending["result"]
    if session.get("pending_action_id") != action_id or work_order.get("action_id") != action_id:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "accepted submission does not match the pending work order",
            recoverable=False,
        )
    verify_pending_trusted_static_inputs(project_root, session, work_order)
    existing = retained_result(project_root, session["orchestration_id"], action_id)
    if existing is not None and existing != result:
        raise OrchestrationControllerError(
            "RESULT_CONFLICT",
            f"action {action_id} already has a different retained result",
            recoverable=False,
        )
    if existing is None:
        write_json_atomic(work_result_path(project_root, session["orchestration_id"], action_id), result)

    expected_phase = pending.get("next_phase")
    completion_reason = pending.get("completion_reason")
    if result["outcome"] == "completed":
        verified_phase, verified_reason = verify_action_postconditions(
            project_root,
            session,
            work_order,
            apply_effects=False,
        )
        if verified_phase != expected_phase:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "accepted submission no longer verifies to its prepared next phase",
                recoverable=True,
            )
        finalized_phase, finalized_reason = verify_action_postconditions(
            project_root,
            session,
            work_order,
            apply_effects=True,
        )
        if finalized_phase != expected_phase:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "action finalization changed the verified next phase",
                recoverable=True,
            )
        completion_reason = finalized_reason or verified_reason or completion_reason
    elif result["outcome"] == "blocked":
        finish_active_child(project_root, session, "blocked_on_sources")
    else:
        finish_active_child(project_root, session, "failed")
        if not any(record.get("action_id") == action_id for record in session["failure_records"]):
            session["failure_records"].append(
                {"recorded_at": timestamp_utc(), "action_id": action_id, "summary": result["summary"]}
            )

    if session.get("last_completed_action_id") != action_id:
        session["completed_action_count"] = int(session["completed_action_count"]) + 1
    session["pending_action_id"] = None
    session["pending_submission"] = None
    if "pending_trusted_static_inputs" in session:
        session["pending_trusted_static_inputs"] = None
    session["last_completed_action_id"] = action_id
    session["recovery"] = default_recovery_state()
    session["updated_at"] = timestamp_utc()
    if expected_phase in TERMINAL_STATUSES:
        session["status"] = expected_phase
        session["phase"] = expected_phase
        session["verdict"] = expected_phase
        session["pause_reason"] = None if expected_phase == "complete" else str(completion_reason or result["summary"])
        session["completed_at"] = session["updated_at"]
    else:
        session["status"] = ACTIVE_STATUS
        session["phase"] = expected_phase or "planning"
        session["verdict"] = None
        session["pause_reason"] = None
    write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
    ensure_completion_events(project_root, session, result)
    return session


def repair_last_completion_events(project_root: Path, session: dict[str, Any]) -> None:
    action_id = session.get("last_completed_action_id")
    if not isinstance(action_id, str) or not action_id:
        return
    result = retained_result(project_root, session["orchestration_id"], action_id)
    if result is not None:
        ensure_completion_events(project_root, session, result)


def submit_result(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    orchestration_id = require_safe_id(args.orchestration_id, "orchestration_id")
    action_id = require_safe_id(args.action_id, "action_id")
    result = load_result(Path(args.result_file).expanduser().resolve(), action_id, project_root)
    with workspace_lock(session_lock_path(project_root, orchestration_id), purpose=f"orchestration {orchestration_id}"):
        session = load_session(project_root, orchestration_id)
        enforce_control_repair_gate(project_root, orchestration_id)
        if args.agent_id is not None and require_agent_id(args.agent_id) != session["agent_id"]:
            raise OrchestrationControllerError("ORCHESTRATION_OWNER_MISMATCH", "--agent-id does not own this session")
        retained = retained_result(project_root, orchestration_id, action_id)
        if retained is not None and retained != result:
            raise OrchestrationControllerError(
                "RESULT_CONFLICT",
                f"action {action_id} already has a different retained result",
                recoverable=False,
            )
        order = load_json_object(
            work_order_path(project_root, orchestration_id, action_id),
            error_code="WORK_ORDER_INVALID",
            label="work order",
        )
        verify_runtime_guards(project_root, session, order)
        pending_submission = session.get("pending_submission")
        if pending_submission is not None:
            if pending_submission.get("action_id") != action_id or pending_submission.get("result") != result:
                raise OrchestrationControllerError(
                    "RESULT_CONFLICT",
                    f"action {action_id} already has a different accepted submission",
                    recoverable=False,
                )
            return finalize_pending_submission(project_root, session, order)
        if retained is not None and session.get("pending_action_id") != action_id:
            if (
                session.get("last_completed_action_id") != action_id
                or int(session.get("completed_action_count", 0) or 0) < 1
            ):
                raise OrchestrationControllerError(
                    "ORCHESTRATION_STATE_INVALID",
                    f"retained result {action_id} is not proven completed by the parent session",
                    recoverable=False,
                )
            ensure_completion_events(project_root, session, retained)
            return session
        if session.get("pending_action_id") != action_id:
            raise OrchestrationControllerError(
                "ACTION_NOT_PENDING",
                f"action {action_id} is not the pending action",
                details={"pending_action_id": session.get("pending_action_id")},
            )
        prepare_submission(project_root, session, order, result)
        return finalize_pending_submission(project_root, session, order)


def select_session(project_root: Path, orchestration_id: str | None) -> dict[str, Any]:
    if orchestration_id is not None:
        return load_session(project_root, require_safe_id(orchestration_id, "orchestration_id"))
    root = orchestration_root(project_root)
    if not root.is_dir():
        raise OrchestrationControllerError("ORCHESTRATION_UNKNOWN", "no orchestration sessions exist")
    sessions: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / SESSION_FILENAME).is_file():
            try:
                sessions.append(load_session(project_root, child.name))
            except OrchestrationControllerError:
                continue
    if not sessions:
        raise OrchestrationControllerError("ORCHESTRATION_UNKNOWN", "no readable orchestration sessions exist")
    active = [item for item in sessions if item.get("status") not in TERMINAL_STATUSES]
    selected = active or sessions
    return sorted(
        selected,
        key=lambda item: (str(item.get("updated_at") or ""), item["orchestration_id"]),
        reverse=True,
    )[0]


def status_session(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    return select_session(project_root, args.orchestration_id)


def render_text(document: dict[str, Any]) -> str:
    if document.get("artifact_type") == WORK_ORDER_ARTIFACT_TYPE:
        return f"{document['orchestration_id']} {document['action_id']}: {document['phase']}\n"
    return (
        f"{document.get('orchestration_id')}: {document.get('status')} "
        f"({document.get('phase')}, actions={document.get('action_count', 0)})\n"
    )


def command_document(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "start":
        return start_session(project_root, args)
    if args.command == "next":
        return next_work(project_root, args)
    if args.command == "submit":
        return submit_result(project_root, args)
    if args.command == "status":
        return status_session(project_root, args)
    raise OrchestrationControllerError("VALUE_INVALID", f"unknown command: {args.command}")


def exit_code_for(document: dict[str, Any]) -> int:
    status = document.get("status")
    if status == "blocked_on_sources":
        return EXIT_BLOCKED
    if status == PAUSED_STATUS:
        return EXIT_PAUSED
    if status in {"no_ship", "failed"}:
        return EXIT_INVALID
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(raw_argv)
    json_mode = json_mode_requested(raw_argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        document = command_document(project_root, args)
    except OrchestrationControllerError as error:
        if json_mode:
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                recoverable=error.recoverable,
                remediation=error.remediation,
                details=error.details,
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return error.exit_code
    except LockUnavailableError as error:
        if json_mode:
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                remediation=error.remediation,
                details=error.details,
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    if args.format == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(document))
    return exit_code_for(document)


if __name__ == "__main__":
    raise SystemExit(main())
