#!/usr/bin/env python3
"""Manage durable PM run-controller state for one research workspace run.

The controller is a local-only state writer. It records phase transitions and
audit events under ``runs/<run_id>/`` and never performs discovery, acquisition,
normalization, or answer work itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_INVALID = 2

STATE_TRANSITIONS = {
    "initialized": ("planned", "failed"),
    "planned": ("discovering", "answering", "no_ship", "failed"),
    "discovering": ("candidates_ready", "blocked_on_sources", "failed"),
    "candidates_ready": ("fetch_planned", "blocked_on_sources", "failed"),
    "fetch_planned": ("fetching", "blocked_on_sources", "failed"),
    "fetching": ("evidence_ready", "blocked_on_sources", "failed"),
    "evidence_ready": ("answering", "blocked_on_sources", "failed"),
    "answering": ("verifying", "blocked_on_sources", "no_ship", "failed"),
    "verifying": ("complete", "blocked_on_sources", "no_ship", "failed"),
    "complete": (),
    "blocked_on_sources": (),
    "no_ship": (),
    "failed": (),
}
STATE_NAMES = tuple(STATE_TRANSITIONS)
TERMINAL_STATES = ("complete", "blocked_on_sources", "no_ship", "failed")
NONTERMINAL_STATES = tuple(state for state in STATE_NAMES if state not in TERMINAL_STATES)
CANDIDATE_STATUSES = ("new", "selected", "rejected", "fetched")
RUN_ID_INVALID_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
CUSTOM_EVENT_TYPE_RE = re.compile(r"^custom\.[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
EVENT_TYPES = frozenset(
    {
        "state_transition",
        "checkpoint",
        "delegation_failed",
        "budget_override",
        "budget_divergence",
        "heartbeat",
        "mutation_recovered",
        "run_adopted",
        "run_abandoned",
        "source_request_opened",
        "source_request_fulfilled",
        "candidate_discovered",
        "candidate_selected",
        "candidate_rejected",
        "fetch_planned",
        "fetch_failed",
        "acquisition_completed",
        "verification_failed",
    }
)

RUN_STATE_FILENAME = "run-state.json"
EVENTS_FILENAME = "events.jsonl"
STATUS_BASELINE_FILENAME = "workspace-status-initial.json"
RUN_REPORT_BASELINE_FILENAME = "run-report-baseline.json"
ACADEMIC_PROVIDER_REQUESTS_FILENAME = "academic-provider-requests.jsonl"
ACADEMIC_PROVIDER_ACCOUNTING_FIELD = "academic_provider_request_accounting"
ACADEMIC_PROVIDER_ACCOUNTING_SCHEMA_VERSION = "1.0"
PENDING_EVENT_FIELD = "_pending_event"
MUTATION_RECOVERY_DIR = ".recovery"
JOURNAL_MAX_BYTES = 16 * 1024 * 1024
JOURNAL_MAX_EVENT_BYTES = 1024 * 1024
JOURNAL_MAX_EVENTS = 4096
JOURNAL_MAX_LINES = 8192
JOURNAL_MAX_DEPTH = 32
JOURNAL_MAX_TOKENS = 262144
TELEMETRY_EVENT_TYPE = "custom.research.telemetry"

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _native_fs import os as _native_os
from _script_errors import emit_error, error_envelope, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError, workspace_lock
from _workspace_module_loader import load_workspace_module


class RunControllerError(Exception):
    """A refused run-controller operation with a stable machine error code."""

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
        envelope = error_envelope(error_code, message, recoverable=recoverable,
                                  remediation=remediation, details=details)
        self.error_code = error_code
        self.exit_code = exit_code
        self.recoverable = envelope["recoverable"]
        self.remediation = envelope["remediation"]
        self.details = details

    def to_envelope(self) -> dict[str, Any]:
        return {**error_envelope(self.error_code, str(self), recoverable=self.recoverable,
                                remediation=self.remediation, details=self.details), "exit_code": self.exit_code}


def parse_non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def parse_positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def add_common_command_options(parser: argparse.ArgumentParser, *, needs_run_id: bool = True) -> None:
    if needs_run_id:
        parser.add_argument("--run-id", default=None, help="Run id under runs/<run_id>.")
    parser.add_argument("--agent-id", default=None, help="Identifier of the PM or orchestrator agent.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format. Defaults to text.")


def add_budget_counter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--questions-processed-this-run",
        type=parse_non_negative_int,
        default=None,
        help="Questions terminally processed by this runner so far.",
    )
    parser.add_argument(
        "--source-requests-opened-this-run",
        type=parse_non_negative_int,
        default=None,
        help="Source requests opened by this runner so far.",
    )
    parser.add_argument(
        "--releases-this-run",
        type=parse_non_negative_int,
        default=None,
        help="Successful claim releases by this runner so far.",
    )
    parser.add_argument(
        "--discovery-results-this-run",
        type=parse_non_negative_int,
        default=None,
        help="Discovery candidates or result records proposed by this runner so far.",
    )
    parser.add_argument(
        "--acquisition-downloads-this-run",
        type=parse_non_negative_int,
        default=None,
        help="Provider-backed downloads completed by this runner so far.",
    )
    parser.add_argument(
        "--github-archive-bytes-this-run",
        type=parse_non_negative_int,
        default=None,
        help="GitHub archive bytes downloaded by this runner so far.",
    )
    parser.add_argument(
        "--academic-provider-requests-this-run",
        type=parse_non_negative_int,
        default=None,
        help="OpenAlex/arXiv provider requests made by this runner so far.",
    )
    parser.add_argument(
        "--web-downloads-this-run",
        type=parse_non_negative_int,
        default=None,
        help="Contracted web provider downloads completed by this runner so far.",
    )
    parser.add_argument(
        "--manual-url-deliveries-this-run",
        type=parse_non_negative_int,
        default=None,
        help="Manual URL deliveries completed by this runner so far.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage durable PM run-controller state.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Create a new run controller artifact.")
    start_parser.add_argument("--run-id", default=None, help="Optional run id. Defaults to a UTC timestamp id.")
    start_parser.add_argument("--caller", action="store_true", help="Bind current caller instructions, policy and run ownership.")
    add_common_command_options(start_parser, needs_run_id=False)

    transition_parser = subparsers.add_parser("transition", help="Move an active run to a non-terminal state.")
    transition_parser.add_argument(
        "--to-state",
        default=None,
        choices=NONTERMINAL_STATES,
        help="Next non-terminal state.",
    )
    transition_parser.add_argument("--reason", default=None, help="Human-readable transition reason.")
    add_common_command_options(transition_parser)
    add_budget_counter_options(transition_parser)

    status_parser = subparsers.add_parser("status", help="Read the current run-state snapshot.")
    status_parser.add_argument("--run-id", default=None, help="Run id under runs/<run_id>.")
    status_parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")

    event_parser = subparsers.add_parser("event", help="Append a custom audit event.")
    event_parser.add_argument("--event-type", default=None, help="Non-empty event type.")
    event_parser.add_argument("--message", default=None, help="Non-empty event message.")
    event_parser.add_argument("--data-json", default=None, help="Optional JSON object stored as event data.")
    add_common_command_options(event_parser)

    heartbeat_parser = subparsers.add_parser("heartbeat", help="Refresh active-run liveness.")
    add_common_command_options(heartbeat_parser)

    adopt_parser = subparsers.add_parser("adopt", help="Transfer ownership of a stale active run.")
    adopt_parser.add_argument(
        "--if-stale-hours",
        type=parse_positive_float,
        default=None,
        help="Only adopt when the run liveness age is at least this many hours.",
    )
    add_common_command_options(adopt_parser)

    abandon_parser = subparsers.add_parser("abandon", help="Mark a stale active run failed for recovery.")
    abandon_parser.add_argument(
        "--if-stale-hours",
        type=parse_positive_float,
        default=None,
        help="Only abandon when the run liveness age is at least this many hours.",
    )
    abandon_parser.add_argument("--reason", default=None, help="Human-readable abandonment reason.")
    add_common_command_options(abandon_parser)

    recover_parser = subparsers.add_parser(
        "recover",
        help="Complete an interrupted run-state/event commit without changing run ownership.",
    )
    add_common_command_options(recover_parser)

    override_parser = subparsers.add_parser(
        "override-manual-url-budget",
        help="Record an approved manual URL delivery budget override.",
    )
    override_parser.add_argument(
        "--new-limit",
        type=parse_non_negative_int,
        required=True,
        help="New maximum manual URL deliveries allowed for this run.",
    )
    override_parser.add_argument("--override-reason", required=True, help="Why the override is needed.")
    override_parser.add_argument("--approved-by", required=True, help="Supervisor or owner approving the override.")
    add_common_command_options(override_parser)

    finish_parser = subparsers.add_parser("finish", help="Finish an active run with a terminal verdict.")
    finish_parser.add_argument("--final-verdict", default=None, choices=TERMINAL_STATES, help="Terminal run verdict.")
    finish_parser.add_argument("--reason", default=None, help="Human-readable terminal reason.")
    add_common_command_options(finish_parser)
    add_budget_counter_options(finish_parser)

    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so contracts stay centralized."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, str) and value.strip():
        text = value.strip()
    elif hasattr(value, "isoformat"):
        text = str(value.isoformat())
    else:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generated_run_id() -> str:
    return "run-" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def require_agent_id(value: str | None) -> str:
    agent_id = value.strip() if isinstance(value, str) else ""
    if not agent_id:
        raise RunControllerError("AGENT_ID_INVALID", "--agent-id must be a non-empty string")
    return agent_id


def validate_run_id(value: str | None, *, allow_generate: bool = False) -> str:
    if value is None and allow_generate:
        value = generated_run_id()
    run_id = value.strip() if isinstance(value, str) else ""
    if not run_id:
        raise RunControllerError("RUN_ID_REQUIRED", "--run-id is required for this command")
    invalid = (
        run_id != value
        or "/" in run_id
        or "\\" in run_id
        or ".." in run_id
        or run_id in {".", ".."}
        or RUN_ID_INVALID_RE.search(run_id) is not None
    )
    if invalid:
        raise RunControllerError(
            "RUN_ID_INVALID",
            f"invalid run id: {value}",
            remediation="Use a plain filename-safe run id such as run-2026-06-29T010203Z.",
            details={"run_id": value},
        )
    return run_id


def runs_root(project_root: Path) -> Path:
    return project_root / "runs"


def run_dir(project_root: Path, run_id: str) -> Path:
    return runs_root(project_root) / run_id


def run_state_path(project_root: Path, run_id: str) -> Path:
    return run_dir(project_root, run_id) / RUN_STATE_FILENAME


def events_path(project_root: Path, run_id: str) -> Path:
    return run_dir(project_root, run_id) / EVENTS_FILENAME


def academic_provider_requests_path(project_root: Path, run_id: str) -> Path:
    return run_dir(project_root, run_id) / ACADEMIC_PROVIDER_REQUESTS_FILENAME


def run_lock_path(project_root: Path, run_id: str) -> Path:
    return run_dir(project_root, run_id) / ".locks" / "run-state.lock"


def relative_workspace_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def initialize_academic_provider_accounting(project_root: Path, run_id: str) -> dict[str, str]:
    """Create the run-owned provider-call ledger before publishing run state.

    A prior interrupted start may have left the empty ledger behind. Reusing
    that exact empty regular file is safe; truncating any other retained object
    would erase accounting evidence and therefore fails closed.
    """

    path = academic_provider_requests_path(project_root, run_id)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise mutation_write_error(path, "academic_provider_accounting_inspect", exc) from exc
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or int(getattr(metadata, "st_nlink", 1) or 1) != 1
            or metadata.st_size != 0
        ):
            raise RunControllerError(
                "RUN_ACADEMIC_PROVIDER_ACCOUNTING_EXISTS",
                (
                    "Refusing to initialize academic provider accounting over a retained "
                    f"non-empty or non-regular artifact: {relative_workspace_path(project_root, path)}"
                ),
                recoverable=False,
                remediation=(
                    "Preserve the retained artifact for audit, choose a fresh run id, and start a new run. "
                    "Do not truncate or replace provider accounting by hand."
                ),
                details={"run_id": run_id, "artifact": relative_workspace_path(project_root, path)},
            ) from None
    except OSError as exc:
        raise mutation_write_error(path, "academic_provider_accounting_create", exc) from exc

    return {
        "schema_version": ACADEMIC_PROVIDER_ACCOUNTING_SCHEMA_VERSION,
        "ledger_path": relative_workspace_path(project_root, path),
    }


def mutation_write_error(path: Path, boundary: str, error: OSError) -> RunControllerError:
    return RunControllerError(
        "RUN_MUTATION_WRITE_FAILED",
        f"Run mutation failed at {boundary} for {path}: {error}",
        recoverable=True,
        remediation=(
            "Restore write access or free space, retain any generated .tmp artifact, then run "
            "run_controller.py recover for this run before retrying another mutation."
        ),
        details={
            "artifact": path.as_posix(),
            "boundary": boundary,
            "errno": getattr(error, "errno", None),
        },
    )


def write_json(path: Path, document: dict[str, Any]) -> None:
    _durable_run_bytes(path, (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8"), "json")


def _run_file_identity(info) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _run_fsync(descriptor: int, boundary: str) -> None:
    """A real native flush checkpoint; failures are never treated as success."""
    _native_os.fsync(descriptor)


def _replace_run_file(directory: int, temporary: str, name: str) -> None:
    _native_os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)


@contextmanager
def _anchored_run_parent(path: Path, *, create: bool = False):
    """Hold all parent components through the native no-follow selector."""
    path = Path(path).expanduser().absolute()
    if sys.platform == "darwin":
        for prefix in (Path("/var"), Path("/tmp")):  # noqa: S108 - qualify fixed system aliases; never create a temporary here
            if path.is_relative_to(prefix):
                selected = prefix.resolve(strict=True)
                if selected != Path("/private") / prefix.name:
                    raise RunControllerError("RUN_JOURNAL_UNSAFE", "System journal path alias changed.", recoverable=False)
                path = selected / path.relative_to(prefix)
                break
    if ".." in path.parts or len(path.parts) > 64:
        raise RunControllerError("RUN_JOURNAL_UNSAFE", "Run journal path cannot be used.", recoverable=False)
    if (not hasattr(_native_os, "O_NOFOLLOW") or _native_os.open not in _native_os.supports_dir_fd
            or _native_os.rename not in _native_os.supports_dir_fd):
        raise RunControllerError("RUN_JOURNAL_UNSUPPORTED", "Native run journal custody is unavailable.", recoverable=False)
    descriptors, bindings = [], []
    try:
        flags = _native_os.O_RDONLY | _native_os.O_DIRECTORY | _native_os.O_NOFOLLOW
        current = _native_os.open(path.anchor, flags)
        descriptors.append(current)
        for part in path.parts[1:-1]:
            if create:
                try:
                    _native_os.mkdir(part, 0o700, dir_fd=current)
                    _run_fsync(current, "ancestor.directory")
                except FileExistsError:
                    pass
            child = _native_os.open(part, flags, dir_fd=current)
            info = _native_os.fstat(child)
            bindings.append((current, part, info.st_dev, info.st_ino))
            descriptors.append(child)
            current = child
        if create and "runs" in path.parts[:-1]:
            run_index = max(index for index, part in enumerate(path.parts[:-1]) if part == "runs")
            for descriptor in descriptors[max(0, run_index - 1):]:
                _run_fsync(descriptor, "custody.directory")
        def revalidate():
            for parent, part, device, inode in bindings:
                actual = _native_os.stat(part, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != (device, inode):
                    raise RunControllerError("RUN_JOURNAL_CHANGED", "Run journal custody changed.", recoverable=True)
        revalidate()
        yield current, revalidate
        revalidate()
    finally:
        for descriptor in reversed(descriptors):
            _native_os.close(descriptor)


def _run_entry(directory: int, name: str):
    try:
        info = _native_os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RunControllerError("RUN_JOURNAL_UNSAFE", "Run journal requires an ordinary unaliased file.", recoverable=False)
    return info


def _durable_run_bytes(path: Path, raw: bytes, boundary: str) -> None:
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with _anchored_run_parent(path, create=True) as (directory, revalidate):
            before = _run_entry(directory, path.name)
            descriptor = _native_os.open(temporary, _native_os.O_WRONLY | _native_os.O_CREAT | _native_os.O_EXCL
                                         | _native_os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                offset = 0
                while offset < len(raw):
                    written = _native_os.write(descriptor, raw[offset:offset + 65536])
                    if written <= 0:
                        raise OSError("Incomplete run journal write")
                    offset += written
                _run_fsync(descriptor, boundary + ".file")
                published = _run_file_identity(_native_os.fstat(descriptor))
            finally:
                _native_os.close(descriptor)
            revalidate()
            current = _run_entry(directory, path.name)
            if (None if current is None else _run_file_identity(current)) != (None if before is None else _run_file_identity(before)):
                raise RunControllerError("RUN_JOURNAL_CHANGED", "Run journal destination changed.", recoverable=True)
            _replace_run_file(directory, temporary, path.name)
            _run_fsync(directory, boundary + ".directory")
            current = _run_entry(directory, path.name)
            if current is None or _run_file_identity(current)[:6] != published[:6]:
                raise RunControllerError("RUN_JOURNAL_CHANGED", "Published run journal changed.", recoverable=True)
            revalidate()
    except OSError as exc:
        raise mutation_write_error(path, boundary, exc) from exc


def _flush_run_file(path: Path) -> None:
    try:
        with _anchored_run_parent(path) as (directory, revalidate):
            before = _run_entry(directory, path.name)
            if before is None:
                raise RunControllerError("RUN_JOURNAL_CHANGED", "Retained run journal is missing.", recoverable=True)
            descriptor = _native_os.open(path.name, _native_os.O_RDONLY | _native_os.O_NOFOLLOW
                                         | _native_os.O_NONBLOCK, dir_fd=directory)
            try:
                if _run_file_identity(_native_os.fstat(descriptor)) != _run_file_identity(before):
                    raise RunControllerError("RUN_JOURNAL_CHANGED", "Retained run journal changed.", recoverable=True)
                _run_fsync(descriptor, "replay.file")
            finally:
                _native_os.close(descriptor)
            _run_fsync(directory, "replay.directory")
            current = _run_entry(directory, path.name)
            if current is None or _run_file_identity(current) != _run_file_identity(before):
                raise RunControllerError("RUN_JOURNAL_CHANGED", "Retained run journal changed.", recoverable=True)
            revalidate()
    except OSError as exc:
        raise mutation_write_error(path, "replay", exc) from exc


def write_run_state_atomic(path: Path, document: dict[str, Any]) -> None:
    phase = "run_state_pending" if PENDING_EVENT_FIELD in document else "run_state_committed"
    _durable_run_bytes(path, _run_state_bytes(document), phase)


def _run_state_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")


def append_event(path: Path, event: dict[str, Any], *, _journal: dict[str, Any] | None = None) -> bool:
    if _journal is not None:
        current = _bounded_journal(path)
        if current != _journal:
            raise RunControllerError("RUN_JOURNAL_CHANGED", "Run journal changed before publication.", recoverable=True)
        existing = current["events"]
    else:
        existing = load_events(path)
    event_id = event.get("event_id")
    for item in existing:
        if item.get("event_id") != event_id:
            continue
        if item == event:
            _flush_run_file(path)
            return False
        raise RunControllerError(
            "RUN_EVENT_ID_CONFLICT",
            f"Event id {event_id!r} already exists with different content in {path}",
            recoverable=False,
            remediation="Preserve the event log and investigate the conflicting retained records before recovery.",
            details={"artifact": path.as_posix(), "event_id": event_id},
        )
    content = "".join(compact_json(item) + "\n" for item in [*existing, event])
    _durable_run_bytes(path, content.encode("utf-8"), "event_log")
    return True


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunControllerError(
                "RUN_EVENTS_INVALID",
                f"Invalid event JSONL in {path}:{line_number}: {exc}",
                recoverable=False,
                remediation=(
                    "Preserve the corrupt event log, restore a verified retained copy or repair the invalid line, "
                    "then run recover. Do not delete raw evidence or unrelated workspace edits."
                ),
                details={"artifact": path.as_posix(), "line": line_number},
            ) from exc
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            raise RunControllerError(
                "RUN_EVENTS_INVALID",
                f"Invalid event record in {path}:{line_number}: expected an object with event_id",
                recoverable=False,
                remediation="Preserve and repair the event log before running recovery.",
                details={"artifact": path.as_posix(), "line": line_number},
            )
        events.append(event)
    event_ids = [event["event_id"] for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise RunControllerError(
            "RUN_EVENTS_INVALID",
            f"Duplicate event ids in {path}",
            recoverable=False,
            remediation="Preserve and reconcile the duplicate retained events before running recovery.",
            details={"artifact": path.as_posix()},
        )
    return events


def _read_run_bytes(path: Path, *, maximum: int, optional: bool = False) -> bytes:
    try:
        with _anchored_run_parent(path) as (directory, revalidate):
            before = _run_entry(directory, path.name)
            if before is None and optional:
                return b""
            if before is None:
                raise RunControllerError("RUN_JOURNAL_UNREADABLE", "Run journal is missing.", recoverable=False)
            if before.st_size > maximum:
                raise RunControllerError("RUN_JOURNAL_LIMIT", "Run journal exceeds the supported byte bound.", recoverable=False)
            descriptor = _native_os.open(path.name, _native_os.O_RDONLY | _native_os.O_NOFOLLOW
                                         | _native_os.O_NONBLOCK, dir_fd=directory)
            try:
                if _run_file_identity(_native_os.fstat(descriptor)) != _run_file_identity(before):
                    raise RunControllerError("RUN_JOURNAL_CHANGED", "Run journal changed before capture.", recoverable=True)
                chunks, size = [], 0
                while chunk := _native_os.read(descriptor, min(65536, maximum - size + 1)):
                    size += len(chunk)
                    if size > maximum:
                        raise RunControllerError("RUN_JOURNAL_LIMIT", "Run journal exceeds the supported byte bound.", recoverable=False)
                    chunks.append(chunk)
                if _run_file_identity(_native_os.fstat(descriptor)) != _run_file_identity(before):
                    raise RunControllerError("RUN_JOURNAL_CHANGED", "Run journal changed during capture.", recoverable=True)
            finally:
                _native_os.close(descriptor)
            current = _run_entry(directory, path.name)
            if current is None or _run_file_identity(current) != _run_file_identity(before):
                raise RunControllerError("RUN_JOURNAL_CHANGED", "Run journal changed after capture.", recoverable=True)
            revalidate()
            return b"".join(chunks)
    except FileNotFoundError:
        raise RunControllerError("RUN_JOURNAL_UNREADABLE", "Run journal is missing.", recoverable=False) from None
    except OSError as error:
        raise RunControllerError("RUN_JOURNAL_UNREADABLE", "Run journal cannot be captured.", recoverable=False) from error


def _bounded_journal_json(raw: bytes):
    """Reject byte, nesting and token work before invoking the JSON decoder."""
    if len(raw) > JOURNAL_MAX_EVENT_BYTES:
        raise RunControllerError("RUN_JOURNAL_LIMIT", "Run event exceeds the supported byte bound.", recoverable=False)
    depth = tokens = 0
    quoted = escaped = scalar = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
            continue
        if char == 34:
            tokens += 1
            quoted = True
            scalar = False
        elif char in (123, 91):
            tokens += 1
            depth += 1
            scalar = False
        elif char in (125, 93):
            tokens += 1
            depth -= 1
            scalar = False
        elif char in (44, 58):
            tokens += 1
            scalar = False
        elif char in (9, 10, 13, 32):
            scalar = False
        elif not scalar:
            scalar = True
            tokens += 1
        if depth > JOURNAL_MAX_DEPTH or tokens > JOURNAL_MAX_TOKENS:
            raise RunControllerError("RUN_JOURNAL_LIMIT", "Run event exceeds the supported structural bound.", recoverable=False)
    try:
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError("Duplicate run journal key")
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError("Non-finite run journal number")
        def finite_float(value):
            result = float(value)
            if not math.isfinite(result):
                raise ValueError("Non-finite run journal number")
            return result
        def bounded_int(value):
            if len(value.lstrip("-")) > 78:
                raise ValueError("Oversized run journal integer")
            result = int(value)
            if result.bit_length() > 256:
                raise ValueError("Oversized run journal integer")
            return result
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant,
                          parse_float=finite_float, parse_int=bounded_int)
    except (ValueError, UnicodeError, RecursionError):
        raise RunControllerError("RUN_EVENTS_INVALID", "Run journal JSON is invalid.", recoverable=False) from None


def _bounded_journal(path: Path) -> dict[str, Any]:
    raw = _read_run_bytes(path, maximum=JOURNAL_MAX_BYTES, optional=True)
    line_count = raw.count(b"\n") + raw.count(b"\r") - raw.count(b"\r\n")
    if line_count > JOURNAL_MAX_LINES:
        raise RunControllerError("RUN_JOURNAL_LIMIT", "Run journal exceeds the supported line bound.", recoverable=False)
    rows = raw.splitlines()
    nonblank = [row for row in rows if row.strip()]
    if len(nonblank) > JOURNAL_MAX_EVENTS or any(len(row) > JOURNAL_MAX_EVENT_BYTES for row in nonblank):
        raise RunControllerError("RUN_JOURNAL_LIMIT", "Run journal exceeds the supported event bound.", recoverable=False)
    events = [_bounded_journal_json(row) for row in nonblank]
    ids = []
    for event in events:
        if type(event) is not dict or type(event.get("event_id")) is not str:
            raise RunControllerError("RUN_EVENTS_INVALID", "Run journal requires closed event identities.", recoverable=False)
        for field in ("event_type", "run_id", "agent_id"):
            if field in event and type(event[field]) is not str:
                raise RunControllerError("RUN_EVENTS_INVALID", "Run journal outer identity is invalid.", recoverable=False)
        kind = event.get("event_type")
        if kind in {TELEMETRY_EVENT_TYPE, "custom.research.decision", "custom.research.pending"}:
            if (event.get("run_id") != path.parent.name or type(event.get("agent_id")) is not str
                    or type(event.get("data")) is not dict):
                raise RunControllerError("RUN_EVENTS_INVALID", "Recognized run journal wrapper is invalid.", recoverable=False)
            data = event["data"]
            if kind == TELEMETRY_EVENT_TYPE:
                from evidence_wiki.telemetry_contracts import EVENT, validate_document
                try:
                    if set(data) != {"logical_id", "event"}:
                        raise ValueError("Unsupported telemetry wrapper")
                    inner = validate_document(EVENT, data["event"])
                    if (data["logical_id"] != inner["logical_id"] or inner["run_id"] != event["run_id"]
                            or inner["agent_id"] != event["agent_id"]):
                        raise ValueError("Telemetry wrapper binding")
                except (Exception, SystemExit):
                    raise RunControllerError("RUN_EVENTS_INVALID", "Retained telemetry wrapper is invalid.", recoverable=False) from None
            elif (set(data) != {"request_id", "record"} or type(data["request_id"]) is not str
                    or type(data["record"]) is not dict):
                raise RunControllerError("RUN_EVENTS_INVALID", "Retained decision wrapper is invalid.", recoverable=False)
            elif ((kind == "custom.research.pending") != (data["record"].get("kind") == "pending_work")):
                raise RunControllerError("RUN_EVENTS_INVALID", "Retained record has an incompatible request namespace.", recoverable=False)
        ids.append(event["event_id"])
    if len(ids) != len(set(ids)):
        raise RunControllerError("RUN_EVENTS_INVALID", "Run journal contains duplicate event identities.", recoverable=False)
    return {"events": events, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "count": len(events)}


def latest_event_at(project_root: Path, run_id: str) -> str | None:
    latest: datetime | None = None
    for event in load_events(events_path(project_root, run_id)):
        parsed = parse_timestamp(event.get("occurred_at"))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return format_timestamp(latest) if latest is not None else None


def run_liveness_at(project_root: Path, document: dict[str, Any]) -> str | None:
    run_id = document.get("run_id")
    heartbeat = document.get("last_heartbeat_at")
    heartbeat_at = parse_timestamp(heartbeat)
    event_at = latest_event_at(project_root, run_id) if isinstance(run_id, str) else None
    event_parsed = parse_timestamp(event_at)
    updated_at = parse_timestamp(document.get("updated_at"))
    candidates = [candidate for candidate in (heartbeat_at, event_parsed, updated_at) if candidate is not None]
    if not candidates:
        return None
    return format_timestamp(max(candidates))


def run_staleness(
    project_root: Path,
    document: dict[str, Any],
    threshold_hours: float,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = document.get("state", {}).get("current") if isinstance(document.get("state"), dict) else None
    liveness_at = run_liveness_at(project_root, document)
    parsed = parse_timestamp(liveness_at)
    age_hours = None
    stale = False
    if current not in TERMINAL_STATES and parsed is not None:
        evaluated_at = now or datetime.now(timezone.utc)
        if evaluated_at.tzinfo is None:
            evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
        evaluated_at = evaluated_at.astimezone(timezone.utc)
        age_hours = max(0.0, (evaluated_at - parsed).total_seconds() / 3600)
        stale = age_hours >= threshold_hours
    return {
        "liveness_at": liveness_at,
        "stale_age_hours": round(age_hours, 6) if age_hours is not None else None,
        "stale": stale,
    }


def next_event_id(path: Path) -> str:
    return f"evt-{len(load_events(path)) + 1:04d}"


def state_snapshot(current: str, entered_at: str, blocking_reason: str | None = None) -> dict[str, Any]:
    return {
        "current": current,
        "entered_at": entered_at,
        "allowed_next_states": list(STATE_TRANSITIONS[current]),
        "blocking_reason": blocking_reason,
    }


def state_transition_event(
    project_root: Path,
    run_id: str,
    agent_id: str,
    *,
    from_state: str | None,
    to_state: str,
    occurred_at: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": next_event_id(events_path(project_root, run_id)),
        "run_id": run_id,
        "occurred_at": occurred_at,
        "agent_id": agent_id,
        "event_type": "state_transition",
        "from_state": from_state,
        "to_state": to_state,
        "message": message,
        "data": {
            "allowed_next_states": list(STATE_TRANSITIONS[to_state]),
            **(data or {}),
        },
    }


def custom_event(
    project_root: Path,
    run_id: str,
    agent_id: str,
    *,
    event_type: str,
    message: str,
    data: dict[str, Any],
    _event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": next_event_id(events_path(project_root, run_id)) if _event_id is None else _event_id,
        "run_id": run_id,
        "occurred_at": timestamp_utc(),
        "agent_id": agent_id,
        "event_type": event_type,
        "from_state": None,
        "to_state": None,
        "message": message,
        "data": data,
    }


def validate_event_type(event_type: str) -> str:
    normalized = event_type.strip()
    if normalized in EVENT_TYPES or CUSTOM_EVENT_TYPE_RE.match(normalized):
        return normalized
    raise RunControllerError(
        "EVENT_TYPE_INVALID",
        f"event type is not in the shared vocabulary: {event_type}",
        remediation=(
            "Use a documented run-controller event type, or a namespaced custom type "
            "such as custom.operator.note."
        ),
        details={"event_type": event_type, "custom_prefix": "custom."},
    )


def commit_run_mutation(
    project_root: Path,
    run_id: str,
    document: dict[str, Any],
    event: dict[str, Any],
    *,
    _journal: dict[str, Any] | None = None,
) -> None:
    """Commit state plus its audit event with a durable recovery marker.

    The state is first atomically written with ``_pending_event``.  Event-log
    append is itself an atomic replacement and idempotent by event id.  A crash
    anywhere between those writes therefore leaves either the prior good state,
    a complete committed mutation awaiting ``recover``, or a fully consistent
    state/event pair; it never makes an unjournaled state transition acceptable.
    """
    document[PENDING_EVENT_FIELD] = event
    write_run_state_atomic(run_state_path(project_root, run_id), document)
    append_event(events_path(project_root, run_id), event, _journal=_journal)
    document.pop(PENDING_EVENT_FIELD, None)
    write_run_state_atomic(run_state_path(project_root, run_id), document)


def quarantine_run_temp_files(project_root: Path, run_id: str) -> list[str]:
    directory = run_dir(project_root, run_id)
    if not directory.is_dir():
        return []
    reserved_prefixes = (
        f".{RUN_STATE_FILENAME}.",
        f".{EVENTS_FILENAME}.",
        f".{STATUS_BASELINE_FILENAME}.",
        f".{RUN_REPORT_BASELINE_FILENAME}.",
    )
    candidates = [
        path
        for path in sorted(directory.iterdir(), key=lambda item: item.name)
        if path.is_file()
        and path.name.endswith(".tmp")
        and any(path.name.startswith(prefix) for prefix in reserved_prefixes)
    ]
    if not candidates:
        return []
    quarantine = directory / MUTATION_RECOVERY_DIR / "quarantine"
    moved: list[str] = []
    try:
        quarantine.mkdir(parents=True, exist_ok=True)
        for path in candidates:
            destination = quarantine / f"{path.name}.{uuid.uuid4().hex}.interrupted"
            path.replace(destination)
            moved.append(relative_workspace_path(project_root, destination))
    except OSError as exc:
        raise mutation_write_error(quarantine, "temp_artifact_quarantine", exc) from exc
    return moved


def load_run_state(project_root: Path, run_id: str, *, allow_pending: bool = False,
                   _captured_raw: bytes | None = None) -> dict[str, Any]:
    path = run_state_path(project_root, run_id)
    if not path.is_file():
        raise RunControllerError(
            "RUN_UNKNOWN",
            f"unknown run id: {run_id} (no runs/{run_id}/run-state.json)",
            details={"run_id": run_id},
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if _captured_raw is None else _bounded_journal_json(_captured_raw)
    except json.JSONDecodeError as exc:
        raise RunControllerError(
            "RUN_STATE_INVALID",
            f"Invalid run-state JSON in {relative_workspace_path(project_root, path)}: {exc}",
            recoverable=False,
            details={"run_id": run_id},
        ) from exc
    if not isinstance(document, dict):
        raise RunControllerError(
            "RUN_STATE_INVALID",
            f"Invalid run-state document in {relative_workspace_path(project_root, path)}: expected JSON object",
            recoverable=False,
            details={"run_id": run_id},
        )
    state = document.get("state")
    current = state.get("current") if isinstance(state, dict) else None
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("run_id") != run_id
        or current not in STATE_TRANSITIONS
        or not isinstance(document.get("state_history"), list)
    ):
        raise RunControllerError(
            "RUN_STATE_INVALID",
            f"Invalid run-state document shape in {relative_workspace_path(project_root, path)}",
            recoverable=False,
            details={"run_id": run_id},
        )
    if "last_heartbeat_at" not in document:
        document["last_heartbeat_at"] = None
    if not isinstance(document.get("recovery_history"), list):
        document["recovery_history"] = []
    pending = document.get(PENDING_EVENT_FIELD)
    if pending is not None and not allow_pending:
        event_id = pending.get("event_id") if isinstance(pending, dict) else None
        raise RunControllerError(
            "RUN_MUTATION_RECOVERY_REQUIRED",
            f"run {run_id} has an interrupted state/event commit",
            recoverable=True,
            remediation=(
                f"Inspect runs/{run_id}/run-state.json and events.jsonl, then run the recover command "
                "before attempting another mutation."
            ),
            details={"run_id": run_id, "pending_event_id": event_id},
        )
    return document


def status_document(
    project_root: Path,
    *,
    questions_processed_this_run: int | None = None,
    source_requests_opened_this_run: int | None = None,
    releases_this_run: int | None = None,
    discovery_results_this_run: int | None = None,
    acquisition_downloads_this_run: int | None = None,
    github_archive_bytes_this_run: int | None = None,
    academic_provider_requests_this_run: int | None = None,
    web_downloads_this_run: int | None = None,
    manual_url_deliveries_this_run: int | None = None,
) -> dict[str, Any]:
    workspace_status = load_sibling_module("workspace_status")
    return workspace_status.build_status_document(
        project_root,
        questions_processed_this_run=questions_processed_this_run,
        source_requests_opened_this_run=source_requests_opened_this_run,
        releases_this_run=releases_this_run,
        discovery_results_this_run=discovery_results_this_run,
        acquisition_downloads_this_run=acquisition_downloads_this_run,
        github_archive_bytes_this_run=github_archive_bytes_this_run,
        academic_provider_requests_this_run=academic_provider_requests_this_run,
        web_downloads_this_run=web_downloads_this_run,
        manual_url_deliveries_this_run=manual_url_deliveries_this_run,
    )


def source_requests_fulfilled_count(project_root: Path) -> int:
    workspace_status = load_sibling_module("workspace_status")
    source_requests = load_sibling_module("source_requests")
    config = workspace_status.load_yaml_mapping(project_root / "research.yml", "research.yml")
    try:
        records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit:
        return 0
    return sum(1 for record in records if record.get("status") == "fulfilled")


def candidate_counts(project_root: Path) -> dict[str, int]:
    counts = {status: 0 for status in CANDIDATE_STATUSES}
    total = 0
    path = project_root / "sources" / "discovery" / "candidates.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            total += 1
            status = record.get("status")
            key = status if isinstance(status, str) and status in counts else "new"
            counts[key] += 1
    return {"total": total, **counts}


def question_counts_from_status(document: dict[str, Any]) -> dict[str, int]:
    questions = document.get("questions") if isinstance(document.get("questions"), dict) else {}
    by_status = questions.get("by_status") if isinstance(questions.get("by_status"), dict) else {}
    return {
        "total": int(questions.get("total", 0) or 0),
        "open": int(by_status.get("open", 0) or 0),
        "in_progress": int(by_status.get("in_progress", 0) or 0),
        "answered": int(by_status.get("answered", 0) or 0),
        "blocked": int(by_status.get("blocked", 0) or 0),
        "deferred": int(by_status.get("deferred", 0) or 0),
        "rejected": int(by_status.get("rejected", 0) or 0),
        "claimed": int(questions.get("claimed", 0) or 0),
    }


def source_counts_from_status(project_root: Path, document: dict[str, Any]) -> dict[str, int]:
    sources = document.get("sources") if isinstance(document.get("sources"), dict) else {}
    manifest_records = int(sources.get("manifest_records", 0) or 0)
    unnormalized = int(sources.get("unnormalized", 0) or 0)
    return {
        "manifest_records": manifest_records,
        "normalized": max(0, manifest_records - unnormalized),
        "unnormalized": unnormalized,
        "source_requests_open": int(sources.get("requests_open", 0) or 0),
        "source_requests_fulfilled": source_requests_fulfilled_count(project_root),
    }


def budget_state_from_status(document: dict[str, Any]) -> dict[str, Any]:
    readiness = document.get("readiness") if isinstance(document.get("readiness"), dict) else {}
    budget = readiness.get("budget_state") if isinstance(readiness.get("budget_state"), dict) else None
    run = document.get("run") if isinstance(document.get("run"), dict) else {}
    defaults = {
        "questions_processed_this_run": 0,
        "questions_remaining_this_run": int(run.get("max_questions_per_run", 0) or 0),
        "source_requests_opened_this_run": 0,
        "source_requests_remaining_this_run": int(run.get("max_source_requests_per_run", 0) or 0),
        "releases_this_run": 0,
        "releases_remaining_this_run": int(run.get("max_releases_per_run", 0) or 0),
        "discovery_results_this_run": 0,
        "discovery_results_remaining_this_run": int(run.get("max_discovery_results_per_run", 0) or 0),
        "acquisition_downloads_this_run": 0,
        "acquisition_downloads_remaining_this_run": int(run.get("max_acquisition_downloads_per_run", 0) or 0),
        "github_archive_bytes_this_run": 0,
        "github_archive_bytes_remaining_this_run": int(run.get("max_github_archive_bytes_per_run", 0) or 0),
        "academic_provider_requests_this_run": 0,
        "academic_provider_requests_remaining_this_run": int(run.get("max_academic_provider_requests_per_run", 0) or 0),
        "web_downloads_this_run": 0,
        "web_downloads_remaining_this_run": int(run.get("max_web_downloads_per_run", 0) or 0),
        "manual_url_deliveries_this_run": 0,
        "manual_url_deliveries_remaining_this_run": int(run.get("max_manual_url_deliveries_per_run", 0) or 0),
        "stop_reasons": [],
        "should_stop": False,
    }
    if budget is not None:
        merged = dict(defaults)
        for key, value in defaults.items():
            if key == "stop_reasons":
                reasons = budget.get(key)
                merged[key] = [str(reason) for reason in reasons] if isinstance(reasons, list) else []
            elif key == "should_stop":
                merged[key] = bool(budget.get(key))
            else:
                merged[key] = int(budget.get(key, value) or 0)
        for key in ("counter_source", "runner_reported", "counter_divergence"):
            if key in budget:
                merged[key] = budget[key]
        return merged
    return defaults


def configured_manual_url_limit(status: dict[str, Any]) -> int:
    run = status.get("run") if isinstance(status.get("run"), dict) else {}
    return int(run.get("max_manual_url_deliveries_per_run", 0) or 0)


def configured_web_download_limit(status: dict[str, Any]) -> int:
    run = status.get("run") if isinstance(status.get("run"), dict) else {}
    fallback = int(run.get("max_manual_url_deliveries_per_run", 0) or 0)
    return int(run.get("max_web_downloads_per_run", fallback) or fallback)


def manual_url_budget_override(document: dict[str, Any]) -> dict[str, Any] | None:
    overrides = document.get("budget_overrides")
    if not isinstance(overrides, dict):
        return None
    override = overrides.get("manual_url_deliveries")
    return override if isinstance(override, dict) else None


def effective_manual_url_limit(status: dict[str, Any], document: dict[str, Any]) -> int:
    limit = configured_manual_url_limit(status)
    override = manual_url_budget_override(document)
    if override is None:
        return limit
    try:
        return max(limit, int(override.get("new_limit", limit) or limit))
    except (TypeError, ValueError):
        return limit


def effective_web_download_limit(status: dict[str, Any], document: dict[str, Any]) -> int:
    limit = configured_web_download_limit(status)
    override = manual_url_budget_override(document)
    if override is None:
        return limit
    try:
        return max(limit, int(override.get("new_limit", limit) or limit))
    except (TypeError, ValueError):
        return limit


def enforce_manual_url_budget(document: dict[str, Any], status: dict[str, Any], count: int | None) -> None:
    if count is None:
        return
    limit = effective_manual_url_limit(status, document)
    if count <= limit:
        return
    raise RunControllerError(
        "BUDGET_EXCEEDED",
        f"manual URL deliveries {count} exceed configured limit {limit}; record an override first.",
        remediation=(
            "Record an approved override with run_controller.py override-manual-url-budget --run-id RUN_ID "
            "--agent-id AGENT --new-limit N --override-reason TEXT --approved-by WHO, then retry the "
            "transition or finish command."
        ),
        details={
            "budget": "manual_url_deliveries",
            "count": count,
            "limit": limit,
            "override_recorded": manual_url_budget_override(document) is not None,
        },
    )


def enforce_web_download_budget(document: dict[str, Any], status: dict[str, Any], count: int | None) -> None:
    if count is None:
        return
    limit = effective_web_download_limit(status, document)
    if count <= limit:
        return
    raise RunControllerError(
        "BUDGET_EXCEEDED",
        f"web downloads {count} exceed configured limit {limit}; record an override first.",
        remediation=(
            "Record an approved override with run_controller.py override-manual-url-budget --run-id RUN_ID "
            "--agent-id AGENT --new-limit N --override-reason TEXT --approved-by WHO, then retry the "
            "transition or finish command."
        ),
        details={
            "budget": "web_downloads",
            "count": count,
            "limit": limit,
            "override_recorded": manual_url_budget_override(document) is not None,
        },
    )


def refresh_counts(project_root: Path, document: dict[str, Any], status: dict[str, Any]) -> None:
    questions = question_counts_from_status(status)
    document["question_counts"] = questions
    document["source_counts"] = source_counts_from_status(project_root, status)
    document["candidate_counts"] = candidate_counts(project_root)
    document["coverage_counts"] = {
        "required": 0,
        "satisfied": 0,
        "missing": 0,
        "unknown": questions["total"],
    }
    document["budget_state"] = budget_state_from_status(status)
    override = manual_url_budget_override(document)
    if override is not None:
        document["budget_state"]["manual_url_deliveries_override"] = override
        document["budget_state"]["web_downloads_override"] = override


def capture_baseline_artifacts(project_root: Path, run_id: str, status: dict[str, Any]) -> dict[str, Any]:
    directory = run_dir(project_root, run_id)
    status_path = directory / STATUS_BASELINE_FILENAME
    run_report_baseline_path = directory / RUN_REPORT_BASELINE_FILENAME
    write_json(status_path, status)

    run_report = load_sibling_module("run_report")
    baseline = run_report.build_baseline_snapshot(project_root)
    write_json(run_report_baseline_path, baseline)
    return {
        "captured_at": timestamp_utc(),
        "status_path": relative_workspace_path(project_root, status_path),
        "run_report_baseline_path": relative_workspace_path(project_root, run_report_baseline_path),
    }


def assert_transition_allowed(run_id: str, from_state: str, to_state: str) -> None:
    if to_state not in STATE_TRANSITIONS[from_state]:
        raise RunControllerError(
            "RUN_TRANSITION_INVALID",
            f"invalid run transition: {from_state} -> {to_state}",
            details={"run_id": run_id, "from_state": from_state, "to_state": to_state},
        )


def assert_not_terminal(run_id: str, current: str) -> None:
    if current in TERMINAL_STATES:
        raise RunControllerError(
            "RUN_TERMINAL",
            f"run {run_id} is already terminal: {current}",
            recoverable=False,
            details={"run_id": run_id, "state": current},
        )


def caller_observation(project_root, operation):
    try:
        return getattr(load_sibling_module("_caller_context"), operation)(project_root)
    except (Exception, SystemExit):
        raise RunControllerError("RUN_CALLER_CONTEXT_CONFLICT", "Current caller controls or release are unavailable.",
            exit_code=3, recoverable=False, details={"reason":"caller_controls_or_release_unavailable"},
            remediation="Inspect current instructions, policy, evidence and independent review; preserve the run until its owner can proceed.") from None


def require_caller_context(project_root, document, agent_id, *, transfer=False, retiring=False):
    if retiring:
        return
    if not retiring and document.get("caller_context") is None:
        revision = load_sibling_module("_pack_revision_guard")
        if document.get("requirement_basis") is not None or (Path(project_root) / "domain-packs/.evidence-wiki-state.yml").exists():
            revision.require(document.get("requirement_basis") == revision.controls(project_root), "run_requirements_changed_or_unbound")
    if document.get("caller_context") is None:
        return
    try:
        load_sibling_module("_caller_context").validate(project_root, document, agent_id=agent_id, transfer=transfer)
    except (Exception, SystemExit) as error:
        reason = str(error) if type(error) is ValueError and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", str(error)) else "caller_controls_unavailable"
        raise RunControllerError("RUN_CALLER_CONTEXT_CONFLICT", "Caller run controls or ownership changed.",
            exit_code=3, recoverable=False, details={"run_id": document.get("run_id"), "reason": reason},
            remediation="Preserve the run, inspect current controls/ownership, and use explicit stale adoption or a new run.") from None


def run_start(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    guard = load_sibling_module("_pack_revision_guard")
    requirements = guard.controls(project_root)
    with workspace_lock(project_root / ".locks/domain-pack-refresh.lock", purpose="run requirement binding"):
        guard.require(guard.controls(project_root) == requirements, "run_requirements_changed")
        return start_with_requirements(project_root, args, requirements)


def start_with_requirements(project_root, args, requirement_basis):
    agent_id = require_agent_id(args.agent_id)
    run_id = validate_run_id(args.run_id, allow_generate=True)
    caller = caller_observation(project_root, "capture") if getattr(args, "caller", False) else None
    existed = run_dir(project_root, run_id).exists()
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        directory = run_dir(project_root, run_id)
        if run_state_path(project_root, run_id).exists() or caller is not None and existed:
            raise RunControllerError("RUN_EXISTS", f"run already exists: {run_id}", details={"run_id": run_id})
        directory.mkdir(parents=True, exist_ok=True)
        quarantine_run_temp_files(project_root, run_id)

        now = timestamp_utc()
        status = status_document(project_root)
        baseline = capture_baseline_artifacts(project_root, run_id, status)
        academic_provider_accounting = initialize_academic_provider_accounting(project_root, run_id)
        project = status.get("project") if isinstance(status.get("project"), dict) else {}
        handoff = project.get("handoff") if isinstance(project.get("handoff"), dict) else None
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "requirement_basis": requirement_basis,
            "run_id": run_id,
            "started_at": now,
            "updated_at": now,
            "last_heartbeat_at": None,
            "agent_id": agent_id,
            "handoff": handoff,
            "state": state_snapshot("initialized", now),
            "state_history": [
                {
                    "from_state": None,
                    "to_state": "initialized",
                    "changed_at": now,
                    "agent_id": agent_id,
                    "reason": "Run state artifact created.",
                }
            ],
            "workspace_baseline": baseline,
            ACADEMIC_PROVIDER_ACCOUNTING_FIELD: academic_provider_accounting,
            "question_counts": {},
            "source_counts": {},
            "candidate_counts": {},
            "coverage_counts": {},
            "budget_state": {},
            "budget_overrides": {},
            "failure_records": [],
            "recovery_history": [],
            "final_verdict": None,
        }
        if caller is not None:
            document["caller_context"] = caller
            require_caller_context(project_root, document, agent_id)
        if getattr(args, "session_binding", None) is not None:
            document["research_session"] = json.loads(compact_json(args.session_binding))
        refresh_counts(project_root, document, status)
        event = state_transition_event(
            project_root,
            run_id,
            agent_id,
            from_state=None,
            to_state="initialized",
            occurred_at=now,
            message="Run state artifact created.",
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def run_session_start(project_root: Path, *, run_id: str, agent_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    """Bind an explicit caller session to this canonical run, with exact replay."""
    project_root = _session_host(project_root, "run_session_start", agent_id=agent_id)
    run_id = validate_run_id(run_id)
    agent_id = require_agent_id(agent_id)
    binding = json.loads(compact_json(binding))
    path = run_state_path(project_root, run_id)
    if path.exists():
        with workspace_lock(run_lock_path(project_root, run_id), purpose="research session replay"):
            document = _session_state(project_root, run_id)
            require_caller_context(project_root, document, agent_id)
            if document.get("research_session") != binding or document["agent_id"] != agent_id:
                raise RunControllerError("RUN_CALLER_CONTEXT_CONFLICT", "Session binding differs from the retained run.", recoverable=False)
            _flush_run_file(events_path(project_root, run_id))
            _flush_run_file(run_state_path(project_root, run_id))
            return document
    return run_start(project_root, SimpleNamespace(run_id=run_id, agent_id=agent_id, caller=True, session_binding=binding))


def run_session_record(project_root: Path, *, run_id: str, agent_id: str, request_id: str,
                       binding: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Retain one inert decision/job/receipt record in the existing run event log."""
    project_root = _session_host(project_root, "run_session_record", agent_id=agent_id)
    run_id, agent_id = validate_run_id(run_id), require_agent_id(agent_id)
    if type(request_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", request_id) is None:
        raise RunControllerError("VALUE_INVALID", "Session request ID is invalid.", recoverable=True)
    encoded = compact_json(record)
    if len(encoded.encode()) > 262144 or type(record) is not dict:
        raise RunControllerError("VALUE_INVALID", "Session record is invalid or too large.", recoverable=True)
    with workspace_lock(run_lock_path(project_root, run_id), purpose="research decision commit"):
        document = _session_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id)
        if document.get("research_session") != binding:
            raise RunControllerError("RUN_CALLER_CONTEXT_CONFLICT", "Session binding differs from the retained run.", recoverable=False)
        journal = _bounded_journal(events_path(project_root, run_id))
        event_type = "custom.research.pending" if record.get("kind") == "pending_work" else "custom.research.decision"
        for event in journal["events"]:
            data = event.get("data") or {}
            if event["event_type"] == event_type and data.get("request_id") == request_id:
                if data.get("record") != record:
                    raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Session request ID has a different retained record.", recoverable=False)
                _flush_run_file(events_path(project_root, run_id))
                _flush_run_file(run_state_path(project_root, run_id))
                return event
        if record.get("kind") == "host_job":
            from datetime import timedelta
            from decimal import Decimal

            from evidence_wiki.workflow_contracts import JOB, validate_document
            job = validate_document(JOB, record.get("job"))
            if (job["id"] != request_id or job["policy_id"] != binding.get("policy_id")
                    or job["input_id"] != binding.get("request_binding") or not set(job["scope"]) <= set(binding.get("original_ids", []))):
                raise RunControllerError("RUN_CALLER_CONTEXT_CONFLICT", "Job reservation differs from the retained session.", recoverable=False)
            if any((row.get("data") or {}).get("record", {}).get("kind") == "host_job"
                   for row in journal["events"] if row.get("event_type") == "custom.research.decision"):
                raise RunControllerError("RUN_JOB_RESERVATION_PENDING", "Run budget already has a retained job reservation.", recoverable=False)
            policy = binding["policy"]
            started = datetime.fromisoformat(document["started_at"].replace("Z", "+00:00"))
            updated = datetime.fromisoformat(document["updated_at"].replace("Z", "+00:00"))
            issued = datetime.fromisoformat(job["issued_utc"].replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(job["deadline_utc"].replace("Z", "+00:00"))
            observed = datetime.now(timezone.utc)
            effect = {"generate": "model", "evaluate": "adjudicate"}.get(job["operation"])
            bridge = sys.modules.get("evidence_wiki.host_context")
            context = bridge.current_context() if bridge is not None else None
            role = "evaluator" if job["operation"] == "evaluate" else "host_worker"
            if (effect is None or effect not in policy["allowed_effects"]
                    or job["grant_ref"] != policy["host_capability_refs"].get(effect)
                    or job["required_capabilities"] != [effect] or job["role"] != role
                    or job["max_attempts"] != 1 or job["budgets"]["model_calls"] < 1 or job["budgets"]["tokens"] < 1
                    or context is not None and dict(context.controller_refs).get(role) != job["controller_ref"]
                    or not max(started, updated) <= issued <= observed < deadline
                    or deadline > started + timedelta(seconds=policy["budgets"]["seconds"])
                    or any(job["budgets"][key] > policy["budgets"][key] for key in ("model_calls", "tool_calls", "tokens"))
                    or Decimal(job["budgets"]["cost"]) > Decimal(policy["budgets"]["cost_limit"])):
                raise RunControllerError("RUN_JOB_RESERVATION_INVALID", "Job reservation exceeds its frozen run policy or lease.", recoverable=False)
            if document["state"]["current"] in TERMINAL_STATES:
                raise RunControllerError("RUN_TERMINAL", "Terminal runs cannot reserve new work.", recoverable=False)
        elif record.get("kind") == "pending_work":
            from datetime import timedelta

            from evidence_wiki.mutation_contracts import digest
            from evidence_wiki.pending_work_contracts import PENDING, create, validate_document, validate_transition
            receipt = validate_document(PENDING, record.get("receipt"))
            previous = None
            for row in journal["events"]:
                prior = (row.get("data") or {}).get("record", {})
                if row.get("event_type") not in {"custom.research.decision", "custom.research.pending"}:
                    continue
                if prior.get("kind") == "host_job":
                    job = prior["job"]
                    try:
                        deadline = datetime.fromisoformat(document["started_at"].replace("Z", "+00:00")) + timedelta(seconds=binding["policy"]["budgets"]["seconds"])
                    except OverflowError:
                        raise RunControllerError("RUN_STATE_INVALID", "Run session deadline is invalid.", recoverable=False) from None
                    previous = create(job, run_id=run_id, binding_id=digest(binding), request_id=job["id"],
                        run_deadline_utc=deadline.isoformat(), observed_utc=job["issued_utc"])
                elif prior.get("kind") == "pending_work":
                    if previous is None:
                        raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Pending work has no reservation.", recoverable=False)
                    previous = validate_transition(previous, prior["receipt"])
            if previous is None or record.get("operation") not in {"cancel", "reconcile"}:
                raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Pending work differs from its immutable reservation.", recoverable=False)
            validate_transition(previous, receipt)
        event = custom_event(project_root, run_id, agent_id, event_type=event_type,
                             message="Inert research decision recorded.", data={"request_id": request_id, "record": record},
                             _event_id=f"evt-{journal['count'] + 1:04d}")
        if any(row["event_id"] == event["event_id"] for row in journal["events"]):
            raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Run event identity cannot be assigned safely.", recoverable=False)
        projected = "".join(compact_json(row) + "\n" for row in [*journal["events"], event]).encode("utf-8")
        if (journal["count"] >= JOURNAL_MAX_EVENTS or len(projected) > JOURNAL_MAX_BYTES
                or len(compact_json(event).encode("utf-8")) > JOURNAL_MAX_EVENT_BYTES
                or len(_run_state_bytes({**document, PENDING_EVENT_FIELD: event})) > JOURNAL_MAX_EVENT_BYTES):
            raise RunControllerError("RUN_JOURNAL_LIMIT", "Research append exceeds the supported journal bound.", recoverable=False)
        document["updated_at"] = event["occurred_at"]
        commit_run_mutation(project_root, run_id, document, event, _journal=journal)
        return event


def _session_state(project_root: Path, run_id: str):
    document = load_run_state(project_root, run_id, _captured_raw=_read_run_bytes(
        run_state_path(project_root, run_id), maximum=JOURNAL_MAX_EVENT_BYTES))
    from evidence_wiki.workflow_contracts import WorkflowContractError, _utc
    for field in ("started_at", "updated_at"):
        try:
            _utc(document.get(field), "/" + field)
        except WorkflowContractError:
            raise RunControllerError("RUN_STATE_INVALID", "Run session clock is invalid.", recoverable=False,
                                     details={"field": field}) from None
    return document


def _session_host(project_root: Path, seam: str, *, agent_id: str):
    bridge = sys.modules.get("evidence_wiki.host_context")
    context = bridge.current_context() if bridge is not None else None
    root = Path(project_root).expanduser().absolute()
    if context is not None:
        context.require_grant("run_controller." + seam)
        root = context.require_path(root, write=True)
        bridge.require_actor(agent_id)
        bridge._require_mutation_workspace_gate(root)
    return root


def _telemetry_host(project_root: Path, seam: str, *, agent_id: str | None = None):
    bridge = sys.modules.get("evidence_wiki.host_context")
    context = bridge.current_context() if bridge is not None else None
    root = Path(project_root).expanduser().absolute()
    if context is not None:
        context.require_grant("run_controller." + seam)
        root = context.require_path(root, write=seam == "run_telemetry_record")
        if agent_id is not None:
            bridge.require_actor(agent_id)
        if seam == "run_telemetry_record" and context.mutation_control_directory is not None:
            bridge._require_mutation_workspace_gate(root)
    return root


def run_telemetry_events(project_root: Path, *, run_id: str) -> dict[str, Any]:
    """Capture bounded journal bytes; retained rows are editable artifact facts."""
    root = _telemetry_host(project_root, "run_telemetry_events")
    run_id = validate_run_id(run_id)
    state_path = run_state_path(root, run_id)
    before = _read_run_bytes(state_path, maximum=JOURNAL_MAX_EVENT_BYTES)
    load_run_state(root, run_id, _captured_raw=before)
    journal = _bounded_journal(events_path(root, run_id))
    if _read_run_bytes(state_path, maximum=JOURNAL_MAX_EVENT_BYTES) != before:
        raise RunControllerError("RUN_JOURNAL_CHANGED", "Run state changed during journal capture.", recoverable=True)
    return {"schema_version": "research-telemetry-journal/v1", "run_id": run_id, **journal,
        "limits": {"max_bytes": JOURNAL_MAX_BYTES, "max_event_bytes": JOURNAL_MAX_EVENT_BYTES,
            "max_events": JOURNAL_MAX_EVENTS, "max_lines": JOURNAL_MAX_LINES, "max_depth": JOURNAL_MAX_DEPTH},
        "authorized": False, "evidence_accepted": False}


def run_telemetry_record(project_root: Path, *, run_id: str, agent_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Retain a closed telemetry event with logical replay and native durability."""
    root = _telemetry_host(project_root, "run_telemetry_record", agent_id=agent_id)
    run_id, agent_id = validate_run_id(run_id), require_agent_id(agent_id)
    from evidence_wiki.source_commands import _no_secret_values
    from evidence_wiki.telemetry_contracts import EVENT, metric_binding, validate_document
    event = validate_document(EVENT, event)
    _no_secret_values(event)
    if event["run_id"] != run_id or event["agent_id"] != agent_id:
        raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Telemetry event binding differs from the selected run.", recoverable=False)
    with workspace_lock(run_lock_path(root, run_id), purpose="telemetry event commit"):
        raw = _read_run_bytes(run_state_path(root, run_id), maximum=JOURNAL_MAX_EVENT_BYTES)
        document = load_run_state(root, run_id, _captured_raw=raw)
        journal = _bounded_journal(events_path(root, run_id))
        metrics = {}
        for retained in journal["events"]:
            if retained.get("event_type") != TELEMETRY_EVENT_TYPE:
                continue
            data = retained.get("data") or {}
            if data.get("logical_id") == event["logical_id"]:
                if data.get("event") != event or retained.get("agent_id") != agent_id:
                    raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Telemetry logical ID has different retained content.", recoverable=False)
                _flush_run_file(events_path(root, run_id))
                _flush_run_file(run_state_path(root, run_id))
                return retained
            previous = validate_document(EVENT, data["event"])
            for metric in previous["metrics"]:
                binding = metric_binding(previous, metric)
                if metric["logical_id"] in metrics and metrics[metric["logical_id"]] != binding:
                    raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Retained metric logical ID has conflicting content.", recoverable=False)
                metrics[metric["logical_id"]] = binding
        for metric in event["metrics"]:
            if metric["logical_id"] in metrics and metrics[metric["logical_id"]] != metric_binding(event, metric):
                raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Metric logical ID has different retained execution content.", recoverable=False)
        require_caller_context(root, document, agent_id)
        retained = custom_event(root, run_id, agent_id, event_type=TELEMETRY_EVENT_TYPE,
            message="Inert telemetry observation recorded.", data={"logical_id": event["logical_id"], "event": event},
            _event_id=f"evt-{journal['count'] + 1:04d}")
        if any(row["event_id"] == retained["event_id"] for row in journal["events"]):
            raise RunControllerError("RUN_EVENT_ID_CONFLICT", "Run event identity cannot be assigned safely.", recoverable=False)
        projected = "".join(compact_json(row) + "\n" for row in [*journal["events"], retained]).encode("utf-8")
        if (journal["count"] >= JOURNAL_MAX_EVENTS or len(projected) > JOURNAL_MAX_BYTES
                or len(compact_json(retained).encode("utf-8")) > JOURNAL_MAX_EVENT_BYTES):
            raise RunControllerError("RUN_JOURNAL_LIMIT", "Telemetry append exceeds the supported journal bound.", recoverable=False)
        document["updated_at"] = retained["occurred_at"]
        pending_size = len(_run_state_bytes({**document, PENDING_EVENT_FIELD: retained}))
        if pending_size > JOURNAL_MAX_EVENT_BYTES:
            raise RunControllerError("RUN_JOURNAL_LIMIT", "Telemetry checkpoint exceeds the supported state bound.", recoverable=False)
        commit_run_mutation(root, run_id, document, retained, _journal=journal)
        return retained


def run_session_transition(project_root: Path, *, run_id: str, agent_id: str, to_state: str,
                           reason: str) -> dict[str, Any]:
    """Use the canonical transition engine without invoking an argument parser."""
    project_root = _session_host(project_root, "run_session_transition", agent_id=agent_id)
    args = SimpleNamespace(run_id=run_id, agent_id=agent_id, to_state=to_state, reason=reason)
    for name in ("questions_processed_this_run", "source_requests_opened_this_run", "releases_this_run",
                 "discovery_results_this_run", "acquisition_downloads_this_run", "github_archive_bytes_this_run",
                 "academic_provider_requests_this_run", "web_downloads_this_run", "manual_url_deliveries_this_run"):
        setattr(args, name, None)
    return run_transition(project_root, args)


def run_session_finish(project_root: Path, *, run_id: str, agent_id: str, final_verdict: str,
                       reason: str) -> dict[str, Any]:
    """Use current canonical completion/readiness checks for the terminal state."""
    project_root = _session_host(project_root, "run_session_finish", agent_id=agent_id)
    args = SimpleNamespace(run_id=run_id, agent_id=agent_id, final_verdict=final_verdict, reason=reason)
    for name in ("questions_processed_this_run", "source_requests_opened_this_run", "releases_this_run",
                 "discovery_results_this_run", "acquisition_downloads_this_run", "github_archive_bytes_this_run",
                 "academic_provider_requests_this_run", "web_downloads_this_run", "manual_url_deliveries_this_run"):
        setattr(args, name, None)
    return run_finish(project_root, args)


def run_transition(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    to_state = args.to_state
    if to_state is None:
        raise RunControllerError("RUN_TRANSITION_INVALID", "--to-state is required")
    if to_state in TERMINAL_STATES:
        raise RunControllerError(
            "RUN_TRANSITION_INVALID",
            "use finish to enter terminal run states",
            details={"run_id": run_id, "from_state": None, "to_state": to_state},
        )
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        document = load_run_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id)
        current = document["state"]["current"]
        assert_not_terminal(run_id, current)
        assert_transition_allowed(run_id, current, to_state)

        now = timestamp_utc()
        reason = (
            args.reason.strip()
            if isinstance(args.reason, str) and args.reason.strip()
            else f"Transitioned to {to_state}."
        )
        status = status_document(
            project_root,
            questions_processed_this_run=args.questions_processed_this_run,
            source_requests_opened_this_run=args.source_requests_opened_this_run,
            releases_this_run=args.releases_this_run,
            discovery_results_this_run=args.discovery_results_this_run,
            acquisition_downloads_this_run=args.acquisition_downloads_this_run,
            github_archive_bytes_this_run=args.github_archive_bytes_this_run,
            academic_provider_requests_this_run=args.academic_provider_requests_this_run,
            web_downloads_this_run=args.web_downloads_this_run,
            manual_url_deliveries_this_run=args.manual_url_deliveries_this_run,
        )
        enforce_web_download_budget(document, status, args.web_downloads_this_run)
        enforce_manual_url_budget(document, status, args.manual_url_deliveries_this_run)
        document["updated_at"] = now
        document["agent_id"] = agent_id
        document["state"] = state_snapshot(to_state, now)
        document["state_history"].append(
            {
                "from_state": current,
                "to_state": to_state,
                "changed_at": now,
                "agent_id": agent_id,
                "reason": reason,
            }
        )
        refresh_counts(project_root, document, status)
        event = state_transition_event(
            project_root,
            run_id,
            agent_id,
            from_state=current,
            to_state=to_state,
            occurred_at=now,
            message=reason,
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def run_status(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    return load_run_state(project_root, run_id)


def parse_data_json(value: str | None) -> dict[str, Any]:
    if value is None or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RunControllerError("EVENT_DATA_INVALID", f"--data-json must be a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RunControllerError("EVENT_DATA_INVALID", "--data-json must be a JSON object")
    return parsed


def run_event(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    event_type = args.event_type.strip() if isinstance(args.event_type, str) else ""
    message = args.message.strip() if isinstance(args.message, str) else ""
    if not event_type:
        raise RunControllerError("VALUE_INVALID", "--event-type must be a non-empty string")
    event_type = validate_event_type(event_type)
    if not message:
        raise RunControllerError("VALUE_INVALID", "--message must be a non-empty string")
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        document = load_run_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id)
        event = custom_event(
            project_root,
            run_id,
            agent_id,
            event_type=event_type,
            message=message,
            data=parse_data_json(args.data_json),
        )
        document["updated_at"] = event["occurred_at"]
        document["agent_id"] = agent_id
        commit_run_mutation(project_root, run_id, document, event)
        return event


def require_stale_threshold(value: float | None, *, command: str) -> float:
    if value is None:
        error_code = "RUN_ADOPT_THRESHOLD_REQUIRED" if command == "adopt" else "RUN_ABANDON_THRESHOLD_REQUIRED"
        raise RunControllerError(
            error_code,
            f"{command} requires --if-stale-hours so active-run recovery is explicit.",
            remediation="Pass --if-stale-hours HOURS after inspecting runs/<run_id>/events.jsonl.",
        )
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise RunControllerError("VALUE_INVALID", "Stale recovery requires a finite positive threshold.")
    return float(value)


def stale_or_refuse(project_root: Path, run_id: str, document: dict[str, Any], threshold_hours: float) -> dict[str, Any]:
    status = run_staleness(project_root, document, threshold_hours)
    if status["stale"]:
        return status
    raise RunControllerError(
        "RUN_NOT_STALE",
        f"run {run_id} is not stale enough for recovery",
        recoverable=True,
        remediation="Wait for the run to exceed the threshold or use a larger --if-stale-hours value.",
        details={"run_id": run_id, "if_stale_hours": threshold_hours, **status},
    )


def recovery_record(
    *,
    action: str,
    run_id: str,
    previous_agent_id: Any,
    agent_id: str,
    threshold_hours: float,
    staleness: dict[str, Any],
    reason: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "action": action,
        "run_id": run_id,
        "previous_agent_id": previous_agent_id if isinstance(previous_agent_id, str) else None,
        "agent_id": agent_id,
        "recorded_at": timestamp_utc(),
        "if_stale_hours": float(threshold_hours),
        "stale_since": staleness.get("liveness_at"),
        "stale_age_hours": staleness.get("stale_age_hours"),
    }
    if reason is not None:
        record["reason"] = reason
    return record


def run_recover(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id} recovery"):
        document = load_run_state(project_root, run_id, allow_pending=True)
        require_caller_context(project_root, document, agent_id)
        quarantined = quarantine_run_temp_files(project_root, run_id)
        pending = document.get(PENDING_EVENT_FIELD)
        if pending is None and not quarantined:
            _flush_run_file(run_state_path(project_root, run_id))
            _flush_run_file(events_path(project_root, run_id))
            return document
        if pending is not None and (
            not isinstance(pending, dict) or not isinstance(pending.get("event_id"), str)
        ):
            raise RunControllerError(
                "RUN_PENDING_EVENT_INVALID",
                f"run {run_id} has an invalid pending event journal",
                recoverable=False,
                remediation="Preserve run-state.json and restore a verified copy before retrying recovery.",
                details={"run_id": run_id},
            )

        if isinstance(pending, dict):
            append_event(events_path(project_root, run_id), pending)
            if pending.get("event_type") == "mutation_recovered":
                document.pop(PENDING_EVENT_FIELD, None)
                write_run_state_atomic(run_state_path(project_root, run_id), document)
                return document

        recovered_event_id = pending.get("event_id") if isinstance(pending, dict) else None
        recorded_at = timestamp_utc()
        recovery = {
            "action": "mutation_recover",
            "run_id": run_id,
            "agent_id": agent_id,
            "previous_agent_id": document.get("agent_id"),
            "recorded_at": recorded_at,
            "fault_code": "interrupted_state_event_commit" if pending is not None else "interrupted_temp_write",
            "recovered_event_id": recovered_event_id,
            "quarantined_artifacts": quarantined,
            "ownership_changed": False,
        }
        recovery_id = f"mutation-recover:{recovered_event_id or 'temp'}:{','.join(quarantined)}"
        existing = document.setdefault("recovery_history", [])
        if not any(item.get("recovery_id") == recovery_id for item in existing if isinstance(item, dict)):
            recovery["recovery_id"] = recovery_id
            existing.append(recovery)
        document.pop(PENDING_EVENT_FIELD, None)
        event = custom_event(
            project_root,
            run_id,
            agent_id,
            event_type="mutation_recovered",
            message="Interrupted run mutation recovered without changing ownership.",
            data=recovery,
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def run_heartbeat(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        document = load_run_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id)
        current = document["state"]["current"]
        assert_not_terminal(run_id, current)
        now = timestamp_utc()
        document["updated_at"] = now
        document["last_heartbeat_at"] = now
        document["agent_id"] = agent_id
        event = custom_event(
            project_root,
            run_id,
            agent_id,
            event_type="heartbeat",
            message="Run heartbeat recorded.",
            data={"state": current},
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def run_adopt(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    threshold_hours = require_stale_threshold(args.if_stale_hours, command="adopt")
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        document = load_run_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id, transfer=True)
        current = document["state"]["current"]
        assert_not_terminal(run_id, current)
        staleness = stale_or_refuse(project_root, run_id, document, threshold_hours)
        previous_agent_id = document.get("agent_id")
        now = timestamp_utc()
        document["updated_at"] = now
        document["last_heartbeat_at"] = now
        document["agent_id"] = agent_id
        recovery = recovery_record(
            action="adopt",
            run_id=run_id,
            previous_agent_id=previous_agent_id,
            agent_id=agent_id,
            threshold_hours=threshold_hours,
            staleness=staleness,
        )
        document.setdefault("recovery_history", []).append(recovery)
        event = custom_event(
            project_root,
            run_id,
            agent_id,
            event_type="run_adopted",
            message=f"Run adopted from {previous_agent_id or 'unknown agent'}.",
            data=recovery,
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def run_abandon(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    threshold_hours = require_stale_threshold(args.if_stale_hours, command="abandon")
    reason = (
        args.reason.strip()
        if isinstance(args.reason, str) and args.reason.strip()
        else "Stale active run abandoned for recovery."
    )
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        document = load_run_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id, transfer=True, retiring=True)
        current = document["state"]["current"]
        assert_not_terminal(run_id, current)
        staleness = stale_or_refuse(project_root, run_id, document, threshold_hours)
        previous_agent_id = document.get("agent_id")
        now = timestamp_utc()
        document["updated_at"] = now
        document["agent_id"] = agent_id
        document["state"] = state_snapshot("failed", now, reason)
        document["state_history"].append(
            {
                "from_state": current,
                "to_state": "failed",
                "changed_at": now,
                "agent_id": agent_id,
                "reason": reason,
            }
        )
        document["final_verdict"] = "failed"
        failure = {
            "recorded_at": now,
            "agent_id": agent_id,
            "state": current,
            "reason": reason,
            "failure_code": "stale_run_abandoned",
            "machine_reason": "stale_run_abandoned",
        }
        document.setdefault("failure_records", []).append(failure)
        recovery = recovery_record(
            action="abandon",
            run_id=run_id,
            previous_agent_id=previous_agent_id,
            agent_id=agent_id,
            threshold_hours=threshold_hours,
            staleness=staleness,
            reason=reason,
        )
        document.setdefault("recovery_history", []).append(recovery)
        status = status_document(project_root)
        refresh_counts(project_root, document, status)
        event = custom_event(
            project_root,
            run_id,
            agent_id,
            event_type="run_abandoned",
            message=reason,
            data={"recovery": recovery, "failure": failure},
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def run_override_manual_url_budget(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    reason = args.override_reason.strip() if isinstance(args.override_reason, str) else ""
    approved_by = args.approved_by.strip() if isinstance(args.approved_by, str) else ""
    if not reason:
        raise RunControllerError("VALUE_INVALID", "--override-reason must be a non-empty string")
    if not approved_by:
        raise RunControllerError("VALUE_INVALID", "--approved-by must be a non-empty string")
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        document = load_run_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id)
        current = document["state"]["current"]
        assert_not_terminal(run_id, current)
        baseline_status = status_document(project_root)
        previous_limit = effective_manual_url_limit(baseline_status, document)
        new_limit = int(args.new_limit)
        if new_limit <= previous_limit:
            raise RunControllerError(
                "BUDGET_OVERRIDE_INVALID",
                f"--new-limit must be greater than the current manual URL delivery limit ({previous_limit}).",
                details={"run_id": run_id, "previous_limit": previous_limit, "new_limit": new_limit},
            )

        recorded_at = timestamp_utc()
        override = {
            "budget": "manual_url_deliveries",
            "previous_limit": previous_limit,
            "new_limit": new_limit,
            "override_reason": reason,
            "approved_by": approved_by,
            "recorded_at": recorded_at,
            "agent_id": agent_id,
        }
        overrides = document.get("budget_overrides") if isinstance(document.get("budget_overrides"), dict) else {}
        overrides["manual_url_deliveries"] = override
        document["budget_overrides"] = overrides
        document["updated_at"] = recorded_at
        document["agent_id"] = agent_id
        refresh_counts(project_root, document, baseline_status)
        event = custom_event(
            project_root,
            run_id,
            agent_id,
            event_type="budget_override",
            message=f"Manual URL delivery budget override approved by {approved_by}.",
            data=override,
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def completion_readiness_findings(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw_findings = document.get("verdict_reasons")
    if not isinstance(raw_findings, list):
        return []
    allowed_fields = ("code", "category", "severity", "message", "artifacts", "remediation")
    findings: list[dict[str, Any]] = []
    for raw in raw_findings:
        if not isinstance(raw, dict):
            continue
        item = {field: raw[field] for field in allowed_fields if field in raw}
        if item:
            findings.append(item)
        if len(findings) >= 50:
            break
    return findings


def evaluate_completion_readiness(project_root: Path, run_id: str) -> dict[str, Any]:
    publication_readiness = load_sibling_module("publication_readiness")
    try:
        document = publication_readiness.build_readiness_document(project_root)
    except SystemExit as exc:
        raise RunControllerError(
            "RUN_COMPLETION_READINESS_UNREADABLE",
            f"fresh publication readiness could not be evaluated for run {run_id}: {exc}",
            remediation="Repair the workspace health/readiness findings, then retry finish.",
            details={"run_id": run_id},
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("verdict"), str):
        raise RunControllerError(
            "RUN_COMPLETION_READINESS_INVALID",
            f"fresh publication readiness returned an invalid document for run {run_id}",
            remediation="Repair the publication-readiness evaluator before retrying finish.",
            details={"run_id": run_id},
        )
    return document


def run_finish(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id)
    agent_id = require_agent_id(args.agent_id)
    final_verdict = args.final_verdict
    if final_verdict is None:
        raise RunControllerError("FINAL_VERDICT_REQUIRED", "--final-verdict is required for finish")
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        document = load_run_state(project_root, run_id)
        require_caller_context(project_root, document, agent_id)
        current = document["state"]["current"]
        assert_not_terminal(run_id, current)
        assert_transition_allowed(run_id, current, final_verdict)

        now = timestamp_utc()
        reason = (
            args.reason.strip()
            if isinstance(args.reason, str) and args.reason.strip()
            else f"Finished run as {final_verdict}."
        )
        blocking_reason = None if final_verdict == "complete" else reason
        status = status_document(
            project_root,
            questions_processed_this_run=args.questions_processed_this_run,
            source_requests_opened_this_run=args.source_requests_opened_this_run,
            releases_this_run=args.releases_this_run,
            discovery_results_this_run=args.discovery_results_this_run,
            acquisition_downloads_this_run=args.acquisition_downloads_this_run,
            github_archive_bytes_this_run=args.github_archive_bytes_this_run,
            academic_provider_requests_this_run=args.academic_provider_requests_this_run,
            web_downloads_this_run=args.web_downloads_this_run,
            manual_url_deliveries_this_run=args.manual_url_deliveries_this_run,
        )
        enforce_web_download_budget(document, status, args.web_downloads_this_run)
        enforce_manual_url_budget(document, status, args.manual_url_deliveries_this_run)
        completion_readiness: dict[str, Any] | None = None
        if final_verdict == "complete":
            completion_readiness = (caller_observation(project_root, "completion_readiness")
                                    if document.get("caller_context") is not None else evaluate_completion_readiness(project_root, run_id))
            if completion_readiness["verdict"] != "ship":
                raise RunControllerError(
                    "RUN_COMPLETION_NOT_READY",
                    (
                        f"run {run_id} cannot complete while fresh publication readiness is "
                        f"{completion_readiness['verdict']}"
                    ),
                    remediation=(
                        "Resolve every returned readiness finding and rerun finish, or close the run honestly "
                        "with --final-verdict no_ship when that transition is legal."
                    ),
                    details={
                        "run_id": run_id,
                        "publication_verdict": completion_readiness["verdict"],
                        "readiness_generated_at": completion_readiness.get("generated_at"),
                        "blocking_findings": completion_readiness_findings(completion_readiness),
                    },
                )
        document["updated_at"] = now
        document["agent_id"] = agent_id
        document["state"] = state_snapshot(final_verdict, now, blocking_reason)
        document["state_history"].append(
            {
                "from_state": current,
                "to_state": final_verdict,
                "changed_at": now,
                "agent_id": agent_id,
                "reason": reason,
            }
        )
        document["final_verdict"] = final_verdict
        if final_verdict == "failed":
            document["failure_records"].append(
                {
                    "recorded_at": now,
                    "agent_id": agent_id,
                    "state": current,
                    "reason": reason,
                }
            )
        refresh_counts(project_root, document, status)
        event = state_transition_event(
            project_root,
            run_id,
            agent_id,
            from_state=current,
            to_state=final_verdict,
            occurred_at=now,
            message=reason,
            data=(
                {
                    "completion_readiness": {
                        "verdict": completion_readiness["verdict"],
                        "generated_at": completion_readiness.get("generated_at"),
                        "finding_codes": sorted(
                            {
                                str(item.get("code"))
                                for item in completion_readiness_findings(completion_readiness)
                                if item.get("code")
                            }
                        ),
                    }
                }
                if completion_readiness is not None
                else None
            ),
        )
        commit_run_mutation(project_root, run_id, document, event)
        return document


def render_text(document: dict[str, Any]) -> str:
    if document.get("event_id"):
        return f"{document['event_id']}: {document['event_type']} {document['run_id']}\n"
    state = document.get("state") if isinstance(document.get("state"), dict) else {}
    return f"{document.get('run_id')}: {state.get('current')}\n"


def command_document(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "start":
        return run_start(project_root, args)
    if args.command == "transition":
        return run_transition(project_root, args)
    if args.command == "status":
        return run_status(project_root, args)
    if args.command == "event":
        return run_event(project_root, args)
    if args.command == "heartbeat":
        return run_heartbeat(project_root, args)
    if args.command == "adopt":
        return run_adopt(project_root, args)
    if args.command == "abandon":
        return run_abandon(project_root, args)
    if args.command == "recover":
        return run_recover(project_root, args)
    if args.command == "override-manual-url-budget":
        return run_override_manual_url_budget(project_root, args)
    if args.command == "finish":
        return run_finish(project_root, args)
    raise RunControllerError("VALUE_INVALID", f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(raw_argv)
    json_mode = json_mode_requested(raw_argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        document = command_document(project_root, args)
    except RunControllerError as error:
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
    except Exception as error:
        code = getattr(error, "error_code", "")
        if code != "DOMAIN_PACK_REVISION_CONFLICT" and not code.startswith("EVIDENCE_REVISION_"):
            raise
        return load_sibling_module("_script_errors").emit_refusal(error, json_mode=json_mode)

    if args.format == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(document))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
