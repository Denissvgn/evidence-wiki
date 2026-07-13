#!/usr/bin/env python3
"""Generate an auditable per-run report for an unattended research run.

An orchestrator (or the ``research-run`` loop skill) captures a baseline at
run start, lets the research agent work the backlog, then generates a report
of what the run changed — without diffing the workspace:

.. code-block:: bash

    python3 scripts/run_report.py baseline --output /tmp/run-baseline.json
    # ... the agent works the backlog ...
    python3 scripts/run_report.py --baseline /tmp/run-baseline.json --agent-id agent-a

The baseline is the JSON output of ``run_report.py baseline``. For v1
compatibility, ``--baseline`` also accepts the unmodified JSON output of
``question_status.py --format json``. The baseline ``generated_at`` timestamp
defines the run window start. The report contains:

- backlog counts before (baseline) and after (current), with deltas,
- every question touched during the run: status transitions, new questions,
  and removed slugs,
- source requests opened or fulfilled during the window (by ``created_at``
  / ``updated_at`` against the window start),
- sources normalized during the window (normalized records whose
  ``normalized_at`` timestamp falls inside it), plus a separate legacy
  date-match list for older records without exact timestamps,
- the current lint issue counts by severity.

The Markdown report is written to ``docs/run-reports/run-<UTC timestamp>.md``
(directory configurable only by editing the constant; reports are generated
artifacts and belong under ``docs/``). ``--format json`` prints the full
document, including ``report_path``, to stdout; the default text mode prints
a one-line confirmation.

Exit codes:

- ``0``: report generated.
- ``2``: missing or invalid baseline, or unreadable workspace.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

if importlib.util.find_spec("yaml") is None:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to generate run reports")


SCHEMA_VERSION = "1.0"
BASELINE_DOCUMENT_TYPE = "run_report_baseline"
RUN_REPORTS_DIR = "docs/run-reports"
EXIT_OK = 0
EXIT_INVALID = 2

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_module_loader import load_workspace_module


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if raw_argv and raw_argv[0] == "baseline":
        return parse_baseline_args(raw_argv[1:])
    return parse_report_args(raw_argv)


def parse_report_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an auditable report of one unattended research run.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help=(
            "Path to a run_report.py baseline artifact captured at run start. "
            "Legacy question_status.py --format json output is also accepted. "
            "When omitted, --run-id loads the baseline path from the run controller state."
        ),
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional PM run id under runs/<run_id>; used for run metadata and baseline lookup.",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="Optional agent identifier recorded in the report.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Stdout format. Defaults to a one-line text confirmation.",
    )
    args = parser.parse_args(argv)
    args.command = "report"
    return args


def parse_baseline_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_report.py baseline",
        description="Capture a baseline snapshot for a future unattended run report.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path where the baseline JSON artifact should be written.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Stdout format. Defaults to a one-line text confirmation.",
    )
    args = parser.parse_args(argv)
    args.command = "baseline"
    return args


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so its logic is reused directly."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def parse_window_start(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_baseline(path: Path) -> tuple[dict[str, Any], datetime | None]:
    if not path.is_file():
        raise SystemExit(f"Missing baseline file: {path}")
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid baseline JSON in {path}: {exc}") from exc
    if not isinstance(baseline, dict):
        raise SystemExit(
            f"Baseline must be question_status.py --format json output or a run_report.py baseline artifact: {path}"
        )
    if baseline.get("document_type") == BASELINE_DOCUMENT_TYPE:
        question_baseline = baseline.get("question_status")
        window_value = baseline.get("generated_at")
    else:
        question_baseline = baseline
        window_value = baseline.get("generated_at")
    if not isinstance(question_baseline, dict) or not isinstance(question_baseline.get("questions"), list):
        raise SystemExit(
            f"Baseline must be question_status.py --format json output or a run_report.py baseline artifact: {path}"
        )
    return question_baseline, parse_window_start(window_value)


def is_run_controller_error(error: BaseException) -> bool:
    return all(hasattr(error, attr) for attr in ("error_code", "exit_code", "details"))


def run_controller_module() -> ModuleType:
    return load_sibling_module("run_controller")


def load_run_state(project_root: Path, run_id: str | None) -> dict[str, Any] | None:
    if run_id is None:
        return None
    run_controller = run_controller_module()
    validated_run_id = run_controller.validate_run_id(run_id)
    return run_controller.load_run_state(project_root, validated_run_id)


def run_controller_summary(document: dict[str, Any] | None) -> dict[str, Any]:
    if document is None:
        return {"present": False}
    state = document.get("state") if isinstance(document.get("state"), dict) else {}
    return {
        "present": True,
        "run_id": document.get("run_id"),
        "state": state.get("current"),
        "state_history": document.get("state_history") if isinstance(document.get("state_history"), list) else [],
        "candidate_counts": document.get("candidate_counts") if isinstance(document.get("candidate_counts"), dict) else {},
        "coverage_counts": document.get("coverage_counts") if isinstance(document.get("coverage_counts"), dict) else {},
        "budget_state": document.get("budget_state") if isinstance(document.get("budget_state"), dict) else {},
        "budget_overrides": document.get("budget_overrides") if isinstance(document.get("budget_overrides"), dict) else {},
        "final_verdict": document.get("final_verdict"),
    }


def official_source_summary(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    workspace_status = load_sibling_module("workspace_status")
    source_requests = load_sibling_module("source_requests")
    status = workspace_status.build_status_document(project_root)
    try:
        request_records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit:
        request_records = []
    open_requests = [
        {
            "request_id": record.get("request_id"),
            "status": record.get("status"),
            "query_or_identifier": record.get("query_or_identifier"),
            "rationale": record.get("rationale"),
            "question_slugs": record.get("question_slugs") if isinstance(record.get("question_slugs"), list) else [],
        }
        for record in request_records
        if record.get("status") == "open"
    ]
    return {
        "final_verdict": status.get("readiness", {}).get("verdict"),
        "verdict_reasons": status.get("readiness", {}).get("verdict_reasons", []),
        "blocked_request_ids": status.get("questions", {}).get("blocked_open_request_ids", []),
        "open_requests": open_requests,
        "candidate_summary": status.get("candidates", {}) if isinstance(status.get("candidates"), dict) else {},
        "coverage_summary": status.get("coverage", {}) if isinstance(status.get("coverage"), dict) else {},
        "source_summary": status.get("sources", {}) if isinstance(status.get("sources"), dict) else {},
        "budget_state": status.get("readiness", {}).get("budget_state", {}),
    }


def baseline_path_from_run_state(project_root: Path, document: dict[str, Any]) -> Path:
    baseline = document.get("workspace_baseline") if isinstance(document.get("workspace_baseline"), dict) else {}
    raw_path = baseline.get("run_report_baseline_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SystemExit(
            "Missing baseline file: run-state workspace_baseline.run_report_baseline_path is absent."
        )
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root / path
    return path


def resolve_baseline_path(project_root: Path, baseline: str | None, run_state: dict[str, Any] | None) -> Path:
    if baseline is not None:
        return Path(baseline).expanduser().resolve()
    if run_state is not None:
        return baseline_path_from_run_state(project_root, run_state).expanduser().resolve()
    raise SystemExit("Missing baseline file: pass --baseline or --run-id.")


def question_state_map(report: dict[str, Any]) -> dict[str, str]:
    states: dict[str, str] = {}
    for record in report.get("questions", []):
        if not isinstance(record, dict):
            continue
        slug = record.get("slug")
        status = record.get("status")
        if isinstance(slug, str) and slug:
            states[slug] = status if isinstance(status, str) else "unknown"
    return states


def diff_questions(
    baseline_states: dict[str, str],
    current_states: dict[str, str],
    current_records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    touched: list[dict[str, Any]] = []
    for slug in sorted(set(baseline_states) | set(current_states)):
        before = baseline_states.get(slug)
        after = current_states.get(slug)
        if before == after:
            continue
        if before is None:
            change = "added"
        elif after is None:
            change = "removed"
        else:
            change = "status_changed"
        entry: dict[str, Any] = {
            "slug": slug,
            "change": change,
            "status_before": before,
            "status_after": after,
        }
        record = current_records.get(slug, {})
        for field in ("priority", "answer_page", "blocked_reason", "claimed_by"):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                entry[field] = value.strip()
        touched.append(entry)
    return touched


def in_window(value: Any, window_start: datetime | None) -> bool:
    if window_start is None:
        return False
    parsed = parse_window_start(value)
    return parsed is not None and parsed >= window_start


def parse_frontmatter_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def source_identifier(path: Path, frontmatter: dict[str, Any]) -> str:
    source_id = frontmatter.get("source_id")
    return source_id if isinstance(source_id, str) and source_id else path.stem


def source_request_activity(
    project_root: Path,
    config: dict[str, Any],
    window_start: datetime | None,
) -> dict[str, Any]:
    source_requests = load_sibling_module("source_requests")
    try:
        records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit:
        records = []
    opened = [
        record["request_id"]
        for record in records
        if isinstance(record.get("request_id"), str) and in_window(record.get("created_at"), window_start)
    ]
    fulfilled = [
        record["request_id"]
        for record in records
        if isinstance(record.get("request_id"), str)
        and record.get("status") == "fulfilled"
        and in_window(record.get("updated_at"), window_start)
    ]
    return {
        "opened": sorted(opened),
        "fulfilled": sorted(fulfilled),
        "open_total": sum(1 for record in records if record.get("status") == "open"),
    }


def normalized_during_window(
    project_root: Path,
    config: dict[str, Any],
    window_start: datetime | None,
) -> dict[str, list[str]]:
    """Normalized records inside the run window, split by timestamp precision."""
    empty = {"precise": [], "legacy_date_match": []}
    if window_start is None:
        return empty
    query_index = load_sibling_module("query_index")
    try:
        normalized_root = query_index.normalized_dir(project_root, config)
    except SystemExit:
        return empty
    if not normalized_root.is_dir():
        return empty
    window_date = window_start.date()
    precise: list[str] = []
    legacy_date_match: list[str] = []
    for path in sorted(normalized_root.rglob("*.md")):
        try:
            frontmatter, _ = query_index.split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        source_id = source_identifier(path, frontmatter)
        if frontmatter.get("normalized_at") is not None:
            if in_window(frontmatter.get("normalized_at"), window_start):
                precise.append(source_id)
            continue
        updated_date = parse_frontmatter_date(frontmatter.get("updated"))
        if updated_date is not None and updated_date >= window_date:
            legacy_date_match.append(source_id)
    return {"precise": precise, "legacy_date_match": legacy_date_match}


def generated_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def relative_label(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def question_status_snapshot(
    project_root: Path,
    config: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)
    records = question_status.collect_questions(questions_dir)
    return {
        "generated_at": generated_at,
        "questions_dir": relative_label(project_root, questions_dir),
        **question_status.build_report(records),
    }


def source_requests_snapshot(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    source_requests = load_sibling_module("source_requests")
    path = source_requests.requests_path(project_root, config)
    records = source_requests.load_requests(path)
    open_records = [record for record in records if record.get("status") == "open"]
    open_records.sort(key=lambda record: (str(record.get("created_at", "")), str(record.get("request_id", ""))))
    return {
        "requests_path": relative_label(project_root, path),
        "open_total": len(open_records),
        "open": open_records,
    }


def frontmatter_timestamp_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def normalized_sources_snapshot(project_root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    query_index = load_sibling_module("query_index")
    normalized_root = query_index.normalized_dir(project_root, config)
    if not normalized_root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(normalized_root.rglob("*.md")):
        try:
            frontmatter, _ = query_index.split_frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        records.append(
            {
                "source_id": source_identifier(path, frontmatter),
                "path": relative_label(project_root, path),
                "normalized_at": frontmatter_timestamp_text(frontmatter.get("normalized_at")),
            }
        )
    records.sort(key=lambda record: (record["source_id"], record["path"]))
    return records


def build_baseline_snapshot(project_root: Path) -> dict[str, Any]:
    question_status = load_sibling_module("question_status")
    config = question_status.load_config(project_root)
    generated_at = generated_timestamp()
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": BASELINE_DOCUMENT_TYPE,
        "generated_at": generated_at,
        "question_status": question_status_snapshot(project_root, config, generated_at),
        "source_requests": source_requests_snapshot(project_root, config),
        "normalized_sources": normalized_sources_snapshot(project_root, config),
    }


def legacy_normalized_warning(source_ids: list[str]) -> str:
    joined = ", ".join(source_ids)
    return (
        "Legacy normalized records without normalized_at matched the date-only fallback "
        f"and were reported in sources_normalized_legacy_date_match: {joined}. "
        "They may have been normalized before this run."
    )


def extend_normalized_warnings(warnings: list[str], legacy_source_ids: list[str]) -> None:
    if legacy_source_ids:
        warnings.append(legacy_normalized_warning(legacy_source_ids))


def normalized_activity_fields(
    project_root: Path,
    config: dict[str, Any],
    window_start: datetime | None,
    warnings: list[str],
) -> dict[str, list[str]]:
    activity = normalized_during_window(project_root, config, window_start)
    extend_normalized_warnings(warnings, activity["legacy_date_match"])
    return {
        "sources_normalized": activity["precise"],
        "sources_normalized_legacy_date_match": activity["legacy_date_match"],
    }


def append_source_id_lines(lines: list[str], source_ids: list[str]) -> None:
    if source_ids:
        lines.extend(f"- `{source_id}`" for source_id in source_ids)
    else:
        lines.append("- None.")


def lint_summary(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    lint = load_sibling_module("lint")
    try:
        results = lint.run_checks(project_root, config)
    except SystemExit as exc:
        return {"error": str(exc), "issue_counts": {}}
    stats = results.get("stats") if isinstance(results.get("stats"), dict) else {}
    issue_counts = stats.get("issue_counts") if isinstance(stats.get("issue_counts"), dict) else {}
    return {"error": None, "issue_counts": issue_counts}


def build_document(
    project_root: Path,
    baseline_path: Path,
    agent_id: str | None,
    run_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    question_status = load_sibling_module("question_status")
    config = question_status.load_config(project_root)
    baseline, window_start = load_baseline(baseline_path)

    questions_dir = question_status.questions_directory(project_root, config)
    current_records_list = question_status.collect_questions(questions_dir)
    current_report = question_status.build_report(current_records_list)
    current_records = {record["slug"]: record for record in current_records_list}

    baseline_states = question_state_map(baseline)
    current_states = question_state_map(current_report)
    touched = diff_questions(baseline_states, current_states, current_records)

    counts_before = {
        "total": baseline.get("total", len(baseline_states)),
        "by_status": baseline.get("by_status", {}),
        "actionable": baseline.get("actionable"),
        "blocked": baseline.get("blocked"),
        "answered": baseline.get("answered"),
    }
    counts_after = {
        "total": current_report["total"],
        "by_status": current_report["by_status"],
        "actionable": current_report["actionable"],
        "blocked": current_report["blocked"],
        "answered": current_report["answered"],
    }

    generated_at = generated_timestamp()
    warnings: list[str] = []
    if window_start is None:
        warnings.append(
            "Baseline has no parseable generated_at; window-based sections "
            "(source requests, normalized sources) are empty."
        )
    normalized_fields = normalized_activity_fields(project_root, config, window_start, warnings)
    official_summary = official_source_summary(project_root, config)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "agent_id": agent_id,
        "run_controller": run_controller_summary(run_state),
        "official_source_evaluation": official_summary,
        "window": {
            "start": window_start.strftime("%Y-%m-%dT%H:%M:%SZ") if window_start else None,
            "end": generated_at,
        },
        "questions": {
            "before": counts_before,
            "after": counts_after,
            "touched": touched,
        },
        "source_requests": source_request_activity(project_root, config, window_start),
        **normalized_fields,
        "lint": lint_summary(project_root, config),
        "warnings": warnings,
    }


def format_counts(counts: dict[str, Any]) -> str:
    by_status = counts.get("by_status") or {}
    status_text = ", ".join(f"{status}: {count}" for status, count in by_status.items()) or "none"
    return f"total {counts.get('total', 0)} ({status_text})"


def render_markdown(document: dict[str, Any]) -> str:
    questions = document["questions"]
    requests = document["source_requests"]
    run_controller = document["run_controller"]
    official = document.get("official_source_evaluation") if isinstance(document.get("official_source_evaluation"), dict) else {}
    lines = [
        "# Research Run Report",
        "",
        f"- Generated: `{document['generated_at']}`",
        f"- Window: `{document['window']['start'] or 'unknown'}` to `{document['window']['end']}`",
    ]
    if document.get("agent_id"):
        lines.append(f"- Agent: `{document['agent_id']}`")
    if run_controller.get("present"):
        lines.extend(
            [
                "",
                "## Run Controller",
                "",
                f"- Run id: `{run_controller.get('run_id')}`",
                f"- State: `{run_controller.get('state')}`",
                f"- Final verdict: `{run_controller.get('final_verdict')}`",
                f"- Candidate counts: {json.dumps(run_controller.get('candidate_counts', {}), sort_keys=True)}",
                f"- Coverage counts: {json.dumps(run_controller.get('coverage_counts', {}), sort_keys=True)}",
                "",
                "### State Transitions",
                "",
            ]
        )
        history = run_controller.get("state_history") if isinstance(run_controller.get("state_history"), list) else []
        if history:
            for entry in history:
                from_state = entry.get("from_state") or "(start)"
                to_state = entry.get("to_state") or "(unknown)"
                changed_at = entry.get("changed_at") or "unknown"
                lines.append(f"- `{from_state}` -> `{to_state}` at `{changed_at}`")
        else:
            lines.append("- None.")
    if official:
        lines.extend(
            [
                "",
                "## Official-Source Evaluation",
                "",
                f"- Artifact verdict: `{official.get('final_verdict')}`",
                f"- Blocked request ids: {', '.join(official.get('blocked_request_ids', [])) or 'none'}",
                f"- Candidate summary: {json.dumps(official.get('candidate_summary', {}), sort_keys=True)}",
                f"- Coverage summary: {json.dumps(official.get('coverage_summary', {}), sort_keys=True)}",
            ]
        )
    lines.extend(
        [
            "",
            "## Backlog",
            "",
            f"- Before: {format_counts(questions['before'])}",
            f"- After: {format_counts(questions['after'])}",
            "",
            "## Questions Touched",
            "",
        ]
    )
    if questions["touched"]:
        for entry in questions["touched"]:
            detail = f"{entry['status_before'] or '(new)'} -> {entry['status_after'] or '(removed)'}"
            suffix = ""
            if entry.get("answer_page"):
                suffix = f" | answer: {entry['answer_page']}"
            elif entry.get("blocked_reason"):
                suffix = f" | blocked: {entry['blocked_reason']}"
            lines.append(f"- `{entry['slug']}`: {detail}{suffix}")
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Source Requests",
            "",
            f"- Opened during run: {', '.join(requests['opened']) or 'none'}",
            f"- Fulfilled during run: {', '.join(requests['fulfilled']) or 'none'}",
            f"- Open now: {requests['open_total']}",
            "",
            "## Sources Normalized During Run",
            "",
        ]
    )
    append_source_id_lines(lines, document["sources_normalized"])
    if document["sources_normalized_legacy_date_match"]:
        lines.extend(
            [
                "",
                "## Legacy Date-Matched Normalized Sources",
                "",
                "These records lack `normalized_at`; they matched only by date-only `updated` metadata.",
                "",
            ]
        )
        append_source_id_lines(lines, document["sources_normalized_legacy_date_match"])
    lint = document["lint"]
    lint_text = (
        f"error: {lint['error']}"
        if lint.get("error")
        else ", ".join(f"{level}: {count}" for level, count in lint.get("issue_counts", {}).items()) or "none"
    )
    lines.extend(["", "## Lint", "", f"- Issues by severity: {lint_text}"])
    if document["warnings"]:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in document["warnings"])
    return "\n".join(lines).rstrip() + "\n"


def report_path_for(project_root: Path, generated_at: str) -> Path:
    stamp = generated_at.replace(":", "").replace("-", "").replace("Z", "Z")
    return project_root / RUN_REPORTS_DIR / f"run-{stamp}.md"


def main(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = parse_args(raw_argv)
    json_mode = json_mode_requested(raw_argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    if args.command == "baseline":
        output_path = Path(args.output).expanduser().resolve()
        try:
            document = build_baseline_snapshot(project_root)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        except SystemExit as exc:
            return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "baseline_path": str(output_path),
                        "generated_at": document["generated_at"],
                    },
                    indent=2,
                    sort_keys=False,
                )
            )
        else:
            print(f"wrote {output_path}")
        return EXIT_OK

    try:
        run_state = load_run_state(project_root, args.run_id)
        baseline_path = resolve_baseline_path(project_root, args.baseline, run_state)
        document = build_document(project_root, baseline_path, args.agent_id, run_state)
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
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    report_path = report_path_for(project_root, document["generated_at"])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(document), encoding="utf-8")
    try:
        document["report_path"] = report_path.relative_to(project_root).as_posix()
    except ValueError:
        document["report_path"] = report_path.as_posix()

    if args.format == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        print(f"wrote {document['report_path']}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
