#!/usr/bin/env python3
"""Aggregate machine-readable status for a research workspace.

This is the single status surface for orchestrators and parent agents. One
invocation combines, without shelling out:

- project identity and the optional ``project.handoff`` correlation block
  from ``research.yml``,
- contract versions from ``workspace-system.yml``,
- smoke validation results (``smoke_validate_workspace.py``),
- question backlog counts (``question_status.py``),
- source pipeline coverage from the manifest plus the unnormalized-source
  signal (``query_index.py``) and open source-request counts
  (``source_requests.py``),
- lint issue counts by severity (``lint.py``),
- run budgets from ``research.yml`` ``run`` (loop skills read them here),
- optional per-run budget counters supplied by the current runner,
- a readiness verdict with machine-checkable reasons.

The script is read-only by default. ``--append-log`` optionally appends one
status summary entry to ``log.md``.

Verdict rules (fixed in schema version 1.0):

- ``attention_required``: smoke validation failed, lint reported HIGH issues,
  or either check could not run.
- ``complete``: no actionable (open or in_progress) questions, no blocked
  questions, smoke passed, and no HIGH lint issues.
- ``blocked_on_sources``: no actionable questions, but blocked questions
  remain.
- ``in_progress``: anything else (actionable questions remain).

Exit codes:

- default mode: ``0`` when the status document was produced (any verdict),
  ``2`` when the workspace cannot be read (missing or invalid ``research.yml``
  or ``workspace-system.yml``).
- ``--check-complete``: ``0`` when the verdict is ``complete``, ``3`` when the
  verdict is ``blocked_on_sources``, ``1`` when the verdict is ``in_progress``,
  ``4`` when the verdict is ``attention_required``, ``2`` when the workspace
  cannot be read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to report workspace status") from exc

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_health import evaluate_workspace_health
from _workspace_locks import LockUnavailableError, workspace_lock
from _workspace_module_loader import load_workspace_module

SCHEMA_VERSION = "1.0"
VERDICT_COMPLETE = "complete"
VERDICT_IN_PROGRESS = "in_progress"
VERDICT_BLOCKED_ON_SOURCES = "blocked_on_sources"
VERDICT_ATTENTION_REQUIRED = "attention_required"
EXIT_REPORTED = 0
EXIT_NOT_COMPLETE = 1
EXIT_WORKSPACE_UNREADABLE = 2
EXIT_BLOCKED_ON_SOURCES = 3
EXIT_ATTENTION_REQUIRED = 4
CHECK_COMPLETE_EXIT_CODES = {
    VERDICT_COMPLETE: EXIT_REPORTED,
    VERDICT_BLOCKED_ON_SOURCES: EXIT_BLOCKED_ON_SOURCES,
    VERDICT_IN_PROGRESS: EXIT_NOT_COMPLETE,
    VERDICT_ATTENTION_REQUIRED: EXIT_ATTENTION_REQUIRED,
}
PROJECT_IDENTITY_FIELDS = ("name", "description", "owner_goal", "language")
CONTRACT_FIELDS = ("starter_version", "schema_version", "compatible_research_yml_contract")
MAX_REASON_ITEMS = 10
RUN_BUDGET_DEFAULTS = {
    "max_questions_per_run": 25,
    "max_source_requests_per_run": 10,
    "max_releases_per_run": 75,
    "max_discovery_results_per_run": 50,
    "max_academic_provider_requests_per_run": 25,
    "max_manual_url_deliveries_per_run": 10,
    "max_web_downloads_per_run": 10,
    "max_open_questions_total": 250,
    "max_intake_per_hour": 25,
    "max_mcp_intake_batch_questions": 100,
    "claim_staleness_hours": 24,
    "stale_run_threshold_hours": 4,
}
DEFAULT_RELEASES_PER_QUESTION = 3
ACQUISITION_DEFAULT_MAX_DOWNLOADS_PER_RUN = 10
GITHUB_DEFAULT_MAX_ARCHIVE_BYTES_PER_RUN = 100 * 1024 * 1024
CACHE_DIR = ".research-cache"
STATUS_CACHE_FILENAME = "workspace-status.json"
WEB_CURATION_KINDS = {"html", "web_link", "link"}
EMPTY_CURATION_COUNTS = {
    "automated_web_records": 0,
    "cited_automated_web_records": 0,
    "missing_terms_license": 0,
    "missing_source_note": 0,
    "missing_origin_url": 0,
    "missing_checksum": 0,
    "missing_candidate_id": 0,
}
BUDGET_COUNTER_FIELDS = (
    "questions_processed_this_run",
    "source_requests_opened_this_run",
    "releases_this_run",
    "discovery_results_this_run",
    "acquisition_downloads_this_run",
    "github_archive_bytes_this_run",
    "academic_provider_requests_this_run",
    "web_downloads_this_run",
    "manual_url_deliveries_this_run",
)
from _handoff_signature import project_handoff_verification
from _intake_limits import recent_intake_summary
from _script_errors import emit_error, handle_system_exit, json_mode_requested


def parse_non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report aggregate research workspace status for orchestrators and agents.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. Defaults to text.",
    )
    parser.add_argument(
        "--check-complete",
        action="store_true",
        help=(
            "Exit 0 when complete, 1 when in progress, 3 when blocked on sources, "
            "4 when attention is required. The status document is still printed."
        ),
    )
    parser.add_argument(
        "--append-log",
        action="store_true",
        help="Append a compact status summary to log.md.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the .research-cache workspace-status cache for this invocation.",
    )
    parser.add_argument(
        "--questions-processed-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "Number of questions this runner has resolved or terminally processed in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--source-requests-opened-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "Number of source requests this runner has opened in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--releases-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "Number of successful claim releases this runner has performed in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--discovery-results-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "Discovery candidates or result records proposed by this runner in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--acquisition-downloads-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "Provider-backed downloads completed by this runner in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--github-archive-bytes-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "GitHub archive bytes downloaded by this runner in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--academic-provider-requests-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "OpenAlex/arXiv provider requests made by this runner in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--web-downloads-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "Contracted web provider downloads completed by this runner in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--manual-url-deliveries-this-run",
        type=parse_non_negative_int,
        default=None,
        help=(
            "Manual URL deliveries completed by this runner in the current run. "
            "When provided, readiness includes budget_state."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Optional PM run id under runs/<run_id>. When omitted, status reports the newest "
            "non-terminal run if one exists, otherwise the newest terminal run."
        ),
    )
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so its logic is reused directly."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Missing {label}: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"Invalid {label}: {path} must contain a mapping")
    return document


def relative_workspace_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def configured_workspace_path(project_root: Path, config: dict[str, Any], section: str, key: str, default: str) -> Path:
    section_config = config.get(section) if isinstance(config.get(section), dict) else {}
    value = section_config.get(key, default)
    if not isinstance(value, str) or not value.strip():
        return project_root / default
    normalized = value.strip().replace("\\", "/")
    if "://" in normalized or (len(value) >= 2 and value[1] == ":" and value[0].isalpha()):
        return project_root / default
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return project_root / default
    return project_root / path.as_posix()


def markdown_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob("*.md") if path.is_file())


def load_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        document = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    return document if isinstance(document, dict) else {}


def source_ids_from_frontmatter(frontmatter: dict[str, Any]) -> list[str]:
    source_ids = frontmatter.get("source_ids")
    if not isinstance(source_ids, list):
        return []
    return [source_id.strip() for source_id in source_ids if isinstance(source_id, str) and source_id.strip()]


def cited_source_ids(project_root: Path, config: dict[str, Any]) -> set[str]:
    wiki_root = configured_workspace_path(project_root, config, "wiki", "root", "wiki")
    output_root = configured_workspace_path(project_root, config, "outputs", "default_dir", "wiki/outputs")
    source_root = wiki_root / "sources"
    paths = markdown_files(wiki_root)
    known = {path.resolve() for path in paths}
    for path in markdown_files(output_root):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved not in known:
            paths.append(path)
            known.add(resolved)

    cited: set[str] = set()
    for path in paths:
        try:
            path.relative_to(source_root)
            continue
        except ValueError:
            pass
        frontmatter = load_frontmatter(path)
        if frontmatter.get("type") == "source":
            continue
        cited.update(source_ids_from_frontmatter(frontmatter))
    return cited


def selected_candidate_request_ids(project_root: Path, config: dict[str, Any]) -> set[str]:
    discover_sources = load_sibling_module("discover_sources")
    path = discover_sources.candidate_store_path(project_root, config)
    if not path.is_file():
        return set()
    request_ids: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("status") not in {"selected", "fetched"}:
            continue
        value = record.get("selected_for_request_id") or record.get("selected_request_id") or record.get("request_id")
        if isinstance(value, str) and value.strip():
            request_ids.add(value.strip())
    return request_ids


def has_text_field(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    return isinstance(value, str) and bool(value.strip())


def has_license_or_terms_status(provenance: dict[str, Any]) -> bool:
    return (
        has_text_field(provenance, "license")
        or has_text_field(provenance, "terms_url")
        or has_text_field(provenance, "terms_note")
    )


def source_curation_counts(project_root: Path, config: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict(EMPTY_CURATION_COUNTS)
    cited = cited_source_ids(project_root, config)
    selected_request_ids = selected_candidate_request_ids(project_root, config)
    for record in records:
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            continue
        kind = record.get("kind")
        if kind not in WEB_CURATION_KINDS or not has_text_field(provenance, "retrieved_by"):
            continue
        counts["automated_web_records"] += 1
        source_id = record.get("id")
        is_cited = isinstance(source_id, str) and source_id in cited
        if is_cited:
            counts["cited_automated_web_records"] += 1
        if not has_license_or_terms_status(provenance):
            counts["missing_terms_license"] += 1
        if not is_cited:
            continue
        if not has_text_field(provenance, "notes"):
            counts["missing_source_note"] += 1
        if not has_text_field(provenance, "origin_url"):
            counts["missing_origin_url"] += 1
        checksum = provenance.get("checksum")
        if not (isinstance(checksum, str) and checksum.strip() and provenance.get("checksum_verified") is True):
            counts["missing_checksum"] += 1
        request_id = provenance.get("request_id")
        if (
            isinstance(request_id, str)
            and request_id.strip() in selected_request_ids
            and not has_text_field(provenance, "candidate_id")
        ):
            counts["missing_candidate_id"] += 1
    return counts


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


def project_section(config: dict[str, Any], project_root: Path) -> dict[str, Any]:
    project = config.get("project") if isinstance(config.get("project"), dict) else {}
    section: dict[str, Any] = {}
    for field in PROJECT_IDENTITY_FIELDS:
        value = project.get(field)
        section[field] = value if isinstance(value, str) else None
    handoff = project.get("handoff")
    section["handoff"] = handoff if isinstance(handoff, dict) else None
    signature = project.get("handoff_signature")
    section["handoff_signature"] = signature if isinstance(signature, str) else None
    section["handoff_signature_status"] = project_handoff_verification(project_root, project).status
    return section


def contract_section(metadata: dict[str, Any]) -> dict[str, Any]:
    workspace_system = metadata.get("workspace_system") if isinstance(metadata.get("workspace_system"), dict) else {}
    section: dict[str, Any] = {}
    for field in CONTRACT_FIELDS:
        value = workspace_system.get(field)
        section[field] = value if isinstance(value, str) else None
    return section


def positive_int_or_default(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def mapping_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def run_section(config: dict[str, Any]) -> dict[str, Any]:
    """Run budgets for unattended loops; invalid or absent values fall back to defaults.

    Wall-clock and token budgets belong to the orchestrator, not the workspace.
    """
    run_config = mapping_or_empty(config.get("run"))
    section: dict[str, Any] = {}
    for field in (
        "max_questions_per_run",
        "max_source_requests_per_run",
        "claim_staleness_hours",
        "max_open_questions_total",
        "max_intake_per_hour",
        "max_mcp_intake_batch_questions",
        "stale_run_threshold_hours",
        "max_discovery_results_per_run",
        "max_academic_provider_requests_per_run",
        "max_manual_url_deliveries_per_run",
    ):
        default = RUN_BUDGET_DEFAULTS[field]
        section[field] = positive_int_or_default(run_config.get(field, default), default)
    web_default = int(section["max_manual_url_deliveries_per_run"])
    section["max_web_downloads_per_run"] = positive_int_or_default(
        run_config.get("max_web_downloads_per_run", web_default),
        web_default,
    )
    releases_default = int(section["max_questions_per_run"]) * DEFAULT_RELEASES_PER_QUESTION
    releases_value = run_config.get("max_releases_per_run", releases_default)
    section["max_releases_per_run"] = positive_int_or_default(releases_value, releases_default)

    integrations = mapping_or_empty(config.get("integrations"))
    acquisition = mapping_or_empty(integrations.get("acquisition"))
    section["max_acquisition_downloads_per_run"] = positive_int_or_default(
        acquisition.get("max_downloads_per_run", ACQUISITION_DEFAULT_MAX_DOWNLOADS_PER_RUN),
        ACQUISITION_DEFAULT_MAX_DOWNLOADS_PER_RUN,
    )
    github = mapping_or_empty(acquisition.get("github"))
    section["max_github_archive_bytes_per_run"] = positive_int_or_default(
        github.get("max_archive_bytes", GITHUB_DEFAULT_MAX_ARCHIVE_BYTES_PER_RUN),
        GITHUB_DEFAULT_MAX_ARCHIVE_BYTES_PER_RUN,
    )
    return section


def parse_claim_timestamp(value: Any) -> datetime | None:
    """Parse a claimed_at value (quoted string or YAML-parsed datetime) to aware UTC."""
    if hasattr(value, "isoformat") and not isinstance(value, str):
        text = str(value.isoformat())
    elif isinstance(value, str) and value.strip():
        text = value.strip()
    else:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def smoke_section(project_root: Path) -> dict[str, Any]:
    section: dict[str, Any] = {"ok": False, "issues": 0, "by_severity": {}, "error": None}
    smoke = load_sibling_module("smoke_validate_workspace")
    try:
        results = smoke.run_checks(project_root)
    except SystemExit as exc:
        section["error"] = str(exc)
        return section
    issues = results.get("issues") if isinstance(results.get("issues"), list) else []
    by_severity: dict[str, int] = {}
    for item in issues:
        severity = item.get("severity") if isinstance(item, dict) else None
        if isinstance(severity, str):
            by_severity[severity] = by_severity.get(severity, 0) + 1
    section["ok"] = bool(results.get("ok"))
    section["issues"] = len(issues)
    section["by_severity"] = dict(sorted(by_severity.items()))
    return section


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def load_source_request_index(project_root: Path, config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
    source_requests = load_sibling_module("source_requests")
    try:
        path = source_requests.requests_path(project_root, config)
        records = source_requests.load_requests(path)
    except SystemExit as exc:
        return {}, str(exc)
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        request_id = record.get("request_id")
        if isinstance(request_id, str) and request_id.strip() and request_id not in by_id:
            by_id[request_id.strip()] = record
    return by_id, None


def blocked_request_link_summary(
    blocked: list[dict[str, Any]],
    source_requests_by_id: dict[str, dict[str, Any]],
    requests_error: str | None,
) -> dict[str, Any]:
    with_requests = 0
    missing_request_slugs: list[str] = []
    missing_request_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    open_request_ids: list[str] = []

    for record in blocked:
        slug = str(record.get("slug") or "")
        explicit_request_ids = string_list(record.get("blocking_request_ids"))
        linked_request_ids = [
            request_id
            for request_id, request in source_requests_by_id.items()
            if request.get("status") == "open"
            and slug in string_list(request.get("question_slugs"))
        ]
        request_ids = explicit_request_ids or linked_request_ids
        if not request_ids:
            missing_request_slugs.append(slug)
            errors.append({"slug": slug, "request_id": None, "problem": "empty"})
            continue
        slug_has_error = False
        if requests_error is not None:
            slug_has_error = True
            for request_id in request_ids:
                errors.append({"slug": slug, "request_id": request_id, "problem": "request_artifact_unreadable"})
        else:
            for request_id in request_ids:
                request = source_requests_by_id.get(request_id)
                if request is None:
                    slug_has_error = True
                    missing_request_ids.append(request_id)
                    errors.append({"slug": slug, "request_id": request_id, "problem": "missing"})
                    continue
                if request.get("status") != "open":
                    slug_has_error = True
                    errors.append(
                        {
                            "slug": slug,
                            "request_id": request_id,
                            "problem": "not_open",
                            "status": request.get("status"),
                        }
                    )
                slugs = string_list(request.get("question_slugs"))
                if slug not in slugs:
                    slug_has_error = True
                    errors.append({"slug": slug, "request_id": request_id, "problem": "not_linked"})
                if request.get("status") == "open":
                    open_request_ids.append(request_id)
        if slug_has_error:
            missing_request_slugs.append(slug)
        else:
            with_requests += 1

    return {
        "blocked_questions_with_requests": with_requests,
        "blocked_questions_missing_requests": len(missing_request_slugs),
        "blocked_slugs_missing_requests": missing_request_slugs,
        "missing_blocking_request_ids": missing_request_ids,
        "blocked_request_link_errors": errors,
        "blocked_open_request_ids": sorted(set(open_request_ids)),
    }


def questions_section(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    records = question_status.collect_questions(questions_dir)
    report = question_status.build_report(records)
    actionable = [record for record in report["questions"] if record.get("status") in question_status.ACTIONABLE_STATUSES]
    blocked = [record for record in report["questions"] if record.get("status") == "blocked"]
    human_review = [record for record in report["questions"] if record.get("status") == "human_review"]
    claim_staleness_hours = run_section(config)["claim_staleness_hours"]
    now = datetime.now(timezone.utc)
    claimed_details: list[dict[str, Any]] = []
    for record in report["questions"]:
        claimed_by = record.get("claimed_by")
        if record.get("status") != "in_progress" or not isinstance(claimed_by, str) or not claimed_by.strip():
            continue
        claimed_at = record.get("claimed_at")
        parsed_at = parse_claim_timestamp(claimed_at)
        claimed_details.append(
            {
                "slug": record["slug"],
                "claimed_by": claimed_by.strip(),
                "claimed_at": parsed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed_at else None,
                "claimed_at_raw": claimed_at if isinstance(claimed_at, str) and claimed_at.strip() else None,
                "stale": parsed_at is None or (now - parsed_at).total_seconds() / 3600 > claim_staleness_hours,
            }
        )
    source_requests_by_id, requests_error = load_source_request_index(project_root, config)
    blocked_links = blocked_request_link_summary(blocked, source_requests_by_id, requests_error)
    return {
        "total": report["total"],
        "by_status": report["by_status"],
        "by_priority": report["by_priority"],
        "actionable": report["actionable"],
        "human_review": report.get("human_review", len(human_review)),
        "blocked": report["blocked"],
        "answered": report["answered"],
        "claimed": len(claimed_details),
        "actionable_slugs": [record["slug"] for record in actionable],
        "human_review_slugs": [record["slug"] for record in human_review],
        "blocked_slugs": [record["slug"] for record in blocked],
        "claimed_slugs": [record["slug"] for record in claimed_details],
        "stale_claim_slugs": [record["slug"] for record in claimed_details if record["stale"]],
        **blocked_links,
        "_claimed_details": claimed_details,
        "_claim_staleness_hours": claim_staleness_hours,
    }


def coverage_section(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    coverage = load_sibling_module("coverage_manifest")
    question_status = load_sibling_module("question_status")
    section = {
        "manifests_total": coverage.coverage_manifest_count(project_root, config),
        "required_questions": 0,
        "passed": 0,
        "blocked": 0,
        "pending": 0,
        "missing": 0,
        "invalid": 0,
        "coverage_verdicts": {},
        "required_question_counts": {
            "total": 0,
            "passed": 0,
            "blocked": 0,
            "pending": 0,
            "missing": 0,
            "invalid": 0,
        },
    }
    try:
        coverage_dir = coverage.coverage_dir(project_root, config)
    except coverage.CoverageManifestError:
        coverage_dir = None
    if coverage_dir is not None and coverage_dir.is_dir():
        for path in sorted(coverage_dir.glob("*.yml")):
            if not path.is_file():
                continue
            summary = coverage.coverage_summary_for_question(project_root, config, path.stem, {})
            status = summary.get("coverage_status")
            if isinstance(status, str) and status:
                section["coverage_verdicts"][path.stem] = status
            if status == "pass":
                section["passed"] += 1
            elif status == "blocked":
                section["blocked"] += 1
            elif status == "pending":
                section["pending"] += 1
            elif status == "invalid":
                section["invalid"] += 1
    questions_dir = question_status.questions_directory(project_root, config)
    if not questions_dir.is_dir():
        return section
    for path in sorted(questions_dir.glob("*.md")):
        frontmatter = question_status.load_frontmatter(path)
        if not isinstance(frontmatter, dict):
            continue
        if frontmatter.get("type") != "question" or frontmatter.get("status") not in {"answered", "human_review"}:
            continue
        if frontmatter.get("coverage_required") is not True:
            continue
        section["required_questions"] += 1
        required_counts = section["required_question_counts"]
        required_counts["total"] += 1
        summary = coverage.coverage_summary_for_question(project_root, config, path.stem, frontmatter)
        status = summary.get("coverage_status")
        if status == "pass":
            required_counts["passed"] += 1
        elif status == "blocked":
            required_counts["blocked"] += 1
        elif status == "pending":
            required_counts["pending"] += 1
        elif status == "missing":
            required_counts["missing"] += 1
            section["missing"] += 1
        elif status == "invalid":
            required_counts["invalid"] += 1
    return section


def intake_section(project_root: Path, questions: dict[str, Any]) -> dict[str, Any]:
    by_status = questions.get("by_status") if isinstance(questions.get("by_status"), dict) else {}
    section = {
        "open_questions_total": int(by_status.get("open", 0) or 0),
        **recent_intake_summary(project_root / "log.md"),
    }
    return section


def count_string_value(counts: dict[str, int], value: Any) -> None:
    key = value.strip() if isinstance(value, str) and value.strip() else "unknown"
    counts[key] = counts.get(key, 0) + 1


def sorted_nonzero_counts(counts: dict[str, int]) -> dict[str, int]:
    return {key: value for key, value in sorted(counts.items()) if value}


def candidate_section(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    discover_sources = load_sibling_module("discover_sources")
    statuses = tuple(discover_sources.CANDIDATE_STATUSES)
    path = discover_sources.candidate_store_path(project_root, config)
    section: dict[str, Any] = {
        "store_exists": False,
        "candidates_path": relative_workspace_path(project_root, path),
        "total": 0,
        "invalid_records": 0,
        "by_status": {status: 0 for status in statuses},
        "by_selection_status": {},
        "by_evidence_path": {},
        "by_trust_tier": {},
        "by_recommended_action": {},
        "by_fetch_status": {},
        "by_fetched_status": {"fetched": 0, "not_fetched": 0},
        "official_candidates": 0,
        "aggregator_candidates": 0,
        "linked_to_source_requests": 0,
        "selection": {"selected": 0, "selected_with_request": 0, "selected_without_request": 0},
        "rejections": {"total": 0, "with_reason": 0, "missing_reason": 0, "by_reason": {}},
        "error": None,
    }
    if not path.is_file():
        return section

    section["store_exists"] = True
    by_evidence_path: dict[str, int] = {}
    by_trust_tier: dict[str, int] = {}
    by_selection_status: dict[str, int] = {}
    by_fetch_status: dict[str, int] = {}
    by_recommended_action: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        section["error"] = str(exc)
        return section

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            section["invalid_records"] += 1
            continue
        if not isinstance(record, dict):
            section["invalid_records"] += 1
            continue
        record = discover_sources.apply_candidate_schema_defaults(record)
        status = discover_sources.candidate_status(record)
        selection_status = str(record.get("selection_status") or "")
        fetch_status = str(record.get("fetch_status") or "")
        trust_tier = str(record.get("trust_tier") or "")
        section["total"] += 1
        section["by_status"][status] += 1
        section["by_fetched_status"]["fetched" if status == "fetched" else "not_fetched"] += 1
        count_string_value(by_selection_status, selection_status)
        count_string_value(by_fetch_status, fetch_status)
        count_string_value(by_evidence_path, record.get("evidence_path"))
        count_string_value(by_trust_tier, record.get("trust_tier"))
        count_string_value(by_recommended_action, record.get("recommended_action"))
        if trust_tier.startswith("official_") or record.get("official_source") is True:
            section["official_candidates"] += 1
        if trust_tier == "aggregator" or str(record.get("source_type") or "") == "aggregator":
            section["aggregator_candidates"] += 1
        if discover_sources.selected_request_id(record) is not None:
            section["linked_to_source_requests"] += 1

        if status == "selected":
            section["selection"]["selected"] += 1
            if discover_sources.selected_request_id(record) is None:
                section["selection"]["selected_without_request"] += 1
            else:
                section["selection"]["selected_with_request"] += 1
        elif status == "rejected":
            section["rejections"]["total"] += 1
            reason = record.get("rejection_reason")
            if isinstance(reason, str) and reason.strip():
                section["rejections"]["with_reason"] += 1
                count_string_value(by_reason, reason)
            else:
                section["rejections"]["missing_reason"] += 1

    section["by_evidence_path"] = sorted_nonzero_counts(by_evidence_path)
    section["by_selection_status"] = sorted_nonzero_counts(by_selection_status)
    section["by_trust_tier"] = sorted_nonzero_counts(by_trust_tier)
    section["by_recommended_action"] = sorted_nonzero_counts(by_recommended_action)
    section["by_fetch_status"] = sorted_nonzero_counts(by_fetch_status)
    section["rejections"]["by_reason"] = sorted_nonzero_counts(by_reason)
    return section


def sources_section(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    query_index = load_sibling_module("query_index")
    section: dict[str, Any] = {
        "manifest_exists": False,
        "manifest_records": 0,
        "invalid_records": 0,
        "by_status": {},
        "unnormalized": 0,
        "needs_ocr": 0,
        "requests_open": 0,
        "requests_open_ids": [],
        "curation": dict(EMPTY_CURATION_COUNTS),
        "evidence_usability_overrides": {"count": 0, "source_ids": []},
    }
    try:
        manifest = query_index.manifest_path(project_root, config)
    except SystemExit as exc:
        section["error"] = str(exc)
        return section
    if manifest.is_file():
        section["manifest_exists"] = True
        by_status: dict[str, int] = {}
        manifest_records: list[dict[str, Any]] = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                section["invalid_records"] += 1
                continue
            if not isinstance(record, dict):
                section["invalid_records"] += 1
                continue
            section["manifest_records"] += 1
            manifest_records.append(record)
            status = record.get("status")
            key = status if isinstance(status, str) and status else "unknown"
            by_status[key] = by_status.get(key, 0) + 1
        section["by_status"] = dict(sorted(by_status.items()))
        section["curation"] = source_curation_counts(project_root, config, manifest_records)
        section["evidence_usability_overrides"] = evidence_usability_override_summary(manifest_records)
    try:
        section["unnormalized"] = len(query_index.unnormalized_source_ids(project_root, config))
    except SystemExit as exc:
        section["error"] = str(exc)
    try:
        normalized_root = query_index.normalized_dir(project_root, config)
    except SystemExit as exc:
        section["error"] = str(exc)
        normalized_root = None
    if normalized_root is not None and normalized_root.is_dir():
        for path in sorted(normalized_root.rglob("*.md")):
            try:
                frontmatter, _ = query_index.split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if frontmatter.get("needs_ocr") is True:
                section["needs_ocr"] += 1
    source_requests = load_sibling_module("source_requests")
    try:
        requests = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit as exc:
        section["error"] = str(exc)
        requests = []
    open_ids = [
        record["request_id"]
        for record in requests
        if record.get("status") == "open" and isinstance(record.get("request_id"), str)
    ]
    section["requests_open"] = len(open_ids)
    section["requests_open_ids"] = sorted(open_ids)
    return section


def evidence_usability_override_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    source_ids: list[str] = []
    for record in records:
        source_id = record.get("id")
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        if isinstance(source_id, str) and isinstance(provenance.get("evidence_usability_override"), dict):
            source_ids.append(source_id)
    return {"count": len(source_ids), "source_ids": sorted(source_ids)}


def lint_section(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    section: dict[str, Any] = {"issue_counts": {}, "pages_checked": 0, "error": None}
    lint = load_sibling_module("lint")
    try:
        results = lint.run_checks(project_root, config)
    except SystemExit as exc:
        section["error"] = str(exc)
        return section
    stats = results.get("stats") if isinstance(results.get("stats"), dict) else {}
    issue_counts = stats.get("issue_counts") if isinstance(stats.get("issue_counts"), dict) else {}
    section["issue_counts"] = issue_counts
    section["pages_checked"] = results.get("pages_checked", 0)
    return section


def operational_debt_section(
    questions: dict[str, Any],
    candidates: dict[str, Any],
    sources: dict[str, Any],
    lint: dict[str, Any],
) -> dict[str, Any]:
    """Summarize durable warnings and deferred work without hiding completion debt."""
    question_statuses = questions.get("by_status") if isinstance(questions.get("by_status"), dict) else {}
    candidate_statuses = candidates.get("by_status") if isinstance(candidates.get("by_status"), dict) else {}
    source_statuses = sources.get("by_status") if isinstance(sources.get("by_status"), dict) else {}
    issue_counts = lint.get("issue_counts") if isinstance(lint.get("issue_counts"), dict) else {}
    warnings_by_severity = {
        severity: int(issue_counts.get(severity, 0) or 0)
        for severity in ("MEDIUM", "LOW")
        if int(issue_counts.get(severity, 0) or 0)
    }
    deferred = {
        "questions": int(question_statuses.get("deferred", 0) or 0),
        "candidates": int(candidate_statuses.get("deferred", 0) or 0),
        "sources": int(source_statuses.get("deferred", 0) or 0),
    }
    warning_count = sum(warnings_by_severity.values())
    deferred_count = sum(deferred.values())
    return {
        "warning_count": warning_count,
        "warnings_by_severity": warnings_by_severity,
        "deferred_count": deferred_count,
        "deferred": deferred,
        # Warnings remain visible but do not by themselves turn an otherwise
        # complete workspace into a failure. Deferred work must be explicitly
        # disposed before the readiness verdict can be complete.
        "blocks_completion": deferred_count > 0,
        "has_debt": warning_count > 0 or deferred_count > 0,
    }


def summarize_slugs(slugs: list[str]) -> str:
    shown = slugs[:MAX_REASON_ITEMS]
    summary = ", ".join(shown)
    remaining = len(slugs) - len(shown)
    if remaining > 0:
        summary += f", and {remaining} more"
    return summary


def claim_timestamp_label(detail: dict[str, Any]) -> str:
    claimed_at = detail.get("claimed_at")
    if isinstance(claimed_at, str) and claimed_at:
        return claimed_at
    raw = detail.get("claimed_at_raw")
    if isinstance(raw, str) and raw:
        return f"unparseable {raw}"
    return "unrecorded"


def summarize_claim_holders(details: list[dict[str, Any]]) -> str:
    shown = details[:MAX_REASON_ITEMS]
    summary = ", ".join(
        f"{detail['slug']} held by {detail['claimed_by']} since {claim_timestamp_label(detail)}"
        for detail in shown
    )
    remaining = len(details) - len(shown)
    if remaining > 0:
        summary += f", and {remaining} more"
    return summary


def append_claim_readiness_reasons(reasons: list[str], questions: dict[str, Any]) -> None:
    details = questions.get("_claimed_details") if isinstance(questions.get("_claimed_details"), list) else []
    if not details:
        return
    reasons.append(f"{len(details)} in-progress claim(s): {summarize_claim_holders(details)}.")
    stale_details = [detail for detail in details if detail.get("stale")]
    if stale_details:
        staleness_hours = int(questions.get("_claim_staleness_hours", RUN_BUDGET_DEFAULTS["claim_staleness_hours"]))
        reasons.append(
            f"{len(stale_details)} stale claim(s) exceed {staleness_hours}h or have invalid timestamps: "
            f"{summarize_claim_holders(stale_details)}. Recover with scripts/question_claim.py "
            f"claim --steal --if-older-than {staleness_hours}."
        )


def structured_verdict_reasons(
    verdict: str,
    reasons: list[str],
    questions: dict[str, Any],
    sources: dict[str, Any],
    lint: dict[str, Any],
    operational_debt: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    structured: list[dict[str, Any]] = []
    high_issues = int(lint.get("issue_counts", {}).get("HIGH", 0) or 0) if isinstance(lint.get("issue_counts"), dict) else 0
    if high_issues:
        structured.append({"code": "lint_high", "severity": "attention", "count": high_issues})
    invalid_sources = int(sources.get("invalid_records", 0) or 0)
    if invalid_sources:
        structured.append({"code": "source_manifest_invalid_records", "severity": "attention", "count": invalid_sources})
    debt = operational_debt if isinstance(operational_debt, dict) else {}
    warning_count = int(debt.get("warning_count", 0) or 0)
    if warning_count:
        structured.append(
            {
                "code": "operational_warnings_accumulated",
                "severity": "warning",
                "count": warning_count,
                "by_severity": dict(debt.get("warnings_by_severity", {})),
            }
        )
    deferred_count = int(debt.get("deferred_count", 0) or 0)
    if deferred_count:
        structured.append(
            {
                "code": "operational_deferred_work",
                "severity": "attention",
                "count": deferred_count,
                "deferred": dict(debt.get("deferred", {})),
            }
        )
    blocked_missing_requests = int(questions.get("blocked_questions_missing_requests", 0) or 0)
    if blocked_missing_requests:
        structured.append(
            {
                "code": "blocked_request_link_missing",
                "severity": "attention",
                "count": blocked_missing_requests,
                "question_slugs": list(questions.get("blocked_slugs_missing_requests", [])),
                "missing_request_ids": list(questions.get("missing_blocking_request_ids", [])),
                "remediation": (
                    "Create a bounded open source request linked to each named question, "
                    "then retain the returned request id in the blocked resolution."
                ),
            }
        )
    actionable = int(questions.get("actionable", 0) or 0)
    if actionable:
        structured.append(
            {
                "code": "actionable_questions_remaining",
                "severity": "in_progress",
                "count": actionable,
                "question_slugs": list(questions.get("actionable_slugs", [])),
            }
        )
    blocked = int(questions.get("blocked", 0) or 0)
    if verdict == VERDICT_BLOCKED_ON_SOURCES and blocked:
        structured.append(
            {
                "code": "blocked_on_linked_source_requests",
                "severity": "blocked",
                "count": blocked,
                "question_slugs": list(questions.get("blocked_slugs", [])),
                "request_ids": list(questions.get("blocked_open_request_ids", [])),
            }
        )
    if not structured:
        structured.append({"code": verdict, "severity": verdict, "message": reasons[0] if reasons else verdict})
    return structured


def readiness_section(
    smoke: dict[str, Any],
    questions: dict[str, Any],
    sources: dict[str, Any],
    lint: dict[str, Any],
    operational_debt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    attention = False

    if smoke.get("error"):
        attention = True
        reasons.append(f"Smoke validation could not run: {smoke['error']}")
    elif not smoke.get("ok"):
        attention = True
        reasons.append(f"Smoke validation failed with {smoke.get('issues', 0)} issue(s).")

    if lint.get("error"):
        attention = True
        reasons.append(f"Lint checks could not run: {lint['error']}")
    else:
        high_issues = int(lint.get("issue_counts", {}).get("HIGH", 0) or 0)
        if high_issues:
            attention = True
            reasons.append(f"Lint reported {high_issues} HIGH issue(s).")

    debt = operational_debt if isinstance(operational_debt, dict) else {}
    warning_count = int(debt.get("warning_count", 0) or 0)
    if warning_count:
        reasons.append(f"Operational debt includes {warning_count} non-blocking warning(s).")
    deferred_count = int(debt.get("deferred_count", 0) or 0)
    if deferred_count:
        attention = True
        deferred = debt.get("deferred") if isinstance(debt.get("deferred"), dict) else {}
        reasons.append(
            f"Operational debt includes {deferred_count} deferred item(s) "
            f"(questions={int(deferred.get('questions', 0) or 0)}, "
            f"candidates={int(deferred.get('candidates', 0) or 0)}, "
            f"sources={int(deferred.get('sources', 0) or 0)}); explicit disposition is required."
        )

    actionable = int(questions.get("actionable", 0) or 0)
    human_review = int(questions.get("human_review", 0) or 0)
    blocked = int(questions.get("blocked", 0) or 0)
    blocked_missing_requests = int(questions.get("blocked_questions_missing_requests", 0) or 0)

    if blocked_missing_requests:
        attention = True
        reasons.append(
            f"{blocked_missing_requests} blocked question(s) lack valid open source request links: "
            f"{summarize_slugs(questions.get('blocked_slugs_missing_requests', []))}."
        )
        missing_ids = questions.get("missing_blocking_request_ids") if isinstance(questions.get("missing_blocking_request_ids"), list) else []
        if missing_ids:
            reasons.append(f"Missing blocking source request id(s): {summarize_slugs(missing_ids)}.")

    if human_review:
        attention = True
        reasons.append(
            f"{human_review} question(s) require human review approval: "
            f"{summarize_slugs(questions.get('human_review_slugs', []))}."
        )

    if attention:
        verdict = VERDICT_ATTENTION_REQUIRED
        if actionable:
            reasons.append(
                f"{actionable} actionable question(s) remain: "
                f"{summarize_slugs(questions.get('actionable_slugs', []))}."
            )
            append_claim_readiness_reasons(reasons, questions)
    elif actionable:
        verdict = VERDICT_IN_PROGRESS
        reasons.append(
            f"{actionable} actionable question(s) remain: "
            f"{summarize_slugs(questions.get('actionable_slugs', []))}."
        )
        append_claim_readiness_reasons(reasons, questions)
    elif blocked:
        verdict = VERDICT_BLOCKED_ON_SOURCES
        reasons.append(
            f"{blocked} question(s) blocked on missing evidence: "
            f"{summarize_slugs(questions.get('blocked_slugs', []))}."
        )
        linked_open_ids = questions.get("blocked_open_request_ids") if isinstance(questions.get("blocked_open_request_ids"), list) else []
        if linked_open_ids:
            reasons.append(
                f"{len(linked_open_ids)} linked open source request(s) await delivery: "
                f"{summarize_slugs(linked_open_ids)}."
            )
    else:
        verdict = VERDICT_COMPLETE
        if int(questions.get("total", 0) or 0) == 0:
            reasons.append("Question backlog is empty; no questions have been injected yet.")
        else:
            reasons.append("All questions are resolved and validation checks pass.")

    return {
        "verdict": verdict,
        "reasons": reasons,
        "verdict_reasons": structured_verdict_reasons(
            verdict,
            reasons,
            questions,
            sources,
            lint,
            operational_debt,
        ),
    }


def runner_reported_counters(
    *,
    questions_processed_this_run: int | None,
    source_requests_opened_this_run: int | None,
    releases_this_run: int | None,
    discovery_results_this_run: int | None,
    acquisition_downloads_this_run: int | None,
    github_archive_bytes_this_run: int | None,
    academic_provider_requests_this_run: int | None,
    web_downloads_this_run: int | None,
    manual_url_deliveries_this_run: int | None,
) -> dict[str, int | None]:
    return {
        "questions_processed_this_run": questions_processed_this_run,
        "source_requests_opened_this_run": source_requests_opened_this_run,
        "releases_this_run": releases_this_run,
        "discovery_results_this_run": discovery_results_this_run,
        "acquisition_downloads_this_run": acquisition_downloads_this_run,
        "github_archive_bytes_this_run": github_archive_bytes_this_run,
        "academic_provider_requests_this_run": academic_provider_requests_this_run,
        "web_downloads_this_run": web_downloads_this_run,
        "manual_url_deliveries_this_run": manual_url_deliveries_this_run,
    }


def budget_state_from_counters(run: dict[str, Any], counters: dict[str, int]) -> dict[str, Any]:
    questions_processed = counters.get("questions_processed_this_run", 0)
    source_requests_opened = counters.get("source_requests_opened_this_run", 0)
    releases = counters.get("releases_this_run", 0)
    discovery_results = counters.get("discovery_results_this_run", 0)
    acquisition_downloads = counters.get("acquisition_downloads_this_run", 0)
    github_archive_bytes = counters.get("github_archive_bytes_this_run", 0)
    academic_provider_requests = counters.get("academic_provider_requests_this_run", 0)
    web_downloads = counters.get("web_downloads_this_run", 0)
    manual_url_deliveries = counters.get("manual_url_deliveries_this_run", 0)
    questions_remaining = max(0, int(run["max_questions_per_run"]) - questions_processed)
    source_requests_remaining = max(0, int(run["max_source_requests_per_run"]) - source_requests_opened)
    releases_remaining = max(0, int(run["max_releases_per_run"]) - releases)
    discovery_results_remaining = max(0, int(run["max_discovery_results_per_run"]) - discovery_results)
    acquisition_downloads_remaining = max(0, int(run["max_acquisition_downloads_per_run"]) - acquisition_downloads)
    github_archive_bytes_remaining = max(0, int(run["max_github_archive_bytes_per_run"]) - github_archive_bytes)
    academic_provider_requests_remaining = max(
        0,
        int(run["max_academic_provider_requests_per_run"]) - academic_provider_requests,
    )
    web_downloads_remaining = max(0, int(run["max_web_downloads_per_run"]) - web_downloads)
    manual_url_deliveries_remaining = max(0, int(run["max_manual_url_deliveries_per_run"]) - manual_url_deliveries)
    stop_reasons: list[str] = []
    if questions_remaining == 0:
        stop_reasons.append("questions_exhausted")
    if source_requests_remaining == 0:
        stop_reasons.append("source_requests_exhausted")
    if releases_remaining == 0:
        stop_reasons.append("releases_exhausted")
    if discovery_results_remaining == 0:
        stop_reasons.append("discovery_results_exhausted")
    if acquisition_downloads_remaining == 0:
        stop_reasons.append("acquisition_downloads_exhausted")
    if github_archive_bytes_remaining == 0:
        stop_reasons.append("github_archive_bytes_exhausted")
    if academic_provider_requests_remaining == 0:
        stop_reasons.append("academic_provider_requests_exhausted")
    if web_downloads_remaining == 0:
        stop_reasons.append("web_downloads_exhausted")
    if manual_url_deliveries_remaining == 0:
        stop_reasons.append("manual_url_deliveries_exhausted")
    return {
        "questions_processed_this_run": questions_processed,
        "questions_remaining_this_run": questions_remaining,
        "source_requests_opened_this_run": source_requests_opened,
        "source_requests_remaining_this_run": source_requests_remaining,
        "releases_this_run": releases,
        "releases_remaining_this_run": releases_remaining,
        "discovery_results_this_run": discovery_results,
        "discovery_results_remaining_this_run": discovery_results_remaining,
        "acquisition_downloads_this_run": acquisition_downloads,
        "acquisition_downloads_remaining_this_run": acquisition_downloads_remaining,
        "github_archive_bytes_this_run": github_archive_bytes,
        "github_archive_bytes_remaining_this_run": github_archive_bytes_remaining,
        "academic_provider_requests_this_run": academic_provider_requests,
        "academic_provider_requests_remaining_this_run": academic_provider_requests_remaining,
        "web_downloads_this_run": web_downloads,
        "web_downloads_remaining_this_run": web_downloads_remaining,
        "manual_url_deliveries_this_run": manual_url_deliveries,
        "manual_url_deliveries_remaining_this_run": manual_url_deliveries_remaining,
        "stop_reasons": stop_reasons,
        "should_stop": bool(stop_reasons),
    }


def budget_state_section(
    run: dict[str, Any],
    questions_processed_this_run: int | None,
    source_requests_opened_this_run: int | None,
    releases_this_run: int | None,
    discovery_results_this_run: int | None,
    acquisition_downloads_this_run: int | None,
    github_archive_bytes_this_run: int | None,
    academic_provider_requests_this_run: int | None,
    web_downloads_this_run: int | None,
    manual_url_deliveries_this_run: int | None,
    *,
    artifact_counters: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    runner_reported = runner_reported_counters(
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
    provided_runner = {key: int(value or 0) for key, value in runner_reported.items() if value is not None}
    if artifact_counters is None and not provided_runner:
        return None

    if artifact_counters is None:
        counters = {key: int(runner_reported.get(key) or 0) for key in BUDGET_COUNTER_FIELDS}
        return budget_state_from_counters(run, counters)

    counters = {key: int(artifact_counters.get(key, 0) or 0) for key in BUDGET_COUNTER_FIELDS}
    budget_state = budget_state_from_counters(run, counters)
    budget_state["counter_source"] = "artifact_derived"
    if provided_runner:
        budget_state["runner_reported"] = provided_runner
        divergence: list[dict[str, int | str]] = []
        for key, reported in provided_runner.items():
            derived = int(counters.get(key, 0) or 0)
            if reported != derived:
                divergence.append({"counter": key, "runner_reported": reported, "artifact_derived": derived})
        budget_state["counter_divergence"] = divergence
    else:
        budget_state["runner_reported"] = {}
        budget_state["counter_divergence"] = []
    return budget_state


def timestamp_after_start(value: Any, started_at: datetime | None) -> bool:
    if started_at is None:
        return True
    parsed = parse_timestamp(value)
    return parsed is not None and parsed >= started_at


def file_mtime_after_start(path: Path, started_at: datetime | None) -> bool:
    if started_at is None:
        return True
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return mtime >= started_at


def artifact_budget_counters(
    project_root: Path,
    config: dict[str, Any],
    run_controller: dict[str, Any],
) -> dict[str, int]:
    counters = {key: 0 for key in BUDGET_COUNTER_FIELDS}
    started_at = parse_timestamp(run_controller.get("started_at"))

    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    terminal_statuses = {"answered", "human_review", "blocked", "deferred", "rejected"}
    for record in question_status.collect_questions(questions_dir):
        status = record.get("status")
        path_name = record.get("path")
        if status in terminal_statuses and isinstance(path_name, str):
            if file_mtime_after_start(questions_dir / path_name, started_at):
                counters["questions_processed_this_run"] += 1

    source_requests = load_sibling_module("source_requests")
    try:
        requests = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit:
        requests = []
    retained_request_keys: set[str] = set()
    for record in requests:
        if not timestamp_after_start(record.get("created_at"), started_at):
            continue
        request_id = record.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            identity = f"id:{request_id.strip()}"
        else:
            identity = json.dumps(
                {
                    "kind": record.get("kind"),
                    "query": record.get("query_or_identifier"),
                    "created_at": record.get("created_at"),
                },
                sort_keys=True,
                default=str,
            )
        retained_request_keys.add(identity)
    counters["source_requests_opened_this_run"] = len(retained_request_keys)

    discover_sources = load_sibling_module("discover_sources")
    candidates_path = discover_sources.candidate_store_path(project_root, config)
    retained_candidate_keys: set[str] = set()
    if candidates_path.is_file():
        for line in candidates_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or not timestamp_after_start(
                record.get("discovered_at") or record.get("created_at") or record.get("updated_at"),
                started_at,
            ):
                continue
            candidate_id = record.get("candidate_id")
            if isinstance(candidate_id, str) and candidate_id.strip():
                identity = f"id:{candidate_id.strip()}"
            else:
                identity = json.dumps(
                    {
                        "provider": record.get("provider"),
                        "url": record.get("url") or record.get("origin_url"),
                        "title": record.get("title"),
                    },
                    sort_keys=True,
                    default=str,
                )
            retained_candidate_keys.add(identity)
    counters["discovery_results_this_run"] = len(retained_candidate_keys)

    query_index = load_sibling_module("query_index")
    try:
        manifest = query_index.manifest_path(project_root, config)
    except SystemExit:
        manifest = None
    if manifest is not None and manifest.is_file():
        retained_acquisition_keys: set[str] = set()
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
            bound_run_id = provenance.get("acquisition_run_id")
            selected_run_id = run_controller.get("run_id")
            if isinstance(bound_run_id, str) and bound_run_id.strip():
                within_run = bound_run_id.strip() == selected_run_id
            else:
                within_run = timestamp_after_start(
                    provenance.get("retrieved_at") or record.get("detected_at") or record.get("created_at"),
                    started_at,
                )
            if not within_run:
                continue
            retrieved_by = str(provenance.get("retrieved_by") or "")
            if not retrieved_by:
                continue
            record_id = record.get("id")
            if isinstance(record_id, str) and record_id.strip():
                acquisition_key = f"id:{record_id.strip()}"
            else:
                acquisition_key = json.dumps(
                    {
                        "raw_paths": record.get("raw_paths"),
                        "checksum": provenance.get("checksum"),
                        "origin_url": provenance.get("origin_url"),
                    },
                    sort_keys=True,
                    default=str,
                )
            if acquisition_key in retained_acquisition_keys:
                continue
            retained_acquisition_keys.add(acquisition_key)
            counters["acquisition_downloads_this_run"] += 1
            lowered = retrieved_by.casefold()
            if "openalex" in lowered or "arxiv" in lowered:
                counters["academic_provider_requests_this_run"] += 1
            if "fetch_sources.py/web" in lowered or lowered.endswith("/web") or "/web/" in lowered:
                counters["web_downloads_this_run"] += 1
            if "manual" in lowered:
                counters["manual_url_deliveries_this_run"] += 1
            artifact_kind = provenance.get("repository_artifact_kind") or record.get("repository_artifact_kind")
            if artifact_kind == "source_archive":
                byte_count = provenance.get("byte_count", record.get("byte_count"))
                if isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count >= 0:
                    counters["github_archive_bytes_this_run"] += byte_count
                else:
                    raw_paths = record.get("raw_paths") if isinstance(record.get("raw_paths"), list) else []
                    for raw_path in raw_paths:
                        if isinstance(raw_path, str):
                            try:
                                counters["github_archive_bytes_this_run"] += (project_root / raw_path).stat().st_size
                            except OSError:
                                continue

    counters["releases_this_run"] = release_events_since(project_root / "log.md", started_at)
    return counters


def release_events_since(log_path: Path, started_at: datetime | None) -> int:
    if not log_path.is_file():
        return 0
    count = 0
    retained_events: set[tuple[str | None, str]] = set()
    current_date: datetime | None = None
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("## [") and "]" in line:
            date_text = line.split("]", 1)[0].removeprefix("## [")
            current_date = parse_timestamp(f"{date_text}T00:00:00Z")
        if "Question release" in line and "(released)" in line:
            event_key = (current_date.date().isoformat() if current_date is not None else None, line.strip())
            if (
                event_key not in retained_events
                and (started_at is None or current_date is None or current_date.date() >= started_at.date())
            ):
                retained_events.add(event_key)
                count += 1
    return count


def is_run_controller_error(error: BaseException) -> bool:
    return all(hasattr(error, attr) for attr in ("error_code", "exit_code", "details"))


def summarize_run_controller_state(
    project_root: Path,
    run_controller: ModuleType,
    document: dict[str, Any],
    *,
    selection: str,
    stale_threshold_hours: int,
) -> dict[str, Any]:
    state = document.get("state") if isinstance(document.get("state"), dict) else {}
    current = state.get("current")
    terminal = current in run_controller.TERMINAL_STATES
    failure_records = document.get("failure_records") if isinstance(document.get("failure_records"), list) else []
    run_id = document.get("run_id")
    run_state_path = (
        run_controller.relative_workspace_path(project_root, run_controller.run_state_path(project_root, run_id))
        if isinstance(run_id, str)
        else None
    )
    last_event_at = run_controller.latest_event_at(project_root, run_id) if isinstance(run_id, str) else None
    staleness = run_controller.run_staleness(project_root, document, float(stale_threshold_hours))
    return {
        "present": True,
        "selection": selection,
        "run_id": run_id,
        "started_at": document.get("started_at"),
        "state": current,
        "terminal": terminal,
        "final_verdict": document.get("final_verdict"),
        "blocking_reason": state.get("blocking_reason"),
        "updated_at": document.get("updated_at"),
        "last_heartbeat_at": document.get("last_heartbeat_at"),
        "last_event_at": last_event_at,
        "liveness_at": staleness.get("liveness_at"),
        "stale_threshold_hours": stale_threshold_hours,
        "stale_age_hours": staleness.get("stale_age_hours"),
        "stale": bool(staleness.get("stale")) and not terminal,
        "allowed_next_states": list(state.get("allowed_next_states") or []),
        "candidate_counts": document.get("candidate_counts") if isinstance(document.get("candidate_counts"), dict) else {},
        "coverage_counts": document.get("coverage_counts") if isinstance(document.get("coverage_counts"), dict) else {},
        "budget_state": document.get("budget_state") if isinstance(document.get("budget_state"), dict) else {},
        "budget_overrides": document.get("budget_overrides") if isinstance(document.get("budget_overrides"), dict) else {},
        "failure_count": len(failure_records),
        "run_state_path": run_state_path,
    }


def sort_run_documents(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        documents,
        key=lambda document: (
            str(document.get("updated_at") or ""),
            str(document.get("started_at") or ""),
            str(document.get("run_id") or ""),
        ),
        reverse=True,
    )


def run_controller_section(project_root: Path, run_id: str | None, stale_threshold_hours: int) -> dict[str, Any]:
    run_controller = load_sibling_module("run_controller")
    if run_id is not None:
        validated_run_id = run_controller.validate_run_id(run_id)
        document = run_controller.load_run_state(project_root, validated_run_id)
        return summarize_run_controller_state(
            project_root,
            run_controller,
            document,
            selection="explicit",
            stale_threshold_hours=stale_threshold_hours,
        )

    root = run_controller.runs_root(project_root)
    if not root.is_dir():
        return {"present": False, "selection": "none"}

    documents: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / run_controller.RUN_STATE_FILENAME).is_file():
            continue
        documents.append(run_controller.load_run_state(project_root, child.name))
    if not documents:
        return {"present": False, "selection": "none"}

    active = [
        document
        for document in documents
        if document.get("state", {}).get("current") not in run_controller.TERMINAL_STATES
    ]
    if active:
        return summarize_run_controller_state(
            project_root,
            run_controller,
            sort_run_documents(active)[0],
            selection="newest_active",
            stale_threshold_hours=stale_threshold_hours,
        )
    return summarize_run_controller_state(
        project_root,
        run_controller,
        sort_run_documents(documents)[0],
        selection="newest_terminal",
        stale_threshold_hours=stale_threshold_hours,
    )


def summarize_orchestration_session(
    project_root: Path,
    document: dict[str, Any],
    *,
    selection: str,
) -> dict[str, Any]:
    """Return the bounded read-only parent-session summary exposed to agents."""
    orchestration_id = document.get("orchestration_id")
    status = document.get("status")
    terminal = status in {"complete", "blocked_on_sources", "no_ship", "failed"}
    session_file = (
        project_root / "runs" / "orchestrations" / orchestration_id / "session.json"
        if isinstance(orchestration_id, str)
        else None
    )
    return {
        "present": True,
        "selection": selection,
        "orchestration_id": orchestration_id,
        "status": status,
        "phase": document.get("phase"),
        "terminal": terminal,
        "verdict": document.get("verdict"),
        "pause_reason": document.get("pause_reason"),
        "pending_action_id": document.get("pending_action_id"),
        "active_run_id": document.get("active_run_id"),
        "child_run_ids": list(document.get("child_run_ids") or []),
        "action_count": int(document.get("action_count", 0) or 0),
        "completed_action_count": int(document.get("completed_action_count", 0) or 0),
        "started_at": document.get("started_at"),
        "updated_at": document.get("updated_at"),
        "completed_at": document.get("completed_at"),
        "session_path": relative_workspace_path(project_root, session_file) if session_file is not None else None,
    }


def orchestration_section(project_root: Path) -> dict[str, Any]:
    """Select the newest non-terminal orchestration, otherwise newest terminal."""
    root = project_root / "runs" / "orchestrations"
    if not root.is_dir():
        return {"present": False, "selection": "none"}
    documents: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        path = child / "session.json"
        if not child.is_dir() or not path.is_file():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(document, dict)
            and document.get("schema_version") == "1.0"
            and document.get("artifact_type") == "orchestration_session"
            and document.get("orchestration_id") == child.name
        ):
            documents.append(document)
    if not documents:
        return {"present": False, "selection": "none"}
    documents.sort(
        key=lambda item: (str(item.get("updated_at") or ""), str(item.get("orchestration_id") or "")),
        reverse=True,
    )
    terminal_statuses = {"complete", "blocked_on_sources", "no_ship", "failed"}
    active = [item for item in documents if item.get("status") not in terminal_statuses]
    if active:
        return summarize_orchestration_session(project_root, active[0], selection="newest_active")
    return summarize_orchestration_session(project_root, documents[0], selection="newest_terminal")


def build_status_document(
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
    run_id: str | None = None,
) -> dict[str, Any]:
    workspace_health = evaluate_workspace_health(project_root)
    if not workspace_health["materially_valid"]:
        reasons = [
            f"{item['code']}: {item['message']}"
            for item in workspace_health["findings"]
            if item["readiness_effect"] == "invalid"
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": timestamp_utc(),
            "project": {
                **{field: None for field in PROJECT_IDENTITY_FIELDS},
                "handoff": None,
                "handoff_signature": None,
                "handoff_signature_status": "unavailable",
            },
            "contract": {field: None for field in CONTRACT_FIELDS},
            "run": run_section({}),
            "run_controller": {"present": False, "selection": "none"},
            "orchestration": {"present": False, "selection": "none"},
            "smoke": {
                "ok": False,
                "issues": len(reasons),
                "by_severity": {"HIGH": len(reasons)},
                "error": "Shared workspace health rejected the workspace contract.",
            },
            "questions": {},
            "coverage": {},
            "intake": {},
            "candidates": {},
            "sources": {},
            "lint": {
                "error": "Shared workspace health rejected the workspace contract.",
                "issue_counts": {"HIGH": len(reasons)},
            },
            "readiness": {
                "verdict": VERDICT_ATTENTION_REQUIRED,
                "reasons": reasons,
                "verdict_reasons": [dict(item) for item in workspace_health["findings"]],
            },
            "workspace_health": workspace_health,
        }
    config = load_yaml_mapping(project_root / "research.yml", "research.yml")
    metadata = load_yaml_mapping(project_root / "workspace-system.yml", "workspace-system.yml")

    run = run_section(config)
    run_controller = run_controller_section(project_root, run_id, int(run["stale_run_threshold_hours"]))
    smoke = smoke_section(project_root)
    questions = questions_section(project_root, config)
    coverage = coverage_section(project_root, config)
    intake = intake_section(project_root, questions)
    candidates = candidate_section(project_root, config)
    sources = sources_section(project_root, config)
    lint = lint_section(project_root, config)
    operational_debt = operational_debt_section(questions, candidates, sources, lint)
    readiness = readiness_section(smoke, questions, sources, lint, operational_debt)
    readiness["operational_debt"] = operational_debt
    artifact_counters = (
        artifact_budget_counters(project_root, config, run_controller)
        if run_controller.get("present")
        else None
    )
    budget_state = budget_state_section(
        run,
        questions_processed_this_run,
        source_requests_opened_this_run,
        releases_this_run,
        discovery_results_this_run,
        acquisition_downloads_this_run,
        github_archive_bytes_this_run,
        academic_provider_requests_this_run,
        web_downloads_this_run,
        manual_url_deliveries_this_run,
        artifact_counters=artifact_counters,
    )
    if budget_state is not None:
        readiness["budget_state"] = budget_state
    public_questions = {key: value for key, value in questions.items() if not key.startswith("_")}

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_utc(),
        "project": project_section(config, project_root),
        "contract": contract_section(metadata),
        "run": run,
        "run_controller": run_controller,
        "orchestration": orchestration_section(project_root),
        "smoke": smoke,
        "questions": public_questions,
        "coverage": coverage,
        "intake": intake,
        "candidates": candidates,
        "sources": sources,
        "lint": lint,
        "readiness": readiness,
        "workspace_health": workspace_health,
    }


def render_text(document: dict[str, Any]) -> str:
    workspace_health = document.get("workspace_health")
    if isinstance(workspace_health, dict) and not workspace_health.get("materially_valid", False):
        lines = [
            "Workspace Status Report",
            "=======================",
            f"Project root: {workspace_health.get('project_root')}",
            f"Workspace health: {workspace_health.get('status')}",
            "",
        ]
        for item in workspace_health.get("findings", []):
            lines.append(f"- {item['severity']} {item['code']}: {item['message']}")
            lines.append(f"  Remediation: {item['remediation']}")
        lines.append("")
        return "\n".join(lines)
    project = document["project"]
    contract = document["contract"]
    run_controller = document["run_controller"]
    orchestration = document.get("orchestration") if isinstance(document.get("orchestration"), dict) else {}
    smoke = document["smoke"]
    questions = document["questions"]
    coverage = document["coverage"]
    intake = document["intake"]
    candidates = document["candidates"]
    sources = document["sources"]
    lint = document["lint"]
    readiness = document["readiness"]

    lines = [
        "Workspace Status Report",
        "=======================",
        f"Project: {project.get('name') or '(unset)'}",
        f"Starter version: {contract.get('starter_version') or 'unknown'}"
        f" (contract {contract.get('compatible_research_yml_contract') or 'unknown'})",
    ]
    handoff = project.get("handoff")
    if isinstance(handoff, dict) and handoff:
        handoff_summary = ", ".join(f"{key}: {value}" for key, value in sorted(handoff.items()))
        lines.append(f"Handoff: {handoff_summary}")
    run = document.get("run", {})
    lines.append(
        f"Run budgets: {run.get('max_questions_per_run')} question(s), "
        f"{run.get('max_source_requests_per_run')} source request(s), "
        f"{run.get('max_releases_per_run')} release(s) per run "
        f"(claim staleness window: {run.get('claim_staleness_hours')}h; "
        f"run stale threshold: {run.get('stale_run_threshold_hours')}h)"
    )
    lines.append(
        f"Acquisition budgets: {run.get('max_discovery_results_per_run')} discovery result(s), "
        f"{run.get('max_acquisition_downloads_per_run')} download(s), "
        f"{run.get('max_github_archive_bytes_per_run')} GitHub archive byte(s), "
        f"{run.get('max_academic_provider_requests_per_run')} academic provider request(s), "
        f"{run.get('max_web_downloads_per_run')} web download(s), "
        f"{run.get('max_manual_url_deliveries_per_run')} manual URL deliveries per run"
    )
    if run_controller.get("present"):
        terminal_suffix = " terminal" if run_controller.get("terminal") else ""
        lines.append(
            f"Run controller: {run_controller.get('run_id')} {run_controller.get('state')}"
            f"{terminal_suffix} ({run_controller.get('selection')})"
        )
    if orchestration.get("present"):
        terminal_suffix = " terminal" if orchestration.get("terminal") else ""
        lines.append(
            f"Orchestration: {orchestration.get('orchestration_id')} {orchestration.get('status')}"
            f"/{orchestration.get('phase')}{terminal_suffix} ({orchestration.get('selection')})"
        )
    if smoke.get("error"):
        lines.append(f"Smoke validation: error ({smoke['error']})")
    else:
        lines.append(f"Smoke validation: {'passed' if smoke.get('ok') else 'failed'} ({smoke.get('issues', 0)} issue(s))")
    status_summary = ", ".join(f"{status}: {count}" for status, count in questions.get("by_status", {}).items()) or "none"
    lines.append(
        f"Questions: total {questions.get('total', 0)} ({status_summary}); "
        f"claimed {questions.get('claimed', 0)}, stale {len(questions.get('stale_claim_slugs', []))}"
    )
    lines.append(
        f"Coverage: {coverage.get('manifests_total', 0)} manifest(s), "
        f"{coverage.get('required_questions', 0)} required answered question(s), "
        f"passed {coverage.get('passed', 0)}, blocked {coverage.get('blocked', 0)}, "
        f"missing {coverage.get('missing', 0)}, invalid {coverage.get('invalid', 0)}"
    )
    lines.append(
        f"Intake: open questions {intake.get('open_questions_total', 0)}, "
        f"{intake.get('questions_created_last_hour', 0)} created in the last hour "
        f"across {intake.get('batches_last_hour', 0)} batch(es)"
    )
    candidate_summary = (
        f"Candidates: total {candidates.get('total', 0)}, "
        f"selected {candidates.get('selection', {}).get('selected', 0)}, "
        f"rejected {candidates.get('rejections', {}).get('total', 0)}, "
        f"fetched {candidates.get('by_status', {}).get('fetched', 0)}"
    )
    if candidates.get("invalid_records", 0):
        candidate_summary += f", invalid {candidates.get('invalid_records', 0)}"
    lines.append(candidate_summary)
    lines.append(
        f"Sources: {sources.get('manifest_records', 0)} manifest record(s), "
        f"{sources.get('unnormalized', 0)} not yet normalized, "
        f"{sources.get('requests_open', 0)} open source request(s)"
        + (f", {sources.get('needs_ocr', 0)} awaiting OCR" if sources.get("needs_ocr", 0) else "")
    )
    curation = sources.get("curation") if isinstance(sources.get("curation"), dict) else {}
    lines.append(
        f"Curation: automated web {curation.get('automated_web_records', 0)}, "
        f"cited {curation.get('cited_automated_web_records', 0)}, "
        f"missing terms/license {curation.get('missing_terms_license', 0)}, "
        f"missing notes {curation.get('missing_source_note', 0)}, "
        f"missing origin URL {curation.get('missing_origin_url', 0)}, "
        f"missing checksum {curation.get('missing_checksum', 0)}, "
        f"missing candidate id {curation.get('missing_candidate_id', 0)}"
    )
    if lint.get("error"):
        lines.append(f"Lint: error ({lint['error']})")
    else:
        lint_summary = ", ".join(f"{level}: {count}" for level, count in lint.get("issue_counts", {}).items()) or "none"
        lines.append(f"Lint issues: {lint_summary}")
    budget_state = readiness.get("budget_state") if isinstance(readiness.get("budget_state"), dict) else None
    if budget_state is not None:
        lines.append(
            f"Run budget state: questions remaining {budget_state.get('questions_remaining_this_run')}, "
            f"source requests remaining {budget_state.get('source_requests_remaining_this_run')}, "
            f"releases remaining {budget_state.get('releases_remaining_this_run')}, "
            f"discovery results remaining {budget_state.get('discovery_results_remaining_this_run')}, "
            f"acquisition downloads remaining {budget_state.get('acquisition_downloads_remaining_this_run')}, "
            f"GitHub archive bytes remaining {budget_state.get('github_archive_bytes_remaining_this_run')}, "
            f"academic provider requests remaining {budget_state.get('academic_provider_requests_remaining_this_run')}, "
            f"web downloads remaining {budget_state.get('web_downloads_remaining_this_run')}, "
            f"manual URL deliveries remaining {budget_state.get('manual_url_deliveries_remaining_this_run')}, "
            f"should_stop {str(bool(budget_state.get('should_stop'))).lower()}"
        )
    operational_debt = (
        readiness.get("operational_debt")
        if isinstance(readiness.get("operational_debt"), dict)
        else None
    )
    if operational_debt is not None:
        deferred = (
            operational_debt.get("deferred")
            if isinstance(operational_debt.get("deferred"), dict)
            else {}
        )
        lines.append(
            f"Operational debt: warnings {operational_debt.get('warning_count', 0)}, "
            f"deferred {operational_debt.get('deferred_count', 0)} "
            f"(questions={deferred.get('questions', 0)}, "
            f"candidates={deferred.get('candidates', 0)}, sources={deferred.get('sources', 0)}), "
            f"blocks_completion {str(bool(operational_debt.get('blocks_completion'))).lower()}"
        )
    lines.append("")
    lines.append(f"Readiness verdict: {readiness['verdict']}")
    for reason in readiness["reasons"]:
        lines.append(f"- {reason}")
    return "\n".join(lines).rstrip() + "\n"


LOG_HEADER = "# Research Wiki Activity Log\n\n"


def append_log_entry(log_path: Path, entry: str) -> None:
    """Append a rendered log entry under the shared workspace lock."""
    lock_path = log_path.parent / ".locks" / "log.lock"
    with workspace_lock(lock_path, purpose="activity log append"):
        with log_path.open("a+", encoding="utf-8") as handle:
            handle.seek(0)
            content = handle.read()
            if not content:
                prefix = LOG_HEADER
            elif content.endswith("\n\n"):
                prefix = ""
            elif content.endswith("\n"):
                prefix = "\n"
            else:
                prefix = "\n\n"
            handle.seek(0, 2)
            handle.write(prefix + entry + "\n")


def render_log_entry(document: dict[str, Any]) -> str:
    date_text = datetime.now(timezone.utc).date().isoformat()
    questions = document["questions"]
    coverage = document["coverage"]
    return (
        f"## [{date_text}] status | Workspace status report\n\n"
        f"- verdict: {document['readiness']['verdict']}\n"
        f"- questions: total {questions.get('total', 0)}, actionable {questions.get('actionable', 0)}, "
        f"human_review {questions.get('human_review', 0)}, blocked {questions.get('blocked', 0)}, "
        f"answered {questions.get('answered', 0)}\n"
        f"- coverage: manifests {coverage.get('manifests_total', 0)}, required {coverage.get('required_questions', 0)}, "
        f"passed {coverage.get('passed', 0)}, blocked {coverage.get('blocked', 0)}, missing {coverage.get('missing', 0)}, "
        f"invalid {coverage.get('invalid', 0)}\n"
        f"- manifest records: {document['sources'].get('manifest_records', 0)}\n"
        f"- unnormalized sources: {document['sources'].get('unnormalized', 0)}\n"
    )


def status_cache_path(project_root: Path) -> Path:
    return project_root / CACHE_DIR / STATUS_CACHE_FILENAME


def cache_key_paths(project_root: Path) -> list[Path]:
    roots = [
        project_root / "research.yml",
        project_root / "workspace-system.yml",
        project_root / "AGENTS.md",
        project_root / "index.md",
        project_root / "log.md",
        project_root / "runs",
        project_root / "sources",
        project_root / "wiki",
        project_root / "raw",
    ]
    paths: list[Path] = []
    for root in roots:
        if root.is_file():
            paths.append(root)
        elif root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(project_root)
                except ValueError:
                    continue
                if relative.parts and relative.parts[0] == CACHE_DIR:
                    continue
                paths.append(path)
    return sorted(paths)


def status_cache_key(project_root: Path, options: dict[str, Any]) -> str:
    records: list[dict[str, Any]] = []
    for path in cache_key_paths(project_root):
        try:
            stat = path.stat()
            relative = path.relative_to(project_root).as_posix()
        except OSError:
            continue
        records.append({"path": relative, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size})
    material = {
        "schema_version": SCHEMA_VERSION,
        "options": options,
        "files": records,
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_cached_status(project_root: Path, cache_key: str) -> dict[str, Any] | None:
    path = status_cache_path(project_root)
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict) or cached.get("cache_key") != cache_key:
        return None
    document = cached.get("document")
    return document if isinstance(document, dict) else None


def write_cached_status(project_root: Path, cache_key: str, document: dict[str, Any]) -> None:
    path = status_cache_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "cache_key": cache_key,
        "generated_at": timestamp_utc(),
        "document": document,
    }
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def refresh_run_controller_liveness(project_root: Path, document: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    """Recompute the wall-clock-dependent ``run_controller`` section on a cache hit.

    Run staleness is derived from ``datetime.now()`` versus the run's last
    heartbeat/event/update, not from any file the cache key hashes. A crashed
    PM stops writing to ``runs/`` entirely, so the cache key never changes and
    a naive cache hit would keep serving the ``stale`` verdict computed at the
    moment the cache was written, forever. Recomputing this section fresh on
    every call (cache hit or not) keeps liveness detection correct while still
    letting the rest of the document (which only changes when files change)
    come from the cache.
    """
    run = document.get("run") if isinstance(document.get("run"), dict) else {}
    try:
        threshold_hours = int(run.get("stale_run_threshold_hours", RUN_BUDGET_DEFAULTS["stale_run_threshold_hours"]))
    except (TypeError, ValueError):
        threshold_hours = int(RUN_BUDGET_DEFAULTS["stale_run_threshold_hours"])
    refreshed = dict(document)
    refreshed["run_controller"] = run_controller_section(project_root, run_id, threshold_hours)
    return refreshed


def cached_status_document(
    project_root: Path,
    *,
    no_cache: bool = False,
    **build_kwargs: Any,
) -> dict[str, Any]:
    if no_cache:
        return build_status_document(project_root, **build_kwargs)
    cache_key = status_cache_key(project_root, build_kwargs)
    cached = read_cached_status(project_root, cache_key)
    if cached is not None:
        return refresh_run_controller_liveness(project_root, cached, build_kwargs.get("run_id"))
    document = build_status_document(project_root, **build_kwargs)
    workspace_health = document.get("workspace_health")
    if isinstance(workspace_health, dict) and not workspace_health.get("materially_valid", False):
        return document
    write_cached_status(project_root, cache_key, document)
    return document


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        document = cached_status_document(
            project_root,
            no_cache=args.no_cache or args.append_log,
            questions_processed_this_run=args.questions_processed_this_run,
            source_requests_opened_this_run=args.source_requests_opened_this_run,
            releases_this_run=args.releases_this_run,
            discovery_results_this_run=args.discovery_results_this_run,
            acquisition_downloads_this_run=args.acquisition_downloads_this_run,
            github_archive_bytes_this_run=args.github_archive_bytes_this_run,
            academic_provider_requests_this_run=args.academic_provider_requests_this_run,
            web_downloads_this_run=args.web_downloads_this_run,
            manual_url_deliveries_this_run=args.manual_url_deliveries_this_run,
            run_id=args.run_id,
        )
    except Exception as exc:
        if is_run_controller_error(exc):
            if json_mode:
                emit_error(
                    str(exc),
                    json_mode=True,
                    error_code=exc.error_code,
                    recoverable=getattr(exc, "recoverable", None),
                    remediation=getattr(exc, "remediation", None),
                    details=getattr(exc, "details", None),
                )
            else:
                print(f"refused ({exc.error_code}): {exc}", file=sys.stderr)
            return int(exc.exit_code)
        raise
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_WORKSPACE_UNREADABLE)

    if args.format == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(document))

    workspace_health = document.get("workspace_health")
    if isinstance(workspace_health, dict) and not workspace_health.get("materially_valid", False):
        return EXIT_WORKSPACE_UNREADABLE

    if args.append_log:
        try:
            append_log_entry(project_root / "log.md", render_log_entry(document))
        except LockUnavailableError as error:
            if json_mode:
                emit_error(
                    str(error),
                    json_mode=True,
                    error_code=error.error_code,
                    details=error.details,
                )
            else:
                print(f"refused ({error.error_code}): {error}", file=sys.stderr)
            return EXIT_WORKSPACE_UNREADABLE

    if args.check_complete:
        return CHECK_COMPLETE_EXIT_CODES[document["readiness"]["verdict"]]
    return EXIT_REPORTED


if __name__ == "__main__":
    raise SystemExit(main())
