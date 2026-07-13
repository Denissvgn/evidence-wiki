#!/usr/bin/env python3
"""Report the status of question task records in a research workspace.

Question pages live under the configured wiki questions directory (default
``wiki/questions``). Each page carries lifecycle frontmatter (``status``,
``priority``, ``origin``, ``answer_page``, ``blocked_reason``). This script
produces a deterministic backlog summary that agents and humans can use to
decide what to answer next and to report progress back to a parent agent.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read question pages") from exc


DEFAULT_QUESTION_DIR = "questions"
ACTIONABLE_STATUSES = ("open", "in_progress")
STATUS_ORDER = ("open", "in_progress", "human_review", "blocked", "deferred", "answered", "rejected")
PRIORITY_ORDER = ("high", "medium", "low")
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import handle_system_exit, json_mode_requested


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize question task records in a research workspace.")
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
        "--status",
        action="append",
        default=None,
        help="Only include questions with the given status. Repeatable.",
    )
    return parser.parse_args(argv)


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "research.yml"
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    config = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(config, dict):
        raise SystemExit(f"Invalid config: {config_path}")
    return config


def validate_workspace_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"research.yml {label} must be a non-empty workspace-relative path")
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not a URL: {value}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not an absolute path: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not an absolute path: {value}")
    if ".." in path.parts:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path without '..': {value}")
    return path.as_posix()


def questions_directory(project_root: Path, config: dict[str, Any]) -> Path:
    wiki_config = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    wiki_root = wiki_config.get("root") if isinstance(wiki_config.get("root"), str) else "wiki"
    return project_root / validate_workspace_relative_path(wiki_root, "wiki.root") / DEFAULT_QUESTION_DIR


def load_frontmatter(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    block = text[4:end]
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def collect_questions(questions_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not questions_dir.is_dir():
        return records
    for path in sorted(questions_dir.glob("*.md")):
        frontmatter = load_frontmatter(path)
        if frontmatter is None or frontmatter.get("type") != "question":
            continue
        status = frontmatter.get("status")
        records.append(
            {
                "slug": path.stem,
                "path": path.name,
                "status": status if isinstance(status, str) else "unknown",
                "priority": frontmatter.get("priority") if isinstance(frontmatter.get("priority"), str) else "",
                "origin": frontmatter.get("origin") if isinstance(frontmatter.get("origin"), str) else "",
                "question": _text_field(frontmatter, "question") or _text_field(frontmatter, "summary"),
                "answer_page": _text_field(frontmatter, "answer_page"),
                "blocked_reason": _text_field(frontmatter, "blocked_reason"),
                "blocking_request_ids": _string_list(frontmatter.get("blocking_request_ids")),
                "claimed_by": _text_field(frontmatter, "claimed_by") or None,
                "claimed_at": _timestamp_field(frontmatter, "claimed_at"),
            }
        )
    return records


def _timestamp_field(frontmatter: dict[str, Any], key: str) -> str | None:
    """Normalize a timestamp value (quoted string or YAML-parsed datetime) to text."""
    value = frontmatter.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return None


def _text_field(frontmatter: dict[str, Any], key: str) -> str:
    value = frontmatter.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def count_by(records: list[dict[str, Any]], key: str, order: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = record.get(key) or "unknown"
        counts[value] = counts.get(value, 0) + 1
    ordered: dict[str, int] = {}
    for value in order:
        if value in counts:
            ordered[value] = counts[value]
    for value in sorted(counts):
        if value not in ordered:
            ordered[value] = counts[value]
    return ordered


def status_sort_key(record: dict[str, Any]) -> tuple[int, int, str]:
    status = record.get("status") or "unknown"
    priority = record.get("priority") or ""
    status_rank = STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)
    priority_rank = PRIORITY_ORDER.index(priority) if priority in PRIORITY_ORDER else len(PRIORITY_ORDER)
    return (status_rank, priority_rank, record.get("slug", ""))


def build_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    records = sorted(records, key=status_sort_key)
    actionable = [record for record in records if record.get("status") in ACTIONABLE_STATUSES]
    blocked = [record for record in records if record.get("status") == "blocked"]
    human_review = [record for record in records if record.get("status") == "human_review"]
    answered = [record for record in records if record.get("status") == "answered"]
    return {
        "total": len(records),
        "by_status": count_by(records, "status", STATUS_ORDER),
        "by_priority": count_by(records, "priority", PRIORITY_ORDER),
        "actionable": len(actionable),
        "human_review": len(human_review),
        "blocked": len(blocked),
        "answered": len(answered),
        "questions": records,
    }


def render_text(report: dict[str, Any], questions_dir_label: str) -> str:
    lines = [
        "Question Backlog Report",
        "=======================",
        f"Questions directory: {questions_dir_label}",
        f"Total question tasks: {report['total']}",
    ]
    if report["by_status"]:
        status_summary = ", ".join(f"{status}: {count}" for status, count in report["by_status"].items())
        lines.append(f"By status: {status_summary}")
    if report["by_priority"]:
        priority_summary = ", ".join(f"{priority}: {count}" for priority, count in report["by_priority"].items())
        lines.append(f"By priority: {priority_summary}")
    lines.append("")

    actionable = [record for record in report["questions"] if record["status"] in ACTIONABLE_STATUSES]
    lines.append(f"Actionable backlog ({len(actionable)}):")
    if actionable:
        for record in actionable:
            priority = record["priority"] or "unset"
            lines.append(f"- [{record['status']}/{priority}] {record['slug']}: {record['question'] or '(no text)'}")
    else:
        lines.append("- None. No open or in-progress questions.")
    lines.append("")

    human_review = [record for record in report["questions"] if record["status"] == "human_review"]
    if human_review:
        lines.append(f"Pending Human Review ({len(human_review)}):")
        for record in human_review:
            lines.append(f"- {record['slug']}: {record['question'] or '(no text)'}")
        lines.append("")

    blocked = [record for record in report["questions"] if record["status"] == "blocked"]
    if blocked:
        lines.append(f"Blocked ({len(blocked)}):")
        for record in blocked:
            reason = record["blocked_reason"] or "(no reason recorded)"
            lines.append(f"- {record['slug']}: {reason}")
        lines.append("")

    answered = [record for record in report["questions"] if record["status"] == "answered"]
    if answered:
        lines.append(f"Answered ({len(answered)}):")
        for record in answered:
            answer_page = record["answer_page"] or "(no answer_page)"
            lines.append(f"- {record['slug']} -> {answer_page}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        config = load_config(project_root)
        questions_dir = questions_directory(project_root, config)
        records = collect_questions(questions_dir)
        if args.status:
            wanted = {value.strip() for value in args.status if value and value.strip()}
            records = [record for record in records if record["status"] in wanted]
        report = build_report(records)
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=2)

    try:
        questions_dir_label = questions_dir.relative_to(project_root).as_posix()
    except ValueError:
        questions_dir_label = questions_dir.as_posix()

    if args.format == "json":
        payload = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "questions_dir": questions_dir_label,
            **report,
        }
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(report, questions_dir_label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
