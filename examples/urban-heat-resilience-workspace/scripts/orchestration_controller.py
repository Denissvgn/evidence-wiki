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
MAX_SCOPE_IDS = 256
MAX_SCOPE_ID_LENGTH = 200
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
MAX_SCOPE_GUARD_BYTES = 8 * 1024 * 1024
MAX_SCOPE_GUARD_ENTRIES = 10_000
MAX_WORK_ORDER_BYTES = 256 * 1024

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
REVIEWABLE_CANDIDATE_STATES = frozenset({"new", "proposed", "discovered", "reviewed", "deferred"})
DISCOVERY_APPEND_CANDIDATE_STATES = frozenset({"new", "proposed", "discovered"})
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
    if (
        not normalized
        or len(normalized) > 160
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
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


def scope_integrity_baseline_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / TRUSTED_INPUTS_DIR / f"{action_id}-scope-baseline.json"


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


def contained_path(
    path: Path,
    *,
    containment_root: Path,
    error_code: str,
    label: str,
    missing_ok: bool,
) -> Path | None:
    """Re-anchor a lexical child path beneath a canonical, link-free root."""
    lexical_root = Path(os.path.abspath(containment_root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise OrchestrationControllerError(error_code, f"{label} escapes the workspace") from exc

    root = lexical_root.resolve()
    anchored = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            ancestor = current.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise OrchestrationControllerError(error_code, f"{label} is missing: {anchored}") from None
        except OSError as exc:
            raise OrchestrationControllerError(
                error_code,
                f"could not inspect {label} ancestor: {current}",
            ) from exc
        if not stat.S_ISDIR(ancestor.st_mode) or path_is_link_like(current, ancestor):
            raise OrchestrationControllerError(
                error_code,
                f"{label} ancestor is not a real directory: {current}",
            )
    return anchored


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
        path = contained_path(
            path,
            containment_root=containment_root,
            error_code=error_code,
            label=label,
            missing_ok=missing_ok,
        )
        if path is None:
            return None
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
    if containment_root is not None:
        path = contained_path(
            path,
            containment_root=containment_root,
            error_code="ORCHESTRATION_POSTCONDITION_FAILED",
            label="verification artifact",
            missing_ok=True,
        )
        if path is None:
            return None
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"could not inspect verification artifact: {path}",
        ) from exc
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


def safe_snapshot_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/") or WINDOWS_ABSOLUTE_RE.match(value):
        return False
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == normalized


def raw_tree_snapshot(
    project_root: Path,
    config: dict[str, Any],
    *,
    include_entries: bool = False,
) -> dict[str, Any]:
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
    entries: dict[str, str] = {}
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
        entries[relative] = digest

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
    snapshot = {
        "algorithm": "sha256-content-v1",
        "file_count": len(records),
        "total_bytes": total_bytes,
        "fingerprint": f"sha256:{digest}",
    }
    if include_entries:
        snapshot["entries"] = dict(sorted(entries.items()))
    return snapshot


def evidence_manifest_digest(project_root: Path) -> str | None:
    return file_digest(
        project_root / "sources" / "manifest.jsonl",
        max_bytes=MAX_MANIFEST_SNAPSHOT_BYTES,
        containment_root=project_root,
    )


def valid_sha256_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def valid_raw_tree_snapshot(value: Any, *, include_entries: bool = False) -> bool:
    required = {"algorithm", "file_count", "total_bytes", "fingerprint"}
    if include_entries:
        required.add("entries")
    if not isinstance(value, dict) or set(value) != required:
        return False
    if (
        value.get("algorithm") != "sha256-content-v1"
        or isinstance(value.get("file_count"), bool)
        or not isinstance(value.get("file_count"), int)
        or not 0 <= value["file_count"] <= MAX_RAW_TREE_SNAPSHOT_ENTRIES
        or isinstance(value.get("total_bytes"), bool)
        or not isinstance(value.get("total_bytes"), int)
        or not 0 <= value["total_bytes"] <= MAX_RAW_TREE_SNAPSHOT_BYTES
        or not valid_sha256_fingerprint(value.get("fingerprint"))
    ):
        return False
    if not include_entries:
        return True
    entries = value.get("entries")
    return (
        isinstance(entries, dict)
        and len(entries) == value["file_count"]
        and len(entries) <= MAX_RAW_TREE_SNAPSHOT_ENTRIES
        and all(
            isinstance(path, str)
            and path.startswith("raw/")
            and len(path) <= MAX_ARTIFACT_PATH_LENGTH
            and safe_snapshot_relative_path(path)
            and valid_sha256_fingerprint(fingerprint)
            for path, fingerprint in entries.items()
        )
    )


def require_immutability_baselines(work_order: dict[str, Any]) -> None:
    """Fail closed when a pending action predates pre-action immutability guards."""
    phase = work_order.get("phase")
    if phase not in {"discovery", "candidate_review"}:
        return
    checks = {
        item.get("check"): item
        for item in work_order.get("required_postconditions", [])
        if isinstance(item, dict) and isinstance(item.get("check"), str)
    }
    manifest_check = "discovery_never_fetches" if phase == "discovery" else "selection_does_not_fetch"
    raw_before = checks.get("raw_tree_unchanged", {}).get("before")
    manifest_guard = checks.get(manifest_check, {})
    manifest_digest = manifest_guard.get("manifest_digest_before")
    if valid_raw_tree_snapshot(raw_before) and (
        manifest_digest is None or valid_sha256_fingerprint(manifest_digest)
    ) and "manifest_digest_before" in manifest_guard:
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_IMMUTABILITY_BASELINE_UNAVAILABLE",
        f"pending {phase} work predates the raw/manifest immutability baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Never bind raw or manifest digests after worker execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_research_question_baseline(work_order: dict[str, Any]) -> None:
    """Refuse legacy research replay when no trustworthy pre-action state exists."""
    if work_order.get("phase") != "research":
        return
    guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "workspace_readiness_changed"
        ),
        None,
    )
    baseline = guard.get("scoped_questions_before") if isinstance(guard, dict) else None
    question_files_before = (
        guard.get("question_file_fingerprints_before") if isinstance(guard, dict) else None
    )
    source_requests_before = (
        guard.get("source_request_record_fingerprints_before") if isinstance(guard, dict) else None
    )
    if (
        isinstance(baseline, dict)
        and baseline
        and valid_question_file_fingerprint_snapshot(question_files_before)
        and valid_record_fingerprint_snapshot(source_requests_before)
        and all(f"{slug}.md" in question_files_before for slug in baseline)
    ):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_RESEARCH_BASELINE_UNAVAILABLE",
        "pending research work predates the scoped-question baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not hand-edit the retained work order or infer a baseline after worker execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_discovery_candidate_baseline(work_order: dict[str, Any]) -> None:
    """Refuse discovery replay when newly created candidate IDs cannot be proven."""
    if work_order.get("phase") != "discovery":
        return
    guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "request_scoped_candidates_increased"
        ),
        None,
    )
    baseline = guard.get("candidate_states_before") if isinstance(guard, dict) else None
    record_baseline = guard.get("candidate_record_fingerprints_before") if isinstance(guard, dict) else None
    before = guard.get("before") if isinstance(guard, dict) else None
    if (
        valid_candidate_state_baseline(baseline)
        and valid_record_fingerprint_snapshot(record_baseline)
        and set(baseline) <= set(record_baseline)
        and isinstance(before, int)
        and not isinstance(before, bool)
        and before == len(baseline)
    ):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_DISCOVERY_BASELINE_UNAVAILABLE",
        "pending discovery work predates the bounded candidate-state baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not hand-edit the retained work order or infer candidate creation after execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_action_baselines(
    work_order: dict[str, Any],
    project_root: Path | None = None,
) -> dict[str, Any]:
    resolved = hydrate_integrity_baselines(project_root, work_order) if project_root is not None else work_order
    require_immutability_baselines(resolved)
    require_research_question_baseline(resolved)
    require_discovery_candidate_baseline(resolved)
    require_candidate_review_selection_baseline(resolved)
    require_acquisition_evidence_baselines(resolved)
    return resolved


def valid_candidate_state_baseline(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_IDS
        and all(
            isinstance(candidate_id, str)
            and bool(candidate_id)
            and len(candidate_id) <= MAX_SCOPE_ID_LENGTH
            and "\x00" not in candidate_id
            and isinstance(state, str)
            and len(state) <= 64
            and re.fullmatch(r"[a-z][a-z0-9_-]*", state) is not None
            for candidate_id, state in value.items()
        )
    )


def valid_scope_id_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= MAX_SCOPE_IDS
        and all(
            isinstance(item, str)
            and bool(item)
            and len(item) <= MAX_SCOPE_ID_LENGTH
            and "\x00" not in item
            for item in value
        )
        and len(value) == len(set(value))
    )


def valid_blocked_question_baseline(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) > MAX_SCOPE_IDS:
        return False
    for slug, snapshot in value.items():
        if (
            not isinstance(slug, str)
            or not slug
            or len(slug) > MAX_SCOPE_ID_LENGTH
            or "\x00" in slug
            or not isinstance(snapshot, dict)
            or set(snapshot) != {"status", "blocking_request_ids", "source_ids_before"}
            or snapshot.get("status") != "blocked"
            or not valid_scope_id_list(snapshot.get("blocking_request_ids"))
            or not snapshot.get("blocking_request_ids")
            or not valid_scope_id_list(snapshot.get("source_ids_before"))
        ):
            return False
    return True


def require_acquisition_evidence_baselines(work_order: dict[str, Any]) -> None:
    """Refuse acquisition replay when exact pre-action reconciliation cannot be proven."""
    if work_order.get("phase") != "acquisition":
        return
    question_guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "linked_blocked_questions_reopened"
        ),
        None,
    )
    manifest_guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "manifest_records_increased"
        ),
        None,
    )
    blocked_questions_before = (
        question_guard.get("blocked_questions_before") if isinstance(question_guard, dict) else None
    )
    matching_source_ids_before = (
        manifest_guard.get("matching_source_ids_before") if isinstance(manifest_guard, dict) else None
    )
    matching_source_records_before = (
        manifest_guard.get("matching_source_records_before") if isinstance(manifest_guard, dict) else None
    )
    manifest_records_before = (
        manifest_guard.get("manifest_record_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    raw_tree_before = manifest_guard.get("raw_tree_before") if isinstance(manifest_guard, dict) else None
    candidate_records_before = (
        manifest_guard.get("candidate_record_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    candidate_audit_records_before = (
        manifest_guard.get("candidate_audit_record_fingerprints_before")
        if isinstance(manifest_guard, dict)
        else None
    )
    source_requests_before = (
        manifest_guard.get("source_request_record_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    normalized_files_before = (
        manifest_guard.get("normalized_file_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    question_files_before = (
        manifest_guard.get("question_file_fingerprints_before") if isinstance(manifest_guard, dict) else None
    )
    if valid_blocked_question_baseline(blocked_questions_before) and valid_scope_id_list(
        matching_source_ids_before
    ) and valid_matching_source_record_snapshot(matching_source_records_before) and (
        set(matching_source_ids_before) == set(matching_source_records_before)
    ) and valid_record_fingerprint_snapshot(manifest_records_before) and (
        set(matching_source_ids_before) <= set(manifest_records_before)
    ) and valid_raw_tree_snapshot(raw_tree_before, include_entries=True) and valid_record_fingerprint_snapshot(
        candidate_records_before
    ) and valid_record_fingerprint_snapshot(candidate_audit_records_before) and valid_record_fingerprint_snapshot(
        source_requests_before
    ) and valid_file_fingerprint_snapshot(
        normalized_files_before,
        prefix="sources/",
    ) and valid_question_file_fingerprint_snapshot(question_files_before):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_ACQUISITION_BASELINE_UNAVAILABLE",
        "pending acquisition work predates the bounded question/evidence baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not infer question transitions or matching evidence after worker execution."
        ),
        details={"action_id": work_order.get("action_id")},
    )


def require_candidate_review_selection_baseline(work_order: dict[str, Any]) -> None:
    if work_order.get("phase") != "candidate_review":
        return
    guard = next(
        (
            item
            for item in work_order.get("required_postconditions", [])
            if isinstance(item, dict) and item.get("check") == "selected_candidate_for_request"
        ),
        None,
    )
    baseline = guard.get("selected_candidate_ids_before") if isinstance(guard, dict) else None
    record_baseline = guard.get("candidate_record_fingerprints_before") if isinstance(guard, dict) else None
    before = guard.get("selected_before") if isinstance(guard, dict) else None
    if (
        valid_scope_id_list(baseline)
        and valid_record_fingerprint_snapshot(record_baseline)
        and set(baseline) <= set(record_baseline)
        and isinstance(before, int)
        and not isinstance(before, bool)
        and before == len(baseline)
    ):
        return
    raise OrchestrationControllerError(
        "ORCHESTRATION_CANDIDATE_REVIEW_BASELINE_UNAVAILABLE",
        "pending candidate-review work predates the bounded selected-candidate baseline and cannot be replayed safely",
        recoverable=False,
        remediation=(
            "Preserve this orchestration for audit and start a fresh orchestration session from the current "
            "workspace state. Do not infer which candidate selections occurred after execution."
        ),
        details={"action_id": work_order.get("action_id")},
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


def canonical_json_fingerprint(value: Any, *, label: str) -> tuple[str, int]:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_INVALID",
            f"{label} cannot be canonically fingerprinted: {exc}",
            recoverable=False,
        ) from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}", len(encoded)


def record_fingerprint_snapshot(
    records: list[dict[str, Any]],
    *,
    id_field: str,
    label: str,
) -> dict[str, str]:
    """Capture all existing record identities without retaining record content."""
    snapshot: dict[str, str] = {}
    total_bytes = 0
    for record in records:
        record_id = record.get(id_field) if isinstance(record, dict) else None
        if (
            not isinstance(record_id, str)
            or not record_id
            or len(record_id) > MAX_SCOPE_ID_LENGTH
            or "\x00" in record_id
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_INVALID",
                f"{label} contains a record without a bounded {id_field}",
                recoverable=False,
            )
        if record_id in snapshot:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_INVALID",
                f"{label} contains duplicate id: {record_id}",
                recoverable=False,
            )
        if len(snapshot) >= MAX_SCOPE_GUARD_ENTRIES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_ENTRIES}-record integrity-guard limit",
                recoverable=False,
                remediation=f"Archive or split {label} before starting another managed action.",
            )
        fingerprint, encoded_bytes = canonical_json_fingerprint(record, label=label)
        total_bytes += encoded_bytes
        if total_bytes > MAX_SCOPE_GUARD_BYTES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_BYTES}-byte integrity-guard limit",
                recoverable=False,
                remediation=f"Archive or split {label} before starting another managed action.",
            )
        snapshot[record_id] = fingerprint
    return dict(sorted(snapshot.items()))


def valid_record_fingerprint_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_GUARD_ENTRIES
        and all(
            isinstance(record_id, str)
            and bool(record_id)
            and len(record_id) <= MAX_SCOPE_ID_LENGTH
            and "\x00" not in record_id
            and valid_sha256_fingerprint(fingerprint)
            for record_id, fingerprint in value.items()
        )
    )


def fingerprint_scope_violations(
    before: dict[str, str],
    after: dict[str, str],
    *,
    mutable_ids: set[str],
    allowed_new_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Describe bounded identity changes outside an explicitly mutable scope."""
    allowed_new = allowed_new_ids if allowed_new_ids is not None else set()
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before) - allowed_new)
    changed = sorted(
        record_id
        for record_id in set(before) & set(after)
        if record_id not in mutable_ids and before[record_id] != after[record_id]
    )
    return {
        "removed": removed[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        "added_outside_scope": added[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        "changed_outside_scope": changed[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
    }


def candidate_record_fingerprint_snapshot(candidates: list[dict[str, Any]]) -> dict[str, str]:
    return record_fingerprint_snapshot(candidates, id_field="candidate_id", label="candidate store")


def source_request_record_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    source_requests = load_sibling_module("source_requests")
    records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    return record_fingerprint_snapshot(records, id_field="request_id", label="source-request store")


def file_tree_fingerprint_snapshot(
    project_root: Path,
    root: Path,
    *,
    label: str,
) -> dict[str, str]:
    """Fingerprint a bounded regular-file tree without following links or junctions."""
    snapshot: dict[str, str] = {}
    total_bytes = 0

    def fail(message: str) -> OrchestrationControllerError:
        return OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            f"{label} {message}",
            recoverable=True,
            remediation=f"Replace links or special files and keep the {label} tree bounded.",
        )

    def visit(path: Path) -> None:
        nonlocal total_bytes
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise fail(f"could not be inspected: {path}: {exc}") from exc
        if path_is_link_like(path, metadata):
            raise fail(f"contains a symbolic link or junction: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise fail(f"could not be enumerated: {path}: {exc}") from exc
            for child in children:
                visit(child)
            return
        if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1) or 1) != 1:
            raise fail(f"contains a non-regular or multiply linked file: {path}")
        if len(snapshot) >= MAX_SCOPE_GUARD_ENTRIES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_ENTRIES}-file integrity-guard limit",
                recoverable=False,
            )
        size = int(metadata.st_size)
        total_bytes += size
        if total_bytes > MAX_SCOPE_GUARD_BYTES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"{label} exceeds the {MAX_SCOPE_GUARD_BYTES}-byte integrity-guard limit",
                recoverable=False,
            )
        relative = relative_workspace_path(project_root, path)
        fingerprint = file_digest(path, max_bytes=size, containment_root=project_root)
        if fingerprint is None:
            raise fail(f"changed while it was fingerprinted: {relative}")
        snapshot[relative] = fingerprint

    visit(root)
    return dict(sorted(snapshot.items()))


def normalized_file_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    normalize_sources = load_sibling_module("normalize_sources")
    _, normalized_relative = normalize_sources.source_paths(config)
    return file_tree_fingerprint_snapshot(
        project_root,
        project_root / normalized_relative,
        label="normalized evidence",
    )


def valid_file_fingerprint_snapshot(value: Any, *, prefix: str | None = None) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_GUARD_ENTRIES
        and all(
            isinstance(path, str)
            and len(path) <= MAX_ARTIFACT_PATH_LENGTH
            and safe_snapshot_relative_path(path)
            and (prefix is None or path.startswith(prefix))
            and valid_sha256_fingerprint(fingerprint)
            for path, fingerprint in value.items()
        )
    )


def question_file_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    """Capture every question file so research cannot mutate work outside its scope."""
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    snapshot: dict[str, str] = {}
    total_bytes = 0
    if not questions_dir.exists():
        return snapshot
    try:
        paths = sorted(questions_dir.glob("*.md"), key=lambda item: item.name)
    except OSError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_WORKSPACE_UNSAFE",
            f"could not enumerate question files: {exc}",
            recoverable=True,
        ) from exc
    for path in paths:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"could not inspect question file {path.name}: {exc}",
                recoverable=True,
            ) from exc
        if (
            path_is_link_like(path, metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or int(getattr(metadata, "st_nlink", 1) or 1) != 1
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"question integrity guard requires a singly linked regular file: {path.name}",
                recoverable=True,
            )
        if len(snapshot) >= MAX_SCOPE_GUARD_ENTRIES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                "question store exceeds the bounded integrity-guard entry limit",
                recoverable=False,
            )
        declared_size = int(metadata.st_size)
        total_bytes += declared_size
        if total_bytes > MAX_SCOPE_GUARD_BYTES:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                "question store exceeds the bounded integrity-guard byte limit",
                recoverable=False,
            )
        fingerprint = file_digest(
            path,
            max_bytes=declared_size,
            containment_root=project_root,
        )
        if fingerprint is None:
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"question file changed while it was fingerprinted: {path.name}",
                recoverable=True,
            )
        snapshot[path.name] = fingerprint
    return snapshot


def valid_question_file_fingerprint_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_GUARD_ENTRIES
        and all(
            isinstance(filename, str)
            and len(filename) <= MAX_ARTIFACT_PATH_LENGTH
            and PurePosixPath(filename).name == filename
            and filename.endswith(".md")
            and valid_sha256_fingerprint(fingerprint)
            for filename, fingerprint in value.items()
        )
    )


def candidate_request_id(candidate: dict[str, Any]) -> str | None:
    for field in ("source_request_id", "selected_for_request_id", "selected_request_id", "request_id"):
        value = candidate.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_state(candidate: dict[str, Any]) -> str:
    value = candidate.get("lifecycle_state") or candidate.get("status") or "new"
    return value.strip().lower() if isinstance(value, str) and value.strip() else "new"


def scoped_question_snapshot(
    project_root: Path,
    config: dict[str, Any],
    slugs: list[str],
) -> dict[str, dict[str, Any]]:
    """Capture the bounded lifecycle fields needed to prove research progress."""
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    records = {
        str(record.get("slug")): record
        for record in question_status.collect_questions(questions_dir)
        if isinstance(record, dict) and isinstance(record.get("slug"), str)
    }
    snapshot: dict[str, dict[str, Any]] = {}
    for slug in slugs:
        record = records.get(slug)
        if record is None:
            continue
        snapshot[slug] = {
            "status": str(record.get("status") or "unknown"),
            "blocking_request_ids": sorted(
                value
                for value in record.get("blocking_request_ids", [])
                if isinstance(value, str) and value
            ),
            "answer_page": str(record.get("answer_page") or ""),
        }
    return snapshot


def scoped_question_evidence_snapshot(
    project_root: Path,
    config: dict[str, Any],
    slugs: list[str],
) -> dict[str, dict[str, Any]]:
    """Capture lifecycle, blocking links, and source links for bounded question scope."""
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    snapshot: dict[str, dict[str, Any]] = {}
    for slug in slugs:
        path = questions_dir / f"{slug}.md"
        if not path.is_file():
            continue
        frontmatter = question_status.load_frontmatter(path)
        if not isinstance(frontmatter, dict) or frontmatter.get("type") != "question":
            continue
        snapshot[slug] = {
            "status": str(frontmatter.get("status") or "unknown"),
            "blocking_request_ids": sorted(
                {
                    value
                    for value in frontmatter.get("blocking_request_ids", [])
                    if isinstance(value, str) and value
                }
            )
            if isinstance(frontmatter.get("blocking_request_ids"), list)
            else [],
            "source_ids": sorted(
                {
                    value
                    for value in frontmatter.get("source_ids", [])
                    if isinstance(value, str) and value
                }
            )
            if isinstance(frontmatter.get("source_ids"), list)
            else [],
        }
    return snapshot


def linked_blocked_questions_snapshot(
    project_root: Path,
    config: dict[str, Any],
    slugs: list[str],
    request_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Capture only blocked scoped questions linked to the scoped source requests."""
    request_scope = set(request_ids)
    linked: dict[str, dict[str, Any]] = {}
    for slug, snapshot in scoped_question_evidence_snapshot(project_root, config, slugs).items():
        blocking_request_ids = [
            request_id
            for request_id in snapshot.get("blocking_request_ids", [])
            if request_id in request_scope
        ]
        if snapshot.get("status") != "blocked" or not blocking_request_ids:
            continue
        linked[slug] = {
            "status": "blocked",
            "blocking_request_ids": blocking_request_ids,
            "source_ids_before": list(snapshot.get("source_ids", [])),
        }
    if not valid_blocked_question_baseline(linked):
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "linked blocked-question baseline exceeds the bounded orchestration contract",
            recoverable=False,
            remediation="Split or repair the oversized question/request linkage before starting acquisition.",
        )
    return linked


def matching_normalized_source_ids(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
    candidate_ids: list[str],
) -> list[str]:
    """Return normalized evidence already correlated to the exact acquisition scope."""
    return sorted(
        matching_normalized_source_records(
            project_root,
            config,
            request_ids,
            candidate_ids,
        )
    )


def matching_normalized_source_records(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
    candidate_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Fingerprint exact pre-existing normalized evidence for scoped reconciliation."""
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids)
    normalize_sources = load_sibling_module("normalize_sources")
    manifest_relative, normalized_relative = normalize_sources.source_paths(config)
    records = normalize_sources.load_manifest(project_root / manifest_relative)
    normalized_root = project_root / normalized_relative
    matching: dict[str, dict[str, str]] = {}
    for record in records:
        provenance = record.get("provenance") if isinstance(record, dict) else None
        source_id = record.get("id") if isinstance(record, dict) else None
        normalized_path = (
            normalize_sources.normalized_output_path_for_record(record, normalized_root)
            if isinstance(record, dict)
            else None
        )
        if (
            not isinstance(provenance, dict)
            or provenance.get("request_id") not in request_scope
            or provenance.get("candidate_id") not in candidate_scope
            or not isinstance(source_id, str)
            or not source_id
            or source_id in matching
            or not isinstance(normalized_path, Path)
            or not normalized_path.is_file()
        ):
            continue
        record_fingerprint, _ = canonical_json_fingerprint(record, label="evidence manifest")
        normalized_fingerprint = file_digest(
            normalized_path,
            max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
            containment_root=project_root,
        )
        if normalized_fingerprint is None:
            raise OrchestrationControllerError(
                "ORCHESTRATION_WORKSPACE_UNSAFE",
                f"matching normalized evidence is unreadable or oversized: {source_id}",
                recoverable=True,
                remediation="Repair the normalized evidence record before starting acquisition.",
            )
        matching[source_id] = {
            "record_fingerprint": record_fingerprint,
            "normalized_fingerprint": normalized_fingerprint,
        }
        if len(matching) > MAX_SCOPE_IDS:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                "matching normalized-source baseline exceeds the bounded orchestration contract",
                recoverable=False,
                remediation="Split or repair duplicate evidence provenance before starting acquisition.",
            )
    if not valid_scope_id_list(sorted(matching)):
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_INVALID",
            "matching normalized-source baseline contains an invalid source id",
            recoverable=False,
            remediation="Repair invalid manifest source ids before starting acquisition.",
        )
    return dict(sorted(matching.items()))


def valid_matching_source_record_snapshot(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and len(value) <= MAX_SCOPE_IDS
        and valid_scope_id_list(sorted(value))
        and all(
            isinstance(snapshot, dict)
            and set(snapshot) == {"record_fingerprint", "normalized_fingerprint"}
            and valid_sha256_fingerprint(snapshot.get("record_fingerprint"))
            and valid_sha256_fingerprint(snapshot.get("normalized_fingerprint"))
            for snapshot in value.values()
        )
    )


def manifest_record_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    normalize_sources = load_sibling_module("normalize_sources")
    manifest_relative, _ = normalize_sources.source_paths(config)
    records = normalize_sources.load_manifest(project_root / manifest_relative)
    return record_fingerprint_snapshot(records, id_field="id", label="evidence manifest")


def candidate_provider(candidate: dict[str, Any]) -> str | None:
    value = candidate.get("provider")
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    paper = candidate.get("paper") if isinstance(candidate.get("paper"), dict) else {}
    value = paper.get("provider")
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def acquisition_route(candidate: dict[str, Any], enabled: set[str]) -> str | None:
    """Return the executable provider chosen by the canonical acquisition planner."""
    if not enabled:
        return None
    source_requests = load_sibling_module("source_requests")
    route = source_requests.candidate_acquisition_route(
        candidate,
        {"enabled": True, "providers": sorted(enabled)},
        candidate_request_id(candidate) or "route-check",
    )
    provider = route.get("provider") if isinstance(route, dict) else None
    if (
        route.get("provider_backed") is not True
        or route.get("allowed_by_config") is not True
        or not isinstance(provider, str)
        or provider not in enabled
    ):
        return None
    return provider


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


def bounded_scope_ids(
    values: list[Any],
    label: str,
    *,
    truncate: bool = False,
) -> list[str]:
    """Normalize an order scope without ever persisting a host-invalid ID array."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_SCOPE_ID_LENGTH
            or "\x00" in value
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_INVALID",
                f"{label} contains an invalid scoped id",
                recoverable=False,
                remediation=f"Repair malformed {label} workspace records before resuming orchestration.",
            )
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
        if truncate and len(normalized) == MAX_SCOPE_IDS:
            break
    if len(normalized) > MAX_SCOPE_IDS:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            f"{label} exceeds the {MAX_SCOPE_IDS}-id work-order limit",
            recoverable=False,
            remediation=f"Split or reduce {label} before resuming orchestration.",
        )
    return normalized


def selected_candidate_id_snapshot(
    candidates: list[dict[str, Any]],
    request_ids: list[str],
) -> list[str]:
    """Capture bounded historical selections without snapshotting unrelated candidates."""
    request_scope = set(request_ids)
    selected = [
        candidate.get("candidate_id")
        for candidate in candidates
        if candidate_request_id(candidate) in request_scope and candidate_state(candidate) == "selected"
    ]
    return sorted(bounded_scope_ids(selected, "selected candidate baseline"))


def request_candidate_state_snapshot(
    candidates: list[dict[str, Any]],
    request_ids: list[str],
) -> dict[str, str]:
    """Capture a bounded identity/state baseline for request-scoped candidates."""
    request_scope = set(request_ids)
    snapshot: dict[str, str] = {}
    for candidate in candidates:
        if candidate_request_id(candidate) not in request_scope:
            continue
        candidate_id = candidate.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or len(candidate_id) > MAX_SCOPE_ID_LENGTH
            or "\x00" in candidate_id
        ):
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                "request-scoped discovery candidate lacks a bounded candidate_id",
                recoverable=False,
            )
        if candidate_id in snapshot:
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                f"request-scoped discovery candidate id is duplicated: {candidate_id}",
                recoverable=False,
            )
        if len(snapshot) >= MAX_SCOPE_IDS:
            raise OrchestrationControllerError(
                "ORCHESTRATION_SCOPE_EXCEEDED",
                f"request-scoped candidate baseline exceeds {MAX_SCOPE_IDS} records",
                recoverable=False,
                remediation="Resolve, supersede, or split the source request before starting another discovery action.",
            )
        state = candidate_state(candidate)
        if len(state) > 64 or not re.fullmatch(r"[a-z][a-z0-9_-]*", state):
            raise OrchestrationControllerError(
                "CANDIDATE_STORE_INVALID",
                f"request-scoped discovery candidate {candidate_id} has an invalid lifecycle state",
                recoverable=False,
            )
        snapshot[candidate_id] = state
    return dict(sorted(snapshot.items()))


def standards_discovery_route(candidate: dict[str, Any]) -> str | None:
    standards = candidate.get("standards") if isinstance(candidate.get("standards"), dict) else None
    if standards is None:
        return None
    registry = standards.get("registry_provider")
    if not isinstance(registry, str) or not registry.strip():
        return None
    registry = registry.strip().lower()
    if registry == "iso-open-data":
        return "iso-open-data"
    if registry in {"your-europe", "eu-harmonised-standards", "eur-lex"}:
        return "eu-product-requirements"
    if registry == "uk-geospatial-register":
        return "uk-geospatial-register"
    if registry in {"nist", "nist-standards-info", "nist-csrc"}:
        return "nist"
    return None


def candidate_uses_permitted_discovery_provider(
    candidate: dict[str, Any],
    permitted: set[str],
) -> bool:
    standards_route = standards_discovery_route(candidate)
    if standards_route is not None:
        return "standards" in permitted or f"standards:{standards_route}" in permitted
    recorded = candidate.get("discovery_providers")
    if isinstance(recorded, list):
        providers = {
            value.strip().lower()
            for value in recorded
            if isinstance(value, str) and value.strip()
        }
    else:
        provider = candidate_provider(candidate)
        providers = {provider} if provider is not None else set()
    return bool(providers) and providers <= permitted


def eligible_new_discovery_candidates(
    candidates: list[dict[str, Any]],
    request_ids: list[str],
    candidate_states_before: dict[str, str],
    discovery_providers: set[str],
    acquisition_providers: set[str],
) -> list[dict[str, Any]]:
    request_scope = set(request_ids)
    return [
        candidate
        for candidate in candidates
        if candidate_request_id(candidate) in request_scope
        and isinstance(candidate.get("candidate_id"), str)
        and candidate.get("candidate_id") not in candidate_states_before
        and candidate_state(candidate) in DISCOVERY_APPEND_CANDIDATE_STATES
        and candidate_uses_permitted_discovery_provider(candidate, discovery_providers)
        and acquisition_route(candidate, acquisition_providers) is not None
    ]


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


PHASE_BUDGET_STOP_REASONS = {
    "research": {"questions_exhausted", "source_requests_exhausted"},
    "discovery": {"discovery_results_exhausted", "academic_provider_requests_exhausted"},
    "acquisition": {
        "acquisition_downloads_exhausted",
        "github_archive_bytes_exhausted",
        "academic_provider_requests_exhausted",
        "web_downloads_exhausted",
        "manual_url_deliveries_exhausted",
    },
}


def rollover_exhausted_child_for_phase(
    project_root: Path,
    status: dict[str, Any],
    session: dict[str, Any],
    phase: str,
) -> bool:
    """Close an exhausted immutable child before issuing same-phase work in a fresh run."""
    relevant = PHASE_BUDGET_STOP_REASONS.get(phase, set())
    active_run_id = session.get("active_run_id")
    if not relevant or not isinstance(active_run_id, str) or not active_run_id:
        return False
    run_controller_status = (
        status.get("run_controller") if isinstance(status.get("run_controller"), dict) else {}
    )
    if run_controller_status.get("run_id") != active_run_id or run_controller_status.get("terminal") is True:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "workspace status did not bind phase budgets to the parent session's active child run",
            recoverable=True,
            remediation="Repair conflicting run-controller state before resuming the orchestration.",
            details={
                "phase": phase,
                "active_run_id": active_run_id,
                "status_run_id": run_controller_status.get("run_id"),
                "status_run_terminal": run_controller_status.get("terminal"),
            },
        )
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    budget = readiness.get("budget_state") if isinstance(readiness.get("budget_state"), dict) else {}
    stop_reasons = {
        value for value in budget.get("stop_reasons", []) if isinstance(value, str)
    }
    exhausted = sorted(stop_reasons & relevant)
    if not exhausted:
        return False
    document = active_child(project_root, session)
    if document is None:
        return False
    allowed = set(document.get("state", {}).get("allowed_next_states") or [])
    verdict = "no_ship" if "no_ship" in allowed else "blocked_on_sources" if "blocked_on_sources" in allowed else None
    if verdict is None:
        reason = (
            f"active child {active_run_id} exhausted {', '.join(exhausted)} but has no safe terminal transition"
        )
        session["status"] = PAUSED_STATUS
        session["phase"] = "paused"
        session["verdict"] = "paused"
        session["pause_reason"] = reason
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
        record_event(project_root, session, "session_paused", reason)
        return True
    finish_active_child(project_root, session, verdict)
    record_event(
        project_root,
        session,
        "child_run_budget_exhausted",
        "Closed a bounded child run after an artifact-derived phase budget was exhausted; subsequent work uses a fresh child run.",
        data={"run_id": active_run_id, "phase": phase, "stop_reasons": exhausted},
    )
    return True


def research_question_scope_limit(
    project_root: Path,
    status: dict[str, Any],
    session: dict[str, Any],
) -> int:
    """Return the current child run's remaining question budget, rolling over at zero."""
    run = status.get("run") if isinstance(status.get("run"), dict) else {}
    configured = run.get("max_questions_per_run", 25)
    if isinstance(configured, bool) or not isinstance(configured, int) or configured < 1:
        configured = 25
    configured = min(configured, MAX_SCOPE_IDS)

    active_run_id = session.get("active_run_id")
    if not isinstance(active_run_id, str) or not active_run_id:
        return configured
    run_controller = status.get("run_controller") if isinstance(status.get("run_controller"), dict) else {}
    if run_controller.get("run_id") != active_run_id or run_controller.get("terminal") is True:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "workspace status did not bind the research budget to the parent session's active child run",
            recoverable=True,
            remediation="Re-run orchestration status, repair conflicting active child runs, and resume the session.",
            details={
                "active_run_id": active_run_id,
                "status_run_id": run_controller.get("run_id"),
                "status_run_terminal": run_controller.get("terminal"),
            },
        )
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    budget = readiness.get("budget_state") if isinstance(readiness.get("budget_state"), dict) else {}
    remaining = budget.get("questions_remaining_this_run")
    if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0:
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "active child run is missing a valid artifact-derived remaining-question budget",
            recoverable=True,
            remediation="Repair the run-controller budget state before resuming this orchestration session.",
            details={"active_run_id": active_run_id, "questions_remaining_this_run": remaining},
        )
    if remaining > 0:
        return min(configured, remaining)

    if run_controller.get("state") != "answering":
        raise OrchestrationControllerError(
            "ORCHESTRATION_STATE_INVALID",
            "only an answering child run can roll over an exhausted question budget",
            recoverable=True,
            remediation="Repair the active child run state before resuming this orchestration session.",
            details={"active_run_id": active_run_id, "state": run_controller.get("state")},
        )

    finish_active_child(project_root, session, "no_ship")
    record_event(
        project_root,
        session,
        "child_run_budget_exhausted",
        "Closed a bounded child run after its question budget was exhausted; the next action uses a fresh child run.",
        data={"run_id": active_run_id, "budget": "max_questions_per_run"},
    )
    return configured


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
        if rollover_exhausted_child_for_phase(project_root, status, session, "research"):
            if session.get("status") == PAUSED_STATUS:
                return None, {"paused": True, "workspace_status": status}
        scope_limit = research_question_scope_limit(project_root, status, session)
        return "research", {
            "status": status,
            "scope": {
                "question_slugs": [str(value) for value in slugs[:scope_limit]],
                "request_ids": [],
                "candidate_ids": [],
            },
        }

    if verdict == "blocked_on_sources":
        requests = open_requests(project_root, config)
        candidates = load_candidates(project_root, config)
        acquisition = policy["acquisition"]
        acquisition_providers = set(acquisition["providers"]) if acquisition["enabled"] else set()
        discovery_providers = composable_discovery_providers(policy)
        route_failures: list[dict[str, Any]] = []
        for request in requests:
            raw_request_id = request.get("request_id")
            if not isinstance(raw_request_id, str) or not raw_request_id:
                continue
            request_id = bounded_scope_ids([raw_request_id], "source request scope")[0]
            question_slugs = bounded_scope_ids(
                [value for value in request.get("question_slugs", []) if isinstance(value, str)],
                f"question scope for source request {request_id}",
            )
            scoped = request_candidates(candidates, request_id)
            routable = [
                candidate
                for candidate in scoped
                if candidate_state(candidate) == "selected" and acquisition_route(candidate, acquisition_providers)
            ]
            if routable:
                # Candidate stores retain provider ranking. Acquire one selected
                # candidate at a time so retries never authorize duplicate
                # downloads for every historical selection on a request.
                candidate_ids = bounded_scope_ids(
                    [routable[0].get("candidate_id")],
                    f"acquisition candidate scope for source request {request_id}",
                )
                if rollover_exhausted_child_for_phase(project_root, status, session, "acquisition"):
                    if session.get("status") == PAUSED_STATUS:
                        return None, {"paused": True, "workspace_status": status}
                return "acquisition", {
                    "status": status,
                    "scope": {
                        "question_slugs": question_slugs,
                        "request_ids": [request_id],
                        "candidate_ids": candidate_ids,
                    },
                    "request": request,
                }
            reviewable = [
                candidate
                for candidate in scoped
                if candidate_state(candidate) in REVIEWABLE_CANDIDATE_STATES
                and acquisition_route(candidate, acquisition_providers) is not None
            ]
            if reviewable:
                candidate_ids = bounded_scope_ids(
                    [item.get("candidate_id") for item in reviewable],
                    f"candidate-review scope for source request {request_id}",
                    truncate=True,
                )
                return "candidate_review", {
                    "status": status,
                    "scope": {
                        "question_slugs": question_slugs,
                        "request_ids": [request_id],
                        "candidate_ids": candidate_ids,
                    },
                    "request": request,
                }
            if discovery_providers:
                if len(scoped) >= MAX_SCOPE_IDS:
                    route_failures.append(
                        {
                            "request_id": request_id,
                            "reason": (
                                "request candidate history exhausted the bounded discovery baseline; review, "
                                "supersede, or split the request before further discovery"
                            ),
                            "candidate_count": len(scoped),
                        }
                    )
                    continue
                if rollover_exhausted_child_for_phase(project_root, status, session, "discovery"):
                    if session.get("status") == PAUSED_STATUS:
                        return None, {"paused": True, "workspace_status": status}
                return "discovery", {
                    "status": status,
                    "scope": {
                        "question_slugs": question_slugs,
                        "request_ids": [request_id],
                        "candidate_ids": [],
                    },
                    "request": request,
                    "candidate_count_before": len(scoped),
                    "discovery_providers": discovery_providers,
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
    budgets = work_order_budgets(status, session)
    run_id: str | None
    skill: str
    inputs = ["research.yml", "AGENTS.md"]
    postconditions: list[dict[str, Any]]
    if route == "research":
        scoped_questions_before = scoped_question_snapshot(
            project_root,
            config,
            [value for value in scope.get("question_slugs", []) if isinstance(value, str)],
        )
        run_id = advance_child(project_root, session, "answering")
        skill = "research-run"
        inputs.extend(["wiki/questions", "sources/normalized", f"runs/{run_id}/run-state.json"])
        postconditions = [
            {
                "check": "workspace_readiness_changed",
                "allowed_verdicts": ["in_progress", "blocked_on_sources", "complete"],
                "scoped_questions_before": scoped_questions_before,
                "question_file_fingerprints_before": question_file_fingerprint_snapshot(
                    project_root,
                    config,
                ),
                "source_request_record_fingerprints_before": source_request_record_fingerprint_snapshot(
                    project_root,
                    config,
                ),
            },
            {"check": "child_run_state", "expected": "answering"},
        ]
    elif route == "discovery":
        permitted = list(context.get("discovery_providers") or [])
        candidates_before = load_candidates(project_root, config)
        candidate_states_before = request_candidate_state_snapshot(
            candidates_before,
            [value for value in scope.get("request_ids", []) if isinstance(value, str)],
        )
        run_id = advance_child(project_root, session, "discovering")
        skill = "research-discover"
        inputs.extend(["sources/source-requests.jsonl", candidates_input])
        effective_policy = {
            "discovery": {"enabled": bool(permitted), "providers": permitted},
            "acquisition": dict(session["provider_policy"]["acquisition"]),
        }
        postconditions = [
            {
                "check": "request_scoped_candidates_increased",
                "before": len(candidate_states_before),
                "candidate_states_before": candidate_states_before,
                "candidate_record_fingerprints_before": candidate_record_fingerprint_snapshot(
                    candidates_before
                ),
            },
            {
                "check": "discovery_never_fetches",
                "manifest_records_before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "manifest_digest_before": evidence_manifest_digest(project_root),
            },
            {"check": "raw_tree_unchanged", "before": raw_tree_snapshot(project_root, config)},
        ]
        remaining_candidate_capacity = MAX_SCOPE_IDS - len(candidate_states_before)
        budgets["max_discovery_results_per_run"] = min(
            int(budgets.get("max_discovery_results_per_run", remaining_candidate_capacity) or 0),
            remaining_candidate_capacity,
        )
    elif route == "candidate_review":
        candidates_before = load_candidates(project_root, config)
        selected_candidate_ids_before = selected_candidate_id_snapshot(
            candidates_before,
            [value for value in scope.get("request_ids", []) if isinstance(value, str)],
        )
        run_id = advance_child(project_root, session, "candidates_ready")
        skill = "research-discover"
        inputs.extend(["sources/source-requests.jsonl", candidates_input])
        postconditions = [
            {
                "check": "selected_candidate_for_request",
                "selected_before": len(selected_candidate_ids_before),
                "selected_candidate_ids_before": selected_candidate_ids_before,
                "candidate_record_fingerprints_before": candidate_record_fingerprint_snapshot(
                    candidates_before
                ),
            },
            {
                "check": "selection_does_not_fetch",
                "manifest_records_before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "manifest_digest_before": evidence_manifest_digest(project_root),
            },
            {"check": "raw_tree_unchanged", "before": raw_tree_snapshot(project_root, config)},
        ]
    elif route == "acquisition":
        scoped_question_slugs = [
            value for value in scope.get("question_slugs", []) if isinstance(value, str)
        ]
        scoped_request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
        scoped_candidate_ids = [value for value in scope.get("candidate_ids", []) if isinstance(value, str)]
        blocked_questions_before = linked_blocked_questions_snapshot(
            project_root,
            config,
            scoped_question_slugs,
            scoped_request_ids,
        )
        matching_source_records_before = matching_normalized_source_records(
            project_root,
            config,
            scoped_request_ids,
            scoped_candidate_ids,
        )
        matching_source_ids_before = sorted(matching_source_records_before)
        manifest_records_before = manifest_record_fingerprint_snapshot(project_root, config)
        raw_tree_before = raw_tree_snapshot(project_root, config, include_entries=True)
        candidate_records_before = candidate_record_fingerprint_snapshot(load_candidates(project_root, config))
        run_id = advance_child(project_root, session, "fetching")
        skill = "research-acquire"
        inputs.extend(["sources/source-requests.jsonl", candidates_input, "sources/manifest.jsonl"])
        postconditions = [
            {"check": "request_fulfilled_with_normalized_source"},
            {
                "check": "linked_blocked_questions_reopened",
                "blocked_questions_before": blocked_questions_before,
            },
            {
                "check": "manifest_records_increased",
                "before": int(status.get("sources", {}).get("manifest_records", 0) or 0),
                "matching_source_ids_before": matching_source_ids_before,
                "matching_source_records_before": matching_source_records_before,
                "manifest_record_fingerprints_before": manifest_records_before,
                "raw_tree_before": raw_tree_before,
                "candidate_record_fingerprints_before": candidate_records_before,
                "candidate_audit_record_fingerprints_before": (
                    candidate_audit_record_fingerprint_snapshot(project_root, config)
                ),
                "source_request_record_fingerprints_before": source_request_record_fingerprint_snapshot(
                    project_root,
                    config,
                ),
                "normalized_file_fingerprints_before": normalized_file_fingerprint_snapshot(
                    project_root,
                    config,
                ),
                "question_file_fingerprints_before": question_file_fingerprint_snapshot(
                    project_root,
                    config,
                ),
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
        "budgets": budgets,
        "inputs": sorted(set(inputs)),
        "required_postconditions": postconditions,
    }


INTEGRITY_BASELINE_FIELDS = frozenset(
    {
        "scoped_questions_before",
        "question_file_fingerprints_before",
        "source_request_record_fingerprints_before",
        "candidate_states_before",
        "candidate_record_fingerprints_before",
        "candidate_audit_record_fingerprints_before",
        "selected_candidate_ids_before",
        "blocked_questions_before",
        "matching_source_ids_before",
        "matching_source_records_before",
        "manifest_record_fingerprints_before",
        "raw_tree_before",
        "normalized_file_fingerprints_before",
    }
)


def baseline_value_count(value: Any) -> int:
    if isinstance(value, (dict, list)):
        return len(value)
    return 1


def externalize_integrity_baselines(
    project_root: Path,
    work_order: dict[str, Any],
) -> None:
    """Move large pre-action guards into one protected controller-owned artifact."""
    extracted: list[dict[str, Any]] = []
    field_count = 0
    entry_count = 0
    for postcondition in work_order.get("required_postconditions", []):
        if not isinstance(postcondition, dict):
            continue
        fields = {
            field: postcondition.pop(field)
            for field in sorted(INTEGRITY_BASELINE_FIELDS & set(postcondition))
        }
        if not fields:
            continue
        field_count += len(fields)
        entry_count += sum(baseline_value_count(value) for value in fields.values())
        extracted.append({"check": postcondition.get("check"), "fields": fields})
    if not extracted:
        return
    orchestration_id = require_safe_id(work_order.get("orchestration_id"), "orchestration_id")
    action_id = require_safe_id(work_order.get("action_id"), "action_id")
    document = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "orchestration_integrity_baseline",
        "orchestration_id": orchestration_id,
        "action_id": action_id,
        "phase": work_order.get("phase"),
        "postconditions": extracted,
    }
    encoded_baseline = (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")
    if len(encoded_baseline) > MAX_SCOPE_GUARD_BYTES:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "scope-integrity baseline exceeds the protected 8 MiB artifact limit",
            recoverable=False,
            remediation="Archive historical workspace records or reduce the bounded action scope before retrying.",
            details={"encoded_bytes": len(encoded_baseline), "max_bytes": MAX_SCOPE_GUARD_BYTES},
        )
    baseline_path = scope_integrity_baseline_path(project_root, orchestration_id, action_id)
    write_json_atomic(baseline_path, document)
    fingerprint = file_digest(
        baseline_path,
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        containment_root=project_root,
    )
    if fingerprint is None:
        raise OrchestrationControllerError(
            "ORCHESTRATION_WRITE_FAILED",
            "could not fingerprint the persisted scope-integrity baseline",
            recoverable=True,
        )
    work_order["required_postconditions"].append(
        {
            "check": "controller_integrity_baseline",
            "path": relative_workspace_path(project_root, baseline_path),
            "fingerprint": fingerprint,
            "field_count": field_count,
            "entry_count": entry_count,
        }
    )


def hydrate_integrity_baselines(
    project_root: Path,
    work_order: dict[str, Any],
) -> dict[str, Any]:
    """Validate and merge one protected baseline artifact into an in-memory work order."""
    phase = work_order.get("phase")
    if phase == "verification":
        return work_order
    guards = [
        item
        for item in work_order.get("required_postconditions", [])
        if isinstance(item, dict) and item.get("check") == "controller_integrity_baseline"
    ]
    if len(guards) != 1:
        return work_order
    guard = guards[0]
    orchestration_id = require_safe_id(work_order.get("orchestration_id"), "orchestration_id")
    action_id = require_safe_id(work_order.get("action_id"), "action_id")
    expected_path = scope_integrity_baseline_path(project_root, orchestration_id, action_id)
    expected_relative = relative_workspace_path(project_root, expected_path)
    if (
        guard.get("path") != expected_relative
        or not valid_sha256_fingerprint(guard.get("fingerprint"))
        or isinstance(guard.get("field_count"), bool)
        or not isinstance(guard.get("field_count"), int)
        or not 1 <= guard["field_count"] <= len(INTEGRITY_BASELINE_FIELDS)
        or isinstance(guard.get("entry_count"), bool)
        or not isinstance(guard.get("entry_count"), int)
        or not 0 <= guard["entry_count"] <= MAX_SCOPE_GUARD_ENTRIES * len(INTEGRITY_BASELINE_FIELDS)
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
            "work order contains an invalid controller integrity-baseline reference",
            recoverable=False,
            remediation="Preserve the orchestration for audit and start a fresh session.",
        )
    actual_fingerprint = file_digest(
        expected_path,
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        containment_root=project_root,
    )
    if actual_fingerprint != guard["fingerprint"]:
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_CHANGED",
            "controller-owned scope-integrity baseline is missing or changed",
            recoverable=False,
            remediation="Restore the protected baseline exactly or preserve this session and start a fresh one.",
        )
    document = load_json_object(
        expected_path,
        error_code="ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
        label="scope-integrity baseline",
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        containment_root=project_root,
    )
    if (
        set(document)
        != {"schema_version", "artifact_type", "orchestration_id", "action_id", "phase", "postconditions"}
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("artifact_type") != "orchestration_integrity_baseline"
        or document.get("orchestration_id") != orchestration_id
        or document.get("action_id") != action_id
        or document.get("phase") != phase
        or not isinstance(document.get("postconditions"), list)
    ):
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
            "controller-owned scope-integrity baseline identity or shape is invalid",
            recoverable=False,
        )
    hydrated = json.loads(json.dumps(work_order))
    hydrated["required_postconditions"] = [
        item
        for item in hydrated["required_postconditions"]
        if item.get("check") != "controller_integrity_baseline"
    ]
    by_check = {
        item.get("check"): item
        for item in hydrated["required_postconditions"]
        if isinstance(item, dict) and isinstance(item.get("check"), str)
    }
    observed_fields = 0
    observed_entries = 0
    seen_checks: set[str] = set()
    for item in document["postconditions"]:
        if not isinstance(item, dict) or set(item) != {"check", "fields"}:
            raise OrchestrationControllerError(
                "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
                "scope-integrity baseline contains an invalid postcondition entry",
                recoverable=False,
            )
        check = item.get("check")
        fields = item.get("fields")
        target = by_check.get(check) if isinstance(check, str) else None
        if (
            not isinstance(target, dict)
            or check in seen_checks
            or not isinstance(fields, dict)
            or not fields
            or not set(fields) <= INTEGRITY_BASELINE_FIELDS
            or set(fields) & set(target)
        ):
            raise OrchestrationControllerError(
                "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
                "scope-integrity baseline does not match retained work-order postconditions",
                recoverable=False,
            )
        seen_checks.add(check)
        target.update(fields)
        observed_fields += len(fields)
        observed_entries += sum(baseline_value_count(value) for value in fields.values())
    if observed_fields != guard["field_count"] or observed_entries != guard["entry_count"]:
        raise OrchestrationControllerError(
            "ORCHESTRATION_INTEGRITY_BASELINE_INVALID",
            "scope-integrity baseline summary does not match its protected content",
            recoverable=False,
        )
    return hydrated


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
    externalize_integrity_baselines(project_root, work_order)
    encoded_order = (json.dumps(work_order, indent=2, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded_order) > MAX_WORK_ORDER_BYTES:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "work order exceeds the managed-run 256 KiB size limit after baseline externalization",
            recoverable=False,
            remediation="Reduce the bounded action scope before resuming orchestration.",
            details={"encoded_bytes": len(encoded_order), "max_bytes": MAX_WORK_ORDER_BYTES},
        )
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
            require_action_baselines(order, project_root)
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
            require_action_baselines(order, project_root)
            legacy_unbound = "pending_trusted_static_inputs" not in session
            if legacy_unbound:
                # Only parse the declarative YAML authorization before the
                # migration snapshot exists. Bind all trusted workspace code
                # before fresh_workspace_status imports or executes it.
                verify_provider_policy_unchanged(project_root, session, order)
                bind_legacy_pending_trusted_inputs(project_root, session, order)
            verify_runtime_guards(project_root, session, order)
            return replay_work_order(project_root, session, resume=args.resume, retained_order=order)
        if pause_if_limited(project_root, session):
            return session
        route, context = choose_route(project_root, session)
        if route is None:
            if session.get("status") == PAUSED_STATUS:
                return session
            return finish_session(project_root, session, context["terminal_status"], context["reason"])
        spec = action_spec(project_root, session, route, context)
        return issue_work_order(project_root, session, spec)


def selected_candidates_for_scope(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
    candidate_ids: list[str],
) -> list[dict[str, Any]]:
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids)
    return [
        candidate
        for candidate in load_candidates(project_root, config)
        if candidate_request_id(candidate) in request_scope
        and candidate.get("candidate_id") in candidate_scope
        and candidate_state(candidate) == "selected"
    ]


def selected_candidates_outside_scope(
    project_root: Path,
    config: dict[str, Any],
    request_ids: list[str],
    candidate_ids: list[str],
    selected_candidate_ids_before: list[str] | None = None,
) -> list[dict[str, Any]]:
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids)
    historical_selected = set(selected_candidate_ids_before or [])
    return [
        candidate
        for candidate in load_candidates(project_root, config)
        if candidate_request_id(candidate) in request_scope
        and candidate.get("candidate_id") not in candidate_scope
        and candidate.get("candidate_id") not in historical_selected
        and candidate_state(candidate) == "selected"
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


def normalized_source_quality_failure(
    project_root: Path,
    path: Path,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return why a normalized source is unusable for acquisition fulfillment."""
    payload = bounded_regular_bytes(
        path,
        max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
        error_code="ORCHESTRATION_POSTCONDITION_FAILED",
        label="normalized evidence",
        containment_root=project_root,
    )
    if payload is None:  # pragma: no cover - missing_ok is false
        return {"reason": "normalized evidence is missing"}
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"reason": "normalized evidence is not valid UTF-8", "error": str(exc)}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {"reason": "normalized evidence lacks YAML frontmatter"}
    closing_index = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    if closing_index is None:
        return {"reason": "normalized evidence has unterminated YAML frontmatter"}
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    except yaml.YAMLError as exc:
        return {"reason": "normalized evidence has invalid YAML frontmatter", "error": str(exc)}
    source_id = record.get("id")
    if (
        not isinstance(frontmatter, dict)
        or frontmatter.get("type") != "normalized_source"
        or frontmatter.get("source_id") != source_id
    ):
        return {
            "reason": "normalized evidence frontmatter does not identify the manifest source",
            "expected_source_id": source_id,
            "actual_source_id": frontmatter.get("source_id")
            if isinstance(frontmatter, dict)
            else None,
        }
    status = frontmatter.get("status")
    if not isinstance(status, str) or not status.strip():
        return {"reason": "normalized evidence lacks a bounded extraction status"}
    if status.strip().lower() in {"failed", "stubbed"}:
        return {"reason": f"normalized evidence has unusable extraction status {status!r}"}
    if frontmatter.get("evidence_usable") is not True:
        return {
            "reason": "normalized evidence is not explicitly marked usable",
            "evidence_usable": frontmatter.get("evidence_usable"),
        }

    is_pdf = (
        record.get("kind") == "pdf"
        or isinstance(record.get("raw_pdf"), str)
        or frontmatter.get("source_kind") == "pdf"
        or frontmatter.get("extraction_method") == "pdf_text"
    )
    if is_pdf:
        body = "\n".join(lines[closing_index + 1 :])
        extracted = re.search(
            r"(?ms)^## Extracted Text[ \t]*\n+(.*?)(?=^##[ \t]+|\Z)",
            body,
        )
        extracted_text = extracted.group(1).strip() if extracted is not None else ""
        if not extracted_text or extracted_text.casefold() == "none extracted.":
            return {"reason": "normalized PDF evidence contains no extracted text"}
    return None


def candidate_failure_audit_events(
    project_root: Path,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Read the bounded append-only candidate audit used to prove route failure."""
    discover_sources = load_sibling_module("discover_sources")
    path = discover_sources.candidate_audit_path(project_root, config)
    payload = bounded_regular_bytes(
        path,
        max_bytes=MAX_SCOPE_GUARD_BYTES,
        error_code="ORCHESTRATION_POSTCONDITION_FAILED",
        label="candidate lifecycle audit",
        missing_ok=True,
        containment_root=project_root,
    )
    if payload is None:
        return []
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise OrchestrationControllerError(
            "ORCHESTRATION_POSTCONDITION_FAILED",
            f"candidate lifecycle audit is not valid UTF-8: {exc}",
            recoverable=True,
        ) from exc
    if len(lines) > MAX_SCOPE_GUARD_ENTRIES:
        raise OrchestrationControllerError(
            "ORCHESTRATION_SCOPE_EXCEEDED",
            "candidate lifecycle audit exceeds the bounded integrity-guard limit",
            recoverable=False,
        )
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                f"candidate lifecycle audit contains invalid JSON at line {line_number}: {exc}",
                recoverable=True,
            ) from exc
        if not isinstance(event, dict):
            raise OrchestrationControllerError(
                "ORCHESTRATION_POSTCONDITION_FAILED",
                f"candidate lifecycle audit entry {line_number} is not an object",
                recoverable=True,
            )
        events.append(event)
    return events


def candidate_audit_record_fingerprint_snapshot(
    project_root: Path,
    config: dict[str, Any],
) -> dict[str, str]:
    return record_fingerprint_snapshot(
        candidate_failure_audit_events(project_root, config),
        id_field="event_id",
        label="candidate lifecycle audit",
    )


def verify_action_postconditions(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
    *,
    apply_effects: bool = False,
) -> tuple[str | None, str | None]:
    work_order = require_action_baselines(work_order, project_root)
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
        research_guard = recorded_postcondition("workspace_readiness_changed")
        before_questions = research_guard.get("scoped_questions_before")
        question_files_before = research_guard.get("question_file_fingerprints_before")
        require(
            isinstance(before_questions, dict)
            and bool(before_questions)
            and valid_question_file_fingerprint_snapshot(question_files_before),
            "research work order lacks a scoped question baseline",
            remediation=(
                "This legacy pending action cannot be rebound safely. Preserve it for inspection and start a "
                "fresh orchestration session after upgrading the workspace."
            ),
        )
        scoped_slugs = [value for value in scope.get("question_slugs", []) if isinstance(value, str)]
        current_question_files = question_file_fingerprint_snapshot(project_root, config)
        question_scope_violations = fingerprint_scope_violations(
            question_files_before,
            current_question_files,
            mutable_ids={f"{slug}.md" for slug in scoped_slugs},
        )
        require(
            not any(question_scope_violations.values()),
            "research changed a question file outside the persisted work-order scope",
            {"question_scope_violations": question_scope_violations},
            "Restore every out-of-scope question file and process only question slugs named by this work order.",
        )
        source_requests_before = research_guard.get("source_request_record_fingerprints_before")
        require(
            valid_record_fingerprint_snapshot(source_requests_before),
            "research work order lacks a source-request integrity baseline",
            remediation="Start a fresh orchestration session; never infer source-request creation after execution.",
        )
        source_requests = load_sibling_module("source_requests")
        current_source_request_records = source_requests.load_requests(
            source_requests.requests_path(project_root, config)
        )
        current_source_request_fingerprints = record_fingerprint_snapshot(
            current_source_request_records,
            id_field="request_id",
            label="source-request store",
        )
        scoped_slug_set = set(scoped_slugs)
        allowed_new_request_ids = {
            str(record.get("request_id"))
            for record in current_source_request_records
            if isinstance(record.get("request_id"), str)
            and record.get("request_id") not in source_requests_before
            and record.get("status") == "open"
            and isinstance(record.get("question_slugs"), list)
            and bool(record.get("question_slugs"))
            and {
                slug for slug in record.get("question_slugs", []) if isinstance(slug, str)
            }
            <= scoped_slug_set
        }
        request_scope_violations = fingerprint_scope_violations(
            source_requests_before,
            current_source_request_fingerprints,
            mutable_ids=set(),
            allowed_new_ids=allowed_new_request_ids,
        )
        require(
            not any(request_scope_violations.values()),
            "research changed source requests outside append-only scoped-question creation",
            {"source_request_scope_violations": request_scope_violations},
            "Restore existing requests and keep each new open request linked only to scoped questions.",
        )
        after_questions = scoped_question_snapshot(project_root, config, scoped_slugs)
        terminal_statuses = {"answered", "human_review", "blocked", "deferred", "rejected"}
        progressed_slugs = sorted(
            slug
            for slug, before in before_questions.items()
            if isinstance(before, dict)
            and before.get("status") in {"open", "in_progress"}
            and after_questions.get(slug, {}).get("status") in terminal_statuses
        )
        require(
            bool(progressed_slugs),
            "research completed without terminally processing a scoped question",
            {
                "question_slugs": scoped_slugs,
                "before": before_questions,
                "after": after_questions,
            },
            (
                "Claim and resolve at least one scoped question as answered, blocked, deferred, or rejected; "
                "a claim-only or unchanged backlog is not completed research."
            ),
        )
        invalid_blocked_links: list[dict[str, Any]] = []
        blocked_progressed_slugs = [
            slug for slug in progressed_slugs if after_questions.get(slug, {}).get("status") == "blocked"
        ]
        if blocked_progressed_slugs:
            requests_by_id = {
                str(item.get("request_id")): item
                for item in current_source_request_records
                if isinstance(item, dict) and isinstance(item.get("request_id"), str)
            }
        else:
            requests_by_id = {}
        for slug in blocked_progressed_slugs:
            question = after_questions.get(slug, {})
            linked_ids = question.get("blocking_request_ids", [])
            valid_ids = [
                request_id
                for request_id in linked_ids
                if isinstance(request_id, str)
                and isinstance(requests_by_id.get(request_id), dict)
                and requests_by_id[request_id].get("status") == "open"
                and slug in requests_by_id[request_id].get("question_slugs", [])
            ]
            if not valid_ids:
                invalid_blocked_links.append(
                    {"question_slug": slug, "blocking_request_ids": list(linked_ids)}
                )
        require(
            not invalid_blocked_links,
            "blocked research questions lack open request artifacts linked to the same scoped question",
            {"invalid_blocked_links": invalid_blocked_links},
            (
                "Create a structured source request for each blocked scoped question, then block the question "
                "with that request id before resubmitting."
            ),
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
        all_candidates = load_candidates(project_root, config)
        candidates = [
            candidate
            for candidate in all_candidates
            if candidate_request_id(candidate) in set(request_ids)
        ]
        candidate_guard = recorded_postcondition("request_scoped_candidates_increased")
        before_candidates = int(candidate_guard.get("before", 0) or 0)
        candidate_states_before = candidate_guard.get("candidate_states_before")
        candidate_records_before = candidate_guard.get("candidate_record_fingerprints_before")
        require(
            valid_candidate_state_baseline(candidate_states_before)
            and valid_record_fingerprint_snapshot(candidate_records_before)
            and set(candidate_states_before) <= set(candidate_records_before)
            and len(candidate_states_before) == before_candidates,
            "discovery work order lacks a valid bounded candidate-state baseline",
            remediation="Start a fresh orchestration session; never infer candidate creation after execution.",
        )
        current_candidate_records = candidate_record_fingerprint_snapshot(all_candidates)
        new_in_scope_ids = {
            str(candidate.get("candidate_id"))
            for candidate in candidates
            if isinstance(candidate.get("candidate_id"), str)
            and candidate.get("candidate_id") not in candidate_records_before
        }
        discovery_scope_violations = fingerprint_scope_violations(
            candidate_records_before,
            current_candidate_records,
            mutable_ids=set(),
            allowed_new_ids=new_in_scope_ids,
        )
        require(
            not any(discovery_scope_violations.values()),
            "discovery changed candidate records outside append-only request scope",
            {"candidate_scope_violations": discovery_scope_violations},
            "Restore all pre-existing and out-of-scope candidates; discovery may only append scoped candidates.",
        )
        current_candidate_states = request_candidate_state_snapshot(candidates, request_ids)
        historical_candidate_changes = {
            candidate_id: {"before": state, "after": current_candidate_states.get(candidate_id)}
            for candidate_id, state in candidate_states_before.items()
            if current_candidate_states.get(candidate_id) != state
        }
        require(
            not historical_candidate_changes,
            "discovery changed or removed a candidate that existed before the action",
            {"historical_candidate_changes": historical_candidate_changes},
            "Restore the pre-action candidate records; discovery may append new candidates but not review old ones.",
        )
        require(
            len(current_candidate_states) > before_candidates,
            "discovery produced no new request-scoped candidate",
            {"request_ids": request_ids, "before": before_candidates, "after": len(current_candidate_states)},
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
        enabled_acquisition = set(acquisition.get("providers", [])) if acquisition.get("enabled") is True else set()
        discovery = work_order.get("provider_policy", {}).get("discovery", {})
        enabled_discovery = set(discovery.get("providers", [])) if discovery.get("enabled") is True else set()
        newly_appended = [
            candidate
            for candidate in candidates
            if isinstance(candidate.get("candidate_id"), str)
            and candidate.get("candidate_id") not in candidate_records_before
        ]
        invalid_new_candidates = [
            {
                "candidate_id": candidate.get("candidate_id"),
                "state": candidate_state(candidate),
                "provider": candidate_provider(candidate),
            }
            for candidate in newly_appended
            if candidate_state(candidate) not in DISCOVERY_APPEND_CANDIDATE_STATES
            or not candidate_uses_permitted_discovery_provider(candidate, enabled_discovery)
        ]
        require(
            not invalid_new_candidates,
            "discovery appended candidates outside the enabled discovery-provider policy",
            {"invalid_new_candidates": invalid_new_candidates},
            "Remove candidates from disabled providers or invalid lifecycle states and replay scoped discovery.",
        )
        eligible_new = eligible_new_discovery_candidates(
            candidates,
            request_ids,
            candidate_states_before,
            enabled_discovery,
            enabled_acquisition,
        )
        require(
            bool(eligible_new),
            "discovery produced no newly added, reviewable candidate through permitted end-to-end providers",
            {
                "request_ids": request_ids,
                "new_candidate_ids": sorted(set(current_candidate_states) - set(candidate_states_before)),
                "enabled_discovery_providers": sorted(enabled_discovery),
                "enabled_acquisition_providers": sorted(enabled_acquisition),
            },
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
        candidate_ids = [value for value in scope.get("candidate_ids", []) if isinstance(value, str)]
        selection_guard = recorded_postcondition("selected_candidate_for_request")
        selected_candidate_ids_before = selection_guard.get("selected_candidate_ids_before")
        candidate_records_before = selection_guard.get("candidate_record_fingerprints_before")
        selected_before = selection_guard.get("selected_before")
        require(
            valid_scope_id_list(selected_candidate_ids_before)
            and valid_record_fingerprint_snapshot(candidate_records_before)
            and set(selected_candidate_ids_before) <= set(candidate_records_before)
            and isinstance(selected_before, int)
            and not isinstance(selected_before, bool)
            and selected_before == len(selected_candidate_ids_before),
            "candidate-review work order lacks a valid selected-candidate baseline",
            remediation="Start a fresh orchestration session; never infer selection changes after execution.",
        )
        current_candidate_records = candidate_record_fingerprint_snapshot(load_candidates(project_root, config))
        review_scope_violations = fingerprint_scope_violations(
            candidate_records_before,
            current_candidate_records,
            mutable_ids=set(candidate_ids),
        )
        require(
            not any(review_scope_violations.values()),
            "candidate review changed records outside the persisted candidate scope",
            {"candidate_scope_violations": review_scope_violations},
            "Restore every out-of-scope candidate and review only candidate ids named by this work order.",
        )
        selected = selected_candidates_for_scope(project_root, config, request_ids, candidate_ids)
        historical_selected = set(selected_candidate_ids_before)
        newly_selected = [
            candidate
            for candidate in selected
            if candidate.get("candidate_id") not in historical_selected
        ]
        selected_outside_scope = selected_candidates_outside_scope(
            project_root,
            config,
            request_ids,
            candidate_ids,
            selected_candidate_ids_before,
        )
        require(
            not selected_outside_scope,
            "candidate review selected a candidate outside the persisted work-order scope",
            {
                "scoped_candidate_ids": candidate_ids,
                "out_of_scope_candidate_ids": sorted(
                    str(candidate.get("candidate_id"))
                    for candidate in selected_outside_scope
                    if candidate.get("candidate_id")
                ),
            },
            "Reject or defer out-of-scope selections and select only a candidate id named by this work order.",
        )
        require(
            bool(newly_selected),
            "candidate review did not newly select a candidate from the persisted work-order scope",
        )
        policy = provider_policy(config)
        enabled = set(policy["acquisition"]["providers"]) if policy["acquisition"]["enabled"] else set()
        routable = [candidate for candidate in newly_selected if acquisition_route(candidate, enabled) is not None]
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
        current_manifest_fingerprints = record_fingerprint_snapshot(
            manifest_records,
            id_field="id",
            label="evidence manifest",
        )
        normalized_root = project_root / normalized_relative
        missing_normalized: list[str] = []
        unusable_normalized: list[dict[str, Any]] = []
        for request in fulfilled:
            source_id = str(request.get("source_id") or "")
            record = by_source_id.get(source_id)
            normalized_path = (
                normalize_sources.normalized_output_path_for_record(record, normalized_root)
                if isinstance(record, dict)
                else None
            )
            if not isinstance(normalized_path, Path) or not normalized_path.is_file():
                missing_normalized.append(source_id)
                continue
            quality_failure = normalized_source_quality_failure(project_root, normalized_path, record)
            if quality_failure is not None:
                unusable_normalized.append({"source_id": source_id, **quality_failure})
        require(
            not missing_normalized,
            "fulfilled source requests do not have normalized evidence",
            {"source_ids": missing_normalized},
        )
        require(
            not unusable_normalized,
            "fulfilled source requests do not have usable normalized evidence",
            {"quality_failures": unusable_normalized},
            (
                "Normalize the acquired source successfully before fulfillment; failed or stubbed records and "
                "PDFs without extracted text cannot satisfy a source request."
            ),
        )
        scoped_candidate_ids = {
            value for value in scope.get("candidate_ids", []) if isinstance(value, str) and value
        }
        all_candidates = load_candidates(project_root, config)
        candidates_by_id = {
            str(candidate.get("candidate_id")): candidate
            for candidate in all_candidates
            if isinstance(candidate.get("candidate_id"), str)
        }
        correlation_failures: list[dict[str, Any]] = []
        for request in fulfilled:
            request_id = str(request.get("request_id") or "")
            source_id = str(request.get("source_id") or "")
            record = by_source_id.get(source_id)
            provenance = record.get("provenance") if isinstance(record, dict) else None
            provenance_request_id = provenance.get("request_id") if isinstance(provenance, dict) else None
            provenance_candidate_id = provenance.get("candidate_id") if isinstance(provenance, dict) else None
            candidate = (
                candidates_by_id.get(provenance_candidate_id)
                if isinstance(provenance_candidate_id, str)
                else None
            )
            if (
                provenance_request_id != request_id
                or provenance_candidate_id not in scoped_candidate_ids
                or not isinstance(candidate, dict)
                or candidate_request_id(candidate) != request_id
                or candidate_state(candidate) != "fetched"
                or candidate.get("fetched_source_id") != source_id
            ):
                correlation_failures.append(
                    {
                        "request_id": request_id,
                        "source_id": source_id,
                        "provenance_request_id": provenance_request_id,
                        "provenance_candidate_id": provenance_candidate_id,
                        "candidate_state": candidate_state(candidate) if isinstance(candidate, dict) else None,
                        "candidate_source_id": candidate.get("fetched_source_id")
                        if isinstance(candidate, dict)
                        else None,
                    }
                )
        require(
            not correlation_failures,
            "acquired evidence is not linked from scoped request and candidate provenance to fetched source state",
            {
                "scoped_candidate_ids": sorted(scoped_candidate_ids),
                "correlation_failures": correlation_failures,
            },
            (
                "Acquire a scoped selected candidate with both --request-id and --candidate-id, inventory and "
                "normalize it, then transition that candidate to fetched with the fulfilled manifest source id."
            ),
        )
        fulfilled_by_request_id = {
            str(item.get("request_id")): item
            for item in fulfilled
            if isinstance(item.get("request_id"), str)
        }
        question_guard = recorded_postcondition("linked_blocked_questions_reopened")
        blocked_questions_before = question_guard.get("blocked_questions_before")
        require(
            valid_blocked_question_baseline(blocked_questions_before),
            "acquisition work order lacks a valid blocked-question baseline",
            remediation="Start a fresh orchestration session; never infer question transitions after execution.",
        )
        current_question_evidence = scoped_question_evidence_snapshot(
            project_root,
            config,
            list(blocked_questions_before),
        )
        question_transition_failures: list[dict[str, Any]] = []
        for slug, before in blocked_questions_before.items():
            linked_request_ids = list(before.get("blocking_request_ids", []))
            linked_fulfilled = [
                fulfilled_by_request_id.get(request_id) for request_id in linked_request_ids
            ]
            expected_source_ids = {
                str(request.get("source_id"))
                for request in linked_fulfilled
                if isinstance(request, dict) and isinstance(request.get("source_id"), str)
            }
            current_question = current_question_evidence.get(slug, {})
            current_source_ids = set(current_question.get("source_ids", []))
            current_blocking_ids = set(current_question.get("blocking_request_ids", []))
            required_source_ids = set(before.get("source_ids_before", [])) | expected_source_ids
            if (
                any(not isinstance(request, dict) for request in linked_fulfilled)
                or current_question.get("status") != "open"
                or current_blocking_ids
                or not required_source_ids <= current_source_ids
            ):
                question_transition_failures.append(
                    {
                        "question_slug": slug,
                        "before": before,
                        "after": current_question or None,
                        "expected_source_ids": sorted(required_source_ids),
                        "fulfilled_request_ids": sorted(
                            request_id
                            for request_id in linked_request_ids
                            if isinstance(fulfilled_by_request_id.get(request_id), dict)
                        ),
                    }
                )
        require(
            not question_transition_failures,
            "acquisition did not reopen every scoped blocked question with fulfilled request/source linkage",
            {"question_transition_failures": question_transition_failures},
            (
                "Fulfill each scoped request, then use question_resolve.py reopen so every baseline-blocked "
                "question is exactly open, has the fulfilled source id, and has no remaining blocking links."
            ),
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
        manifest_guard = recorded_postcondition("manifest_records_increased")
        before_manifest = int(manifest_guard.get("before", 0) or 0)
        matching_source_ids_before = manifest_guard.get("matching_source_ids_before")
        matching_source_records_before = manifest_guard.get("matching_source_records_before")
        manifest_records_before = manifest_guard.get("manifest_record_fingerprints_before")
        raw_tree_before = manifest_guard.get("raw_tree_before")
        candidate_records_before = manifest_guard.get("candidate_record_fingerprints_before")
        source_requests_before = manifest_guard.get("source_request_record_fingerprints_before")
        normalized_files_before = manifest_guard.get("normalized_file_fingerprints_before")
        question_files_before = manifest_guard.get("question_file_fingerprints_before")
        require(
            valid_scope_id_list(matching_source_ids_before)
            and valid_matching_source_record_snapshot(matching_source_records_before)
            and set(matching_source_ids_before) == set(matching_source_records_before)
            and valid_record_fingerprint_snapshot(manifest_records_before)
            and set(matching_source_ids_before) <= set(manifest_records_before)
            and before_manifest == len(manifest_records_before)
            and valid_raw_tree_snapshot(raw_tree_before, include_entries=True)
            and valid_record_fingerprint_snapshot(candidate_records_before)
            and valid_record_fingerprint_snapshot(source_requests_before)
            and valid_file_fingerprint_snapshot(normalized_files_before, prefix="sources/")
            and valid_question_file_fingerprint_snapshot(question_files_before),
            "acquisition work order lacks a valid bounded evidence integrity baseline",
            remediation="Start a fresh orchestration session; never infer matching evidence after execution.",
        )
        current_source_request_fingerprints = record_fingerprint_snapshot(
            all_requests,
            id_field="request_id",
            label="source-request store",
        )
        request_scope_violations = fingerprint_scope_violations(
            source_requests_before,
            current_source_request_fingerprints,
            mutable_ids=set(request_ids),
        )
        require(
            not any(request_scope_violations.values()),
            "acquisition changed source requests outside the persisted request scope",
            {"source_request_scope_violations": request_scope_violations},
            "Restore every out-of-scope request and fulfill only request ids named by this work order.",
        )
        current_question_files = question_file_fingerprint_snapshot(project_root, config)
        acquisition_question_scope_violations = fingerprint_scope_violations(
            question_files_before,
            current_question_files,
            mutable_ids={f"{slug}.md" for slug in scope.get("question_slugs", []) if isinstance(slug, str)},
        )
        require(
            not any(acquisition_question_scope_violations.values()),
            "acquisition changed question files outside the persisted question scope",
            {"question_scope_violations": acquisition_question_scope_violations},
            "Restore every out-of-scope question and reopen only questions named by this work order.",
        )
        fulfilled_source_ids = {
            str(item.get("source_id"))
            for item in fulfilled
            if isinstance(item.get("source_id"), str) and item.get("source_id")
        }
        manifest_scope_violations = fingerprint_scope_violations(
            manifest_records_before,
            current_manifest_fingerprints,
            mutable_ids=set(),
            allowed_new_ids=fulfilled_source_ids,
        )
        require(
            not any(manifest_scope_violations.values()),
            "acquisition changed, removed, or added evidence-manifest records outside fulfilled source scope",
            {
                "manifest_scope_violations": manifest_scope_violations,
                "fulfilled_source_ids": sorted(fulfilled_source_ids),
            },
            "Restore existing and out-of-scope manifest records; only fulfilled scoped sources may be appended.",
        )
        expected_new_source_ids = fulfilled_source_ids - set(manifest_records_before)
        actual_new_source_ids = set(current_manifest_fingerprints) - set(manifest_records_before)
        require(
            actual_new_source_ids == expected_new_source_ids,
            "fulfilled sources are not exactly accounted for by pre-existing matches or new manifest ids",
            {
                "expected_new_source_ids": sorted(expected_new_source_ids),
                "actual_new_source_ids": sorted(actual_new_source_ids),
                "matching_source_ids_before": matching_source_ids_before,
            },
        )
        preexisting_fulfilled = fulfilled_source_ids & set(manifest_records_before)
        reconciliation_failures: list[dict[str, Any]] = []
        for source_id in sorted(preexisting_fulfilled):
            expected = matching_source_records_before.get(source_id)
            record = by_source_id.get(source_id)
            normalized_path = (
                normalize_sources.normalized_output_path_for_record(record, normalized_root)
                if isinstance(record, dict)
                else None
            )
            normalized_fingerprint = (
                file_digest(
                    normalized_path,
                    max_bytes=MAX_VERIFICATION_ARTIFACT_BYTES,
                    containment_root=project_root,
                )
                if isinstance(normalized_path, Path) and normalized_path.is_file()
                else None
            )
            if (
                not isinstance(expected, dict)
                or current_manifest_fingerprints.get(source_id) != expected.get("record_fingerprint")
                or normalized_fingerprint != expected.get("normalized_fingerprint")
            ):
                reconciliation_failures.append(
                    {
                        "source_id": source_id,
                        "was_scoped_match": isinstance(expected, dict),
                        "record_unchanged": isinstance(expected, dict)
                        and current_manifest_fingerprints.get(source_id) == expected.get("record_fingerprint"),
                        "normalized_unchanged": isinstance(expected, dict)
                        and normalized_fingerprint == expected.get("normalized_fingerprint"),
                    }
                )
        require(
            not reconciliation_failures,
            "pre-existing fulfilled evidence is not an unchanged exact scoped reconciliation match",
            {"reconciliation_failures": reconciliation_failures},
            "Use only the unchanged scoped pre-existing source or acquire a genuinely new source id.",
        )

        current_normalized_files = normalized_file_fingerprint_snapshot(project_root, config)
        allowed_new_normalized_paths: set[str] = set()
        for source_id in expected_new_source_ids:
            record = by_source_id.get(source_id)
            if not isinstance(record, dict):
                continue
            normalized_path = normalize_sources.normalized_output_path_for_record(record, normalized_root)
            allowed_new_normalized_paths.add(relative_workspace_path(project_root, normalized_path))
        normalized_scope_violations = fingerprint_scope_violations(
            normalized_files_before,
            current_normalized_files,
            mutable_ids=set(),
            allowed_new_ids=allowed_new_normalized_paths,
        )
        require(
            not any(normalized_scope_violations.values()),
            "acquisition changed normalized evidence outside newly fulfilled source scope",
            {"normalized_scope_violations": normalized_scope_violations},
            "Restore existing normalized evidence and keep new outputs limited to newly fulfilled sources.",
        )
        require(
            (set(current_normalized_files) - set(normalized_files_before)) == allowed_new_normalized_paths,
            "new fulfilled sources do not map exactly to newly created normalized outputs",
            {
                "expected_new_normalized_paths": sorted(allowed_new_normalized_paths),
                "actual_new_normalized_paths": sorted(
                    set(current_normalized_files) - set(normalized_files_before)
                ),
            },
        )

        current_raw_tree = raw_tree_snapshot(project_root, config, include_entries=True)
        before_raw_entries = raw_tree_before["entries"]
        current_raw_entries = current_raw_tree["entries"]
        raw_existing_changes = fingerprint_scope_violations(
            before_raw_entries,
            current_raw_entries,
            mutable_ids=set(),
            allowed_new_ids=set(current_raw_entries) - set(before_raw_entries),
        )
        allowed_new_raw_paths: set[str] = set()
        for source_id in expected_new_source_ids:
            record = by_source_id.get(source_id)
            raw_paths = record.get("raw_paths") if isinstance(record, dict) else None
            if not isinstance(raw_paths, list):
                continue
            for raw_path in raw_paths:
                if isinstance(raw_path, str) and raw_path.startswith("raw/") and safe_snapshot_relative_path(raw_path):
                    allowed_new_raw_paths.add(raw_path)
                    allowed_new_raw_paths.add(f"{raw_path}.provenance.yml")
        actual_new_raw_paths = set(current_raw_entries) - set(before_raw_entries)
        unexpected_new_raw_paths = sorted(actual_new_raw_paths - allowed_new_raw_paths)
        require(
            not any(raw_existing_changes.values()) and not unexpected_new_raw_paths,
            "acquisition changed raw evidence outside newly fulfilled manifest source scope",
            {
                "raw_scope_violations": raw_existing_changes,
                "unexpected_new_raw_paths": unexpected_new_raw_paths[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
                "allowed_new_raw_paths": sorted(allowed_new_raw_paths)[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
            },
            "Restore existing raw evidence and remove deliveries not referenced by newly fulfilled scoped sources.",
        )

        current_candidate_fingerprints = candidate_record_fingerprint_snapshot(all_candidates)
        candidate_scope_violations = fingerprint_scope_violations(
            candidate_records_before,
            current_candidate_fingerprints,
            mutable_ids=scoped_candidate_ids,
        )
        require(
            not any(candidate_scope_violations.values()),
            "acquisition changed candidate records outside the persisted candidate scope",
            {"candidate_scope_violations": candidate_scope_violations},
            "Restore every out-of-scope candidate and transition only the scoped candidate to fetched.",
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


def verify_blocked_action_postconditions(
    project_root: Path,
    session: dict[str, Any],
    work_order: dict[str, Any],
) -> tuple[str, str | None]:
    """Classify a blocked action without trusting its human-readable summary.

    Most blocked actions remain pending and are replayed after an explicit
    resume. Acquisition has one additional bounded outcome: an audited failure
    of the scoped selected candidate completes that route attempt so planning
    can continue with another candidate.
    """
    work_order = require_action_baselines(work_order, project_root)
    if work_order.get("phase") != "acquisition":
        return PAUSED_STATUS, None

    config = load_config(project_root)
    scope = work_order.get("scope") if isinstance(work_order.get("scope"), dict) else {}
    request_ids = [value for value in scope.get("request_ids", []) if isinstance(value, str)]
    candidate_ids = [value for value in scope.get("candidate_ids", []) if isinstance(value, str)]
    request_scope = set(request_ids)
    candidate_scope = set(candidate_ids)

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
                remediation=remediation or "Restore the persisted acquisition baseline and replay the same action.",
                details=details,
            )

    require(
        bool(request_scope)
        and bool(candidate_scope)
        and valid_scope_id_list(request_ids)
        and valid_scope_id_list(candidate_ids),
        "blocked acquisition lacks a bounded request and candidate scope",
    )
    postconditions = {
        item.get("check"): item
        for item in work_order.get("required_postconditions", [])
        if isinstance(item, dict) and isinstance(item.get("check"), str)
    }
    manifest_guard = postconditions.get("manifest_records_increased", {})
    manifest_records_before = manifest_guard.get("manifest_record_fingerprints_before")
    raw_tree_before = manifest_guard.get("raw_tree_before")
    candidate_records_before = manifest_guard.get("candidate_record_fingerprints_before")
    candidate_audit_records_before = manifest_guard.get(
        "candidate_audit_record_fingerprints_before"
    )
    source_requests_before = manifest_guard.get("source_request_record_fingerprints_before")
    normalized_files_before = manifest_guard.get("normalized_file_fingerprints_before")
    question_files_before = manifest_guard.get("question_file_fingerprints_before")
    before_manifest = manifest_guard.get("before")
    require(
        valid_record_fingerprint_snapshot(manifest_records_before)
        and isinstance(before_manifest, int)
        and not isinstance(before_manifest, bool)
        and before_manifest == len(manifest_records_before)
        and valid_raw_tree_snapshot(raw_tree_before, include_entries=True)
        and valid_record_fingerprint_snapshot(candidate_records_before)
        and candidate_scope <= set(candidate_records_before)
        and valid_record_fingerprint_snapshot(candidate_audit_records_before)
        and valid_record_fingerprint_snapshot(source_requests_before)
        and request_scope <= set(source_requests_before)
        and valid_file_fingerprint_snapshot(normalized_files_before, prefix="sources/")
        and valid_question_file_fingerprint_snapshot(question_files_before),
        "blocked acquisition lacks its exact pre-action integrity baseline",
        remediation="Preserve this action for audit and start a fresh orchestration; do not infer a baseline.",
    )

    current_source_requests = source_request_record_fingerprint_snapshot(project_root, config)
    require(
        current_source_requests == source_requests_before,
        "blocked acquisition changed the source-request store",
        {
            "source_request_scope_violations": fingerprint_scope_violations(
                source_requests_before,
                current_source_requests,
                mutable_ids=set(),
            )
        },
        "Restore every request to its pre-action state; a blocked attempt cannot fulfill a request.",
    )
    current_question_files = question_file_fingerprint_snapshot(project_root, config)
    require(
        current_question_files == question_files_before,
        "blocked acquisition changed question files",
        {
            "question_scope_violations": fingerprint_scope_violations(
                question_files_before,
                current_question_files,
                mutable_ids=set(),
            )
        },
        "Restore every question to its pre-action state; a blocked attempt cannot reopen a question.",
    )

    all_candidates = load_candidates(project_root, config)
    candidates_by_id = {
        str(candidate.get("candidate_id")): candidate
        for candidate in all_candidates
        if isinstance(candidate.get("candidate_id"), str)
    }
    current_candidate_fingerprints = candidate_record_fingerprint_snapshot(all_candidates)
    candidate_scope_violations = fingerprint_scope_violations(
        candidate_records_before,
        current_candidate_fingerprints,
        mutable_ids=candidate_scope,
    )
    require(
        not any(candidate_scope_violations.values()),
        "blocked acquisition changed candidate records outside the persisted candidate scope",
        {"candidate_scope_violations": candidate_scope_violations},
        "Restore every out-of-scope candidate and mutate only the selected candidate named by this work order.",
    )
    scoped_candidates = [candidates_by_id.get(candidate_id) for candidate_id in candidate_ids]
    candidate_correlation_failures = [
        candidate_id
        for candidate_id, candidate in zip(candidate_ids, scoped_candidates, strict=True)
        if not isinstance(candidate, dict) or candidate_request_id(candidate) not in request_scope
    ]
    require(
        not candidate_correlation_failures,
        "blocked acquisition lost its request-to-candidate correlation",
        {"candidate_ids": candidate_correlation_failures},
    )
    candidate_states = {
        candidate_id: candidate_state(candidate)
        for candidate_id, candidate in zip(candidate_ids, scoped_candidates, strict=True)
        if isinstance(candidate, dict)
    }
    require(
        set(candidate_states.values()) <= {"selected", "failed"}
        and len(set(candidate_states.values())) == 1,
        "blocked acquisition must leave every scoped candidate selected or transition it to failed",
        {"candidate_states": candidate_states},
        (
            "Leave retryable candidates selected, or use discover_sources.py candidates transition with "
            "--expected-state selected --to-state failed for a candidate-specific route failure."
        ),
    )
    route_failed = bool(candidate_states) and next(iter(candidate_states.values())) == "failed"
    audit_events = candidate_failure_audit_events(project_root, config)
    current_audit_fingerprints = record_fingerprint_snapshot(
        audit_events,
        id_field="event_id",
        label="candidate lifecycle audit",
    )
    new_audit_event_ids = set(current_audit_fingerprints) - set(candidate_audit_records_before)
    audit_scope_violations = fingerprint_scope_violations(
        candidate_audit_records_before,
        current_audit_fingerprints,
        mutable_ids=set(),
        allowed_new_ids=new_audit_event_ids,
    )
    require(
        not any(audit_scope_violations.values()),
        "blocked acquisition changed existing candidate lifecycle audit records",
        {"candidate_audit_scope_violations": audit_scope_violations},
        "Restore the append-only candidate lifecycle audit and replay the same action.",
    )
    if not route_failed:
        changed_selected = [
            candidate_id
            for candidate_id in candidate_ids
            if current_candidate_fingerprints.get(candidate_id)
            != candidate_records_before.get(candidate_id)
        ]
        require(
            not changed_selected,
            "retryable blocked acquisition changed its selected candidate record",
            {"changed_candidate_ids": changed_selected},
            "Restore the selected candidate record, then resume and replay the same action.",
        )
        require(
            not new_audit_event_ids,
            "retryable blocked acquisition appended candidate lifecycle events",
            {"new_candidate_audit_event_ids": sorted(new_audit_event_ids)},
            "Remove the unexpected lifecycle events, leave the scoped candidate selected, and replay the action.",
        )
    else:
        run_id = work_order.get("run_id")
        require(
            len(new_audit_event_ids) == len(candidate_ids),
            "candidate-specific acquisition failure did not append exactly one audit event per scoped candidate",
            {
                "candidate_ids": candidate_ids,
                "new_candidate_audit_event_ids": sorted(new_audit_event_ids),
            },
        )
        new_audit_events = [
            event for event in audit_events if event.get("event_id") in new_audit_event_ids
        ]
        invalid_failures: list[dict[str, Any]] = []
        for candidate_id, candidate in zip(candidate_ids, scoped_candidates, strict=True):
            if not isinstance(candidate, dict):  # pragma: no cover - correlation was checked above
                invalid_failures.append(
                    {
                        "candidate_id": candidate_id,
                        "record_is_valid": False,
                        "audit_matches": False,
                    }
                )
                continue
            request_id = candidate_request_id(candidate)
            reason = candidate.get("failure_reason")
            actor = candidate.get("failed_by")
            failed_at = candidate.get("failed_at")
            record_is_valid = (
                current_candidate_fingerprints.get(candidate_id)
                != candidate_records_before.get(candidate_id)
                and candidate.get("fetch_status") == "failed"
                and candidate.get("selection_status") == "selected"
                and isinstance(reason, str)
                and bool(reason.strip())
                and isinstance(actor, str)
                and bool(actor.strip())
                and isinstance(failed_at, str)
                and bool(failed_at.strip())
                and candidate.get("lifecycle_reason") == reason
                and candidate.get("lifecycle_updated_by") == actor
                and candidate.get("lifecycle_updated_at") == failed_at
                and candidate.get("lifecycle_run_id") == run_id
            )
            audit_matches = any(
                event.get("event_type") == "candidate_transition"
                and event.get("event") == "transition"
                and event.get("candidate_id") == candidate_id
                and event.get("prior_state") == "selected"
                and event.get("new_state") == "failed"
                and event.get("request_id") == request_id
                and event.get("run_id") == run_id
                and event.get("actor") == actor
                and event.get("reason") == reason
                and event.get("at") == failed_at
                for event in new_audit_events
            )
            if not record_is_valid or not audit_matches:
                invalid_failures.append(
                    {
                        "candidate_id": candidate_id,
                        "record_is_valid": record_is_valid,
                        "audit_matches": audit_matches,
                    }
                )
        require(
            not invalid_failures,
            "candidate-specific acquisition failure lacks its canonical selected-to-failed audit",
            {"invalid_candidate_failures": invalid_failures},
            (
                "Record the route failure with discover_sources.py candidates transition using the work-order "
                "candidate id, request id, run id, and expected selected state."
            ),
        )

    normalize_sources = load_sibling_module("normalize_sources")
    manifest_relative, normalized_relative = normalize_sources.source_paths(config)
    manifest_records = normalize_sources.load_manifest(project_root / manifest_relative)
    current_manifest_fingerprints = record_fingerprint_snapshot(
        manifest_records,
        id_field="id",
        label="evidence manifest",
    )
    actual_new_source_ids = set(current_manifest_fingerprints) - set(manifest_records_before)
    manifest_scope_violations = fingerprint_scope_violations(
        manifest_records_before,
        current_manifest_fingerprints,
        mutable_ids=set(),
        allowed_new_ids=actual_new_source_ids,
    )
    require(
        not any(manifest_scope_violations.values()),
        "blocked acquisition changed existing evidence-manifest records",
        {"manifest_scope_violations": manifest_scope_violations},
        "Restore every pre-action manifest record; partial acquisition may only append scoped records.",
    )
    correlated_records: list[dict[str, Any]] = []
    uncorrelated_new_sources: list[str] = []
    for record in manifest_records:
        source_id = record.get("id") if isinstance(record, dict) else None
        provenance = record.get("provenance") if isinstance(record, dict) else None
        request_id = provenance.get("request_id") if isinstance(provenance, dict) else None
        candidate_id = provenance.get("candidate_id") if isinstance(provenance, dict) else None
        candidate = candidates_by_id.get(candidate_id) if isinstance(candidate_id, str) else None
        correlated = (
            request_id in request_scope
            and candidate_id in candidate_scope
            and isinstance(candidate, dict)
            and candidate_request_id(candidate) == request_id
        )
        if correlated:
            correlated_records.append(record)
        elif source_id in actual_new_source_ids:
            uncorrelated_new_sources.append(str(source_id))
    require(
        not uncorrelated_new_sources,
        "blocked acquisition appended manifest records outside its request/candidate correlation",
        {"source_ids": sorted(uncorrelated_new_sources)},
        "Remove uncorrelated manifest additions and retain only records tied to the scoped request and candidate.",
    )

    normalized_root = project_root / normalized_relative
    allowed_new_normalized_paths = {
        relative_workspace_path(
            project_root,
            normalize_sources.normalized_output_path_for_record(record, normalized_root),
        )
        for record in correlated_records
    }
    current_normalized_files = normalized_file_fingerprint_snapshot(project_root, config)
    actual_new_normalized_paths = set(current_normalized_files) - set(normalized_files_before)
    normalized_scope_violations = fingerprint_scope_violations(
        normalized_files_before,
        current_normalized_files,
        mutable_ids=set(),
        allowed_new_ids=actual_new_normalized_paths,
    )
    unexpected_normalized_paths = sorted(
        actual_new_normalized_paths - allowed_new_normalized_paths
    )
    require(
        not any(normalized_scope_violations.values()) and not unexpected_normalized_paths,
        "blocked acquisition changed normalized evidence outside correlated partial outputs",
        {
            "normalized_scope_violations": normalized_scope_violations,
            "unexpected_new_normalized_paths": unexpected_normalized_paths,
        },
        "Restore existing normalized evidence and remove outputs not correlated to the scoped acquisition.",
    )

    allowed_new_raw_paths: set[str] = set()
    for record in correlated_records:
        raw_paths = record.get("raw_paths") if isinstance(record.get("raw_paths"), list) else []
        for raw_path in raw_paths:
            if (
                isinstance(raw_path, str)
                and raw_path.startswith("raw/")
                and safe_snapshot_relative_path(raw_path)
            ):
                allowed_new_raw_paths.add(raw_path)
                allowed_new_raw_paths.add(f"{raw_path}.provenance.yml")
    current_raw_tree = raw_tree_snapshot(project_root, config, include_entries=True)
    before_raw_entries = raw_tree_before["entries"]
    current_raw_entries = current_raw_tree["entries"]
    actual_new_raw_paths = set(current_raw_entries) - set(before_raw_entries)
    raw_scope_violations = fingerprint_scope_violations(
        before_raw_entries,
        current_raw_entries,
        mutable_ids=set(),
        allowed_new_ids=actual_new_raw_paths,
    )
    unexpected_raw_paths = sorted(actual_new_raw_paths - allowed_new_raw_paths)
    require(
        not any(raw_scope_violations.values()) and not unexpected_raw_paths,
        "blocked acquisition changed raw evidence outside correlated partial deliveries",
        {
            "raw_scope_violations": raw_scope_violations,
            "unexpected_new_raw_paths": unexpected_raw_paths[:MAX_TRUSTED_STATIC_INPUT_DIFFERENCES],
        },
        "Restore existing raw evidence and remove deliveries not referenced by a correlated manifest record.",
    )

    controller = load_sibling_module("run_controller")
    run_id = work_order.get("run_id")
    run_state = controller.load_run_state(project_root, run_id) if isinstance(run_id, str) else None
    current_child_state = (
        run_state.get("state", {}).get("current") if isinstance(run_state, dict) else None
    )
    require(
        current_child_state in {"fetching", "evidence_ready"},
        "blocked acquisition child run is in an invalid state",
        {"child_state": current_child_state},
    )
    if route_failed:
        return "planning", "The scoped candidate route failed; planning may continue with remaining routes."
    return PAUSED_STATUS, "The scoped acquisition remains pending and can be replayed after resume."


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
        next_phase, completion_reason = verify_blocked_action_postconditions(
            project_root,
            session,
            work_order,
        )
        completion_reason = completion_reason or result["summary"]
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
    expected_phase = pending.get("next_phase")
    completion_reason = pending.get("completion_reason")
    if result["outcome"] == "blocked":
        verified_phase, verified_reason = verify_blocked_action_postconditions(
            project_root,
            session,
            work_order,
        )
        if verified_phase != expected_phase:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "accepted blocked submission no longer verifies to its prepared next phase",
                recoverable=True,
            )
        completion_reason = verified_reason or completion_reason

    existing = retained_result(project_root, session["orchestration_id"], action_id)
    if existing is not None and existing != result:
        raise OrchestrationControllerError(
            "RESULT_CONFLICT",
            f"action {action_id} already has a different retained result",
            recoverable=False,
        )
    if result["outcome"] == "blocked" and expected_phase == PAUSED_STATUS:
        if existing is not None:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "retryable blocked action already has a retained completion result",
                recoverable=False,
            )
        session["pending_submission"] = None
        session["recovery"] = default_recovery_state()
        session["status"] = PAUSED_STATUS
        session["phase"] = PAUSED_STATUS
        session["verdict"] = PAUSED_STATUS
        session["pause_reason"] = result["summary"]
        session["completed_at"] = None
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
        record_event_once(
            project_root,
            session,
            "action_paused",
            result["summary"],
            action_id=action_id,
            data={"outcome": result["outcome"], "resume_replays_action": True},
        )
        return session
    if existing is None:
        write_json_atomic(work_result_path(project_root, session["orchestration_id"], action_id), result)

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
        pass
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
        require_action_baselines(order, project_root)
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
