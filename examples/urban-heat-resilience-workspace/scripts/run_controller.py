#!/usr/bin/env python3
"""Manage durable PM run-controller state for one research workspace run.

The controller is a local-only state writer. It records phase transitions and
audit events under ``runs/<run_id>/`` and never performs discovery, acquisition,
normalization, or answer work itself.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_INVALID = 2

STATE_TRANSITIONS = {
    "initialized": ("planned", "failed"),
    "planned": ("discovering", "no_ship", "failed"),
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
PENDING_EVENT_FIELD = "_pending_event"
MUTATION_RECOVERY_DIR = ".recovery"

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import emit_error, handle_system_exit, json_mode_requested
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
        self.error_code = error_code
        self.exit_code = exit_code
        self.recoverable = recoverable
        self.remediation = remediation
        self.details = details


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
    if parsed <= 0:
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


def run_lock_path(project_root: Path, run_id: str) -> Path:
    return run_dir(project_root, run_id) / ".locks" / "run-state.lock"


def relative_workspace_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        raise mutation_write_error(path, "json_atomic_replace", exc) from exc


def write_run_state_atomic(path: Path, document: dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        raise mutation_write_error(path, "run_state_atomic_replace", exc) from exc


def append_event(path: Path, event: dict[str, Any]) -> bool:
    existing = load_events(path)
    event_id = event.get("event_id")
    for item in existing:
        if item.get("event_id") != event_id:
            continue
        if item == event:
            return False
        raise RunControllerError(
            "RUN_EVENT_ID_CONFLICT",
            f"Event id {event_id!r} already exists with different content in {path}",
            recoverable=False,
            remediation="Preserve the event log and investigate the conflicting retained records before recovery.",
            details={"artifact": path.as_posix(), "event_id": event_id},
        )
    content = "".join(compact_json(item) + "\n" for item in [*existing, event])
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        raise mutation_write_error(path, "event_log_atomic_replace", exc) from exc
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
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": next_event_id(events_path(project_root, run_id)),
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
    append_event(events_path(project_root, run_id), event)
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


def load_run_state(project_root: Path, run_id: str, *, allow_pending: bool = False) -> dict[str, Any]:
    path = run_state_path(project_root, run_id)
    if not path.is_file():
        raise RunControllerError(
            "RUN_UNKNOWN",
            f"unknown run id: {run_id} (no runs/{run_id}/run-state.json)",
            details={"run_id": run_id},
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
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
            "Run override-manual-url-budget with --new-limit, --override-reason, and --approved-by, "
            "then retry the transition or finish command."
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
            "Run override-manual-url-budget with --new-limit, --override-reason, and --approved-by, "
            "then retry the transition or finish command."
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


def run_start(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    agent_id = require_agent_id(args.agent_id)
    run_id = validate_run_id(args.run_id, allow_generate=True)
    with workspace_lock(run_lock_path(project_root, run_id), purpose=f"run state {run_id}"):
        directory = run_dir(project_root, run_id)
        if directory.exists() and run_state_path(project_root, run_id).exists():
            raise RunControllerError("RUN_EXISTS", f"run already exists: {run_id}", details={"run_id": run_id})
        directory.mkdir(parents=True, exist_ok=True)
        quarantine_run_temp_files(project_root, run_id)

        now = timestamp_utc()
        status = status_document(project_root)
        baseline = capture_baseline_artifacts(project_root, run_id, status)
        project = status.get("project") if isinstance(status.get("project"), dict) else {}
        handoff = project.get("handoff") if isinstance(project.get("handoff"), dict) else None
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
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
        quarantined = quarantine_run_temp_files(project_root, run_id)
        document = load_run_state(project_root, run_id, allow_pending=True)
        pending = document.get(PENDING_EVENT_FIELD)
        if pending is None and not quarantined:
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
            completion_readiness = evaluate_completion_readiness(project_root, run_id)
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

    if args.format == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(document))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
