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
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import urlparse

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

SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.jsonl"
ANSWERS_FILENAME = "answers.json"
WORK_ORDERS_DIR = "work-orders"
WORK_RESULTS_DIR = "work-results"

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


def answers_path(project_root: Path, orchestration_id: str) -> Path:
    return session_dir(project_root, orchestration_id) / ANSWERS_FILENAME


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


def load_json_object(path: Path, *, error_code: str, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OrchestrationControllerError(error_code, f"could not read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise OrchestrationControllerError(error_code, f"invalid JSON in {label}: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise OrchestrationControllerError(error_code, f"{label} must contain a JSON object: {path}")
    return document


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
) -> None:
    append_event(
        project_root,
        session["orchestration_id"],
        {
            "schema_version": SCHEMA_VERSION,
            "orchestration_id": session["orchestration_id"],
            "occurred_at": timestamp_utc(),
            "agent_id": session["agent_id"],
            "event_type": event_type,
            "action_id": action_id,
            "phase": session["phase"],
            "message": message,
            "data": data or {},
        },
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
    return status


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
    """Return a bounded metadata fingerprint for configured immutable raw roots."""
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
    seen: set[Path] = set()
    for root in sorted(set(roots), key=lambda path: path.as_posix()):
        if not root.is_dir():
            continue
        for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            stat = path.stat()
            relative = relative_workspace_path(project_root, path)
            total_bytes += stat.st_size
            records.append(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}")
    digest = hashlib.sha256("\n".join(records).encode()).hexdigest()
    return {"file_count": len(records), "total_bytes": total_bytes, "fingerprint": f"sha256:{digest}"}


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


def load_result(path: Path, action_id: str, project_root: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise OrchestrationControllerError("RESULT_UNREADABLE", f"could not read result file: {path}") from exc
    if size > MAX_RESULT_BYTES:
        raise OrchestrationControllerError("RESULT_INVALID", f"result exceeds {MAX_RESULT_BYTES} bytes")
    document = load_json_object(path, error_code="RESULT_INVALID", label="orchestration result")
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
        postconditions = [
            {
                "check": "fresh_verification_bundle",
                "paths": [
                    f"{evaluation_root}/citation-verification.json",
                    f"{evaluation_root}/quote-verification.json",
                    f"{evaluation_root}/coverage-summary.json",
                    f"{evaluation_root}/lint.json",
                    f"{evaluation_root}/publication-readiness.json",
                ],
            },
            {"check": "publication_readiness", "expected": "ship"},
            {
                "check": "answer_export_written",
                "path": f"runs/orchestrations/{session['orchestration_id']}/answers.json",
            },
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
    write_json_atomic(work_order_path(project_root, session["orchestration_id"], action_id), work_order)
    session["phase"] = spec["phase"]
    session["pending_action_id"] = action_id
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
        work_order["issued_at"] = format_timestamp(now)
        work_order["lease"] = {
            "duration_seconds": duration,
            "expires_at": format_timestamp(now + timedelta(seconds=duration)),
            "attempt": int(lease.get("attempt", 1) or 1) + 1,
        }
        write_json_atomic(path, work_order)
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, session["orchestration_id"]), session)
        record_event(project_root, session, "action_reissued", "Reissued expired work order.", action_id=action_id)
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
        write_json_atomic(path, session)
        record_event(project_root, session, "session_started", "Orchestration session created.")
        return session


def next_work(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    orchestration_id = require_safe_id(args.orchestration_id, "orchestration_id")
    with workspace_lock(session_lock_path(project_root, orchestration_id), purpose=f"orchestration {orchestration_id}"):
        session = load_session(project_root, orchestration_id)
        if args.agent_id is not None and require_agent_id(args.agent_id) != session["agent_id"]:
            raise OrchestrationControllerError(
                "ORCHESTRATION_OWNER_MISMATCH",
                "--agent-id does not own this orchestration session",
                recoverable=False,
            )
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
            verify_runtime_guards(project_root, session, order)
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
        readiness_module = load_sibling_module("publication_readiness")
        bundle = readiness_module.build_bundle(project_root, str(run_id))
        readiness = bundle["publication_readiness"]
        evaluation_dir = project_root / "runs" / str(run_id) / "evaluation"
        citation = load_json_object(
            evaluation_dir / "citation-verification.json",
            error_code="ORCHESTRATION_POSTCONDITION_FAILED",
            label="fresh citation verification",
        )
        lint = load_json_object(
            evaluation_dir / "lint.json",
            error_code="ORCHESTRATION_POSTCONDITION_FAILED",
            label="fresh lint report",
        )
        export = load_json_object(
            evaluation_dir / "export.json",
            error_code="ORCHESTRATION_POSTCONDITION_FAILED",
            label="fresh answer export",
        )
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
        write_json_atomic(evaluation_dir / "quote-verification.json", quotes)
        coverage = status.get("coverage") if isinstance(status.get("coverage"), dict) else {}
        coverage_report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": timestamp_utc(),
            "network_io_executed": False,
            "coverage": coverage,
        }
        write_json_atomic(evaluation_dir / "coverage-summary.json", coverage_report)
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
            write_json_atomic(answers_path(project_root, session["orchestration_id"]), export)
            if current == "verifying":
                controller.run_finish(project_root, child_args(run_id, session["agent_id"], final_verdict="complete"))
            session["active_run_id"] = None
        return "complete", "Fresh publication readiness returned ship and answers were exported."

    raise OrchestrationControllerError("ORCHESTRATION_STATE_INVALID", f"unsupported submitted phase: {phase}")


def submit_result(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    orchestration_id = require_safe_id(args.orchestration_id, "orchestration_id")
    action_id = require_safe_id(args.action_id, "action_id")
    result = load_result(Path(args.result_file).expanduser().resolve(), action_id, project_root)
    with workspace_lock(session_lock_path(project_root, orchestration_id), purpose=f"orchestration {orchestration_id}"):
        session = load_session(project_root, orchestration_id)
        if args.agent_id is not None and require_agent_id(args.agent_id) != session["agent_id"]:
            raise OrchestrationControllerError("ORCHESTRATION_OWNER_MISMATCH", "--agent-id does not own this session")
        retained_path = work_result_path(project_root, orchestration_id, action_id)
        retained = (
            load_json_object(retained_path, error_code="RESULT_INVALID", label="retained result")
            if retained_path.is_file()
            else None
        )
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
            return session
        if session.get("pending_action_id") != action_id:
            raise OrchestrationControllerError(
                "ACTION_NOT_PENDING",
                f"action {action_id} is not the pending action",
                details={"pending_action_id": session.get("pending_action_id")},
            )
        if result["outcome"] == "failed":
            if retained is None:
                write_json_atomic(retained_path, result)
            session["failure_records"].append(
                {"recorded_at": timestamp_utc(), "action_id": action_id, "summary": result["summary"]}
            )
            finish_active_child(project_root, session, "failed")
            session["last_completed_action_id"] = action_id
            session["completed_action_count"] = int(session["completed_action_count"]) + 1
            return finish_session(project_root, session, "failed", result["summary"])
        if result["outcome"] == "blocked":
            if retained is None:
                write_json_atomic(retained_path, result)
            finish_active_child(project_root, session, "blocked_on_sources")
            session["last_completed_action_id"] = action_id
            session["completed_action_count"] = int(session["completed_action_count"]) + 1
            return finish_session(project_root, session, "blocked_on_sources", result["summary"])

        next_phase, completion_reason = verify_action_postconditions(
            project_root,
            session,
            order,
            apply_effects=False,
        )
        # Retain the accepted result before mutating child-run/session state.
        # A crash after this point is recoverable: the identical resubmission
        # replays idempotent finalization from the retained result.
        if retained is None:
            write_json_atomic(retained_path, result)
        finalized_phase, finalized_reason = verify_action_postconditions(
            project_root,
            session,
            order,
            apply_effects=True,
        )
        if finalized_phase != next_phase:
            raise OrchestrationControllerError(
                "ORCHESTRATION_STATE_INVALID",
                "action finalization changed the verified next phase",
                recoverable=True,
            )
        completion_reason = finalized_reason or completion_reason
        session["pending_action_id"] = None
        session["last_completed_action_id"] = action_id
        session["completed_action_count"] = int(session["completed_action_count"]) + 1
        session["phase"] = next_phase or "planning"
        session["updated_at"] = timestamp_utc()
        write_json_atomic(session_path(project_root, orchestration_id), session)
        record_event(
            project_root,
            session,
            "action_completed",
            result["summary"],
            action_id=action_id,
            data={"artifacts": result["artifacts"]},
        )
        if next_phase == "complete":
            return finish_session(project_root, session, "complete", completion_reason or "Orchestration complete.")
        return session


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
