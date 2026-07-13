#!/usr/bin/env python3
"""Inject a validated batch of research questions into a running workspace.

This is the machine intake half of the question lifecycle API. A planner or
parent agent submits a batch document (JSON or YAML, via ``--from-file`` or
stdin) and each new question becomes an ``open`` question task page under the
configured wiki questions directory, reusing the exact page-creation logic of
``init_research_workspace.py``.

Batch schema (version 1.0)::

    schema_version: "1.0"          # required, must match
    handoff:                       # optional correlation block
      task_id: chain-task-0042     # free-form non-empty strings
      requested_by: planner-agent
      chain_run_id: run-2026-06-09-a
    questions:                     # required, non-empty list
      - question: What benchmarks matter?   # required
        id: benchmarks                      # optional slug hint
        priority: high                      # optional: high|medium|low
        origin: planner_agent               # optional, default parent_agent
        summary: One-line restatement.      # optional, used in index/frontmatter
        context: |                          # optional, stored in the page body
          Free-text constraints supplied with the request.

Behavior:

- The whole batch is validated before anything is written; any schema error
  rejects the batch (no partial intake).
- Questions are deduplicated against the existing backlog by normalized
  question text (case- and whitespace-insensitive). Duplicates are reported
  as skipped, never overwritten, so re-running a batch is idempotent.
- Created pages are validated against ``research.yml`` frontmatter rules
  (allowed priorities, allowed ``open`` status, required fields) before
  writing.
- ``index.md`` question rows are updated and one ``intake`` entry is appended
  to ``log.md`` per batch that created at least one page.
- ``--dry-run`` prints the planned pages (including rendered content) as JSON
  and writes nothing.

Exit codes:

- ``0``: batch accepted (including a fully-duplicate no-op batch).
- ``2``: invalid batch document, unreadable workspace, or config violation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to intake question batches") from exc


SCHEMA_VERSION = "1.0"
SUPPORTED_BATCH_SCHEMA_VERSIONS = ("1.0",)
BATCH_TOP_LEVEL_KEYS = frozenset(("schema_version", "handoff", "handoff_signature", "questions"))
HANDOFF_FIELDS = ("task_id", "requested_by", "chain_run_id")
INTAKE_QUESTION_ITEM_KEYS = frozenset(
    ("id", "question", "text", "priority", "origin", "summary", "context")
)
INTAKE_PAGE_INTRO = "Recorded via the question intake API (`scripts/intake_questions.py`)."
INDEX_PLACEHOLDER_ROW = "| (none yet) | | | |"
MAX_INTAKE_QUESTION_BYTES = 1024
MAX_INTAKE_SUMMARY_BYTES = 1024
MAX_INTAKE_CONTEXT_BYTES = 8192
EXIT_OK = 0
EXIT_INVALID = 2

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _handoff_signature import handoff_secret, verify_handoff_signature
from _intake_limits import (
    DEFAULT_MAX_INTAKE_PER_HOUR,
    DEFAULT_MAX_OPEN_QUESTIONS_TOTAL,
    INTAKE_WINDOW_SECONDS,
    positive_int_config,
    recent_intake_summary,
    timestamp_utc,
)
from _script_errors import handle_system_exit, json_mode_requested
from _workspace_module_loader import load_workspace_module


class IntakeValidationError(SystemExit):
    def __init__(self, message: str, *, error_code: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details


class IntakeLimitExceeded(IntakeValidationError):
    pass


class IntakeFieldTooLong(IntakeValidationError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inject a validated batch of research questions into the workspace backlog.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--from-file",
        default="-",
        help="Batch document path (.json or .yaml/.yml). Use '-' (default) to read stdin.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text. --dry-run always reports JSON.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the batch and print planned pages as JSON without writing anything.",
    )
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so its logic is reused directly."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "research.yml"
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit(f"Invalid config: {config_path}")
    return config


def read_batch_document(from_file: str) -> dict[str, Any]:
    if from_file == "-":
        text = sys.stdin.read()
        label = "stdin"
    else:
        path = Path(from_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"Missing batch file: {path}")
        text = path.read_text(encoding="utf-8")
        label = path.as_posix()
    if not text.strip():
        raise SystemExit(f"Empty batch document: {label}")
    if from_file != "-" and Path(from_file).suffix.lower() == ".json":
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON batch in {label}: {exc}") from exc
    else:
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SystemExit(f"Invalid batch document in {label}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"Batch document must be a mapping: {label}")
    return document


def validate_batch_envelope(batch: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """Validate top-level batch fields; return the normalized handoff block and signature."""
    unknown = sorted(set(batch) - BATCH_TOP_LEVEL_KEYS)
    if unknown:
        allowed = ", ".join(sorted(BATCH_TOP_LEVEL_KEYS))
        raise SystemExit(
            f"question batch has unknown keys: {', '.join(unknown)}. Allowed keys: {allowed}"
        )
    schema_version = batch.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise SystemExit("question batch schema_version must be a non-empty string")
    if schema_version.strip() not in SUPPORTED_BATCH_SCHEMA_VERSIONS:
        supported = ", ".join(SUPPORTED_BATCH_SCHEMA_VERSIONS)
        raise SystemExit(
            f"Unsupported question batch schema_version: {schema_version}. Supported: {supported}"
        )
    handoff: dict[str, str] = {}
    if "handoff" in batch:
        raw_handoff = batch["handoff"]
        if not isinstance(raw_handoff, dict):
            raise SystemExit("question batch handoff must be a mapping")
        unknown_handoff = sorted(set(raw_handoff) - set(HANDOFF_FIELDS))
        if unknown_handoff:
            allowed = ", ".join(HANDOFF_FIELDS)
            raise SystemExit(
                f"question batch handoff has unknown keys: {', '.join(unknown_handoff)}. "
                f"Allowed keys: {allowed}"
            )
        for field in HANDOFF_FIELDS:
            if field in raw_handoff:
                value = raw_handoff[field]
                if not isinstance(value, str) or not value.strip():
                    raise SystemExit(f"question batch handoff.{field} must be a non-empty string")
                handoff[field] = value.strip()
    handoff_signature = batch.get("handoff_signature")
    if handoff_signature is not None:
        if not isinstance(handoff_signature, str) or not handoff_signature.strip():
            raise SystemExit("question batch handoff_signature must be a non-empty string")
        if not handoff:
            raise SystemExit("question batch handoff_signature requires a handoff block")
        handoff_signature = handoff_signature.strip()
    if "questions" not in batch:
        raise SystemExit("question batch must include a questions list")
    questions = batch["questions"]
    if not isinstance(questions, list) or not questions:
        raise SystemExit("question batch questions must be a non-empty list")
    return handoff, handoff_signature


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def validate_intake_field_lengths(raw_questions: list[Any]) -> None:
    field_limits = (
        ("question", MAX_INTAKE_QUESTION_BYTES),
        ("text", MAX_INTAKE_QUESTION_BYTES),
        ("summary", MAX_INTAKE_SUMMARY_BYTES),
        ("context", MAX_INTAKE_CONTEXT_BYTES),
    )
    violations: list[dict[str, Any]] = []
    for index, item in enumerate(raw_questions):
        if not isinstance(item, dict):
            continue
        for field, max_bytes in field_limits:
            value = item.get(field)
            if not isinstance(value, str):
                continue
            actual_bytes = utf8_len(value.strip())
            if actual_bytes > max_bytes:
                violations.append(
                    {
                        "item_index": index,
                        "field": field,
                        "actual_bytes": actual_bytes,
                        "max_bytes": max_bytes,
                    }
                )
    if not violations:
        return
    count = len(violations)
    noun = "field" if count == 1 else "fields"
    verb = "exceeds" if count == 1 else "exceed"
    raise IntakeFieldTooLong(
        f"Intake field length exceeded: {count} {noun} {verb} the intake byte limit.",
        error_code="INTAKE_FIELD_TOO_LONG",
        details={
            "violations": violations,
            "max_question_bytes": MAX_INTAKE_QUESTION_BYTES,
            "max_summary_bytes": MAX_INTAKE_SUMMARY_BYTES,
            "max_context_bytes": MAX_INTAKE_CONTEXT_BYTES,
        },
    )


def question_frontmatter_rules(config: dict[str, Any]) -> dict[str, Any]:
    wiki = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    type_rules = wiki.get("frontmatter_type_rules") if isinstance(wiki.get("frontmatter_type_rules"), dict) else {}
    question_rules = type_rules.get("question") if isinstance(type_rules.get("question"), dict) else {}
    return question_rules


def validate_against_config(config: dict[str, Any], items: list[dict[str, str]], init: ModuleType) -> None:
    """Reject the batch when generated pages would violate research.yml rules."""
    wiki = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    required_dirs = wiki.get("required_dirs") if isinstance(wiki.get("required_dirs"), list) else []
    if init.QUESTION_WIKI_DIR not in required_dirs:
        raise SystemExit(
            f"Cannot intake questions: wiki.required_dirs must include '{init.QUESTION_WIKI_DIR}'."
        )
    allowed_types = wiki.get("allowed_page_types") if isinstance(wiki.get("allowed_page_types"), list) else []
    if allowed_types and "question" not in allowed_types:
        raise SystemExit("Cannot intake questions: wiki.allowed_page_types does not allow 'question' pages.")
    generated_keys = set(init.QUESTION_PAGE_FRONTMATTER_KEYS)
    required_fields = wiki.get("frontmatter_required") if isinstance(wiki.get("frontmatter_required"), list) else []
    missing = [field for field in required_fields if isinstance(field, str) and field not in generated_keys]
    if missing:
        raise SystemExit(
            "Cannot intake questions: research.yml wiki.frontmatter_required expects fields "
            f"the intake page template does not generate: {', '.join(missing)}"
        )
    rules = question_frontmatter_rules(config)
    allowed_values = rules.get("allowed_values") if isinstance(rules.get("allowed_values"), dict) else {}
    allowed_statuses = allowed_values.get("status") if isinstance(allowed_values.get("status"), list) else []
    if allowed_statuses and "open" not in allowed_statuses:
        raise SystemExit("Cannot intake questions: research.yml question rules do not allow status 'open'.")
    allowed_priorities = allowed_values.get("priority") if isinstance(allowed_values.get("priority"), list) else []
    if allowed_priorities:
        for item in items:
            if item["priority"] not in allowed_priorities:
                allowed = ", ".join(str(value) for value in allowed_priorities)
                raise SystemExit(
                    f"question batch priority '{item['priority']}' for '{item['question']}' "
                    f"is not allowed by research.yml. Allowed: {allowed}"
                )


def normalize_question_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def wiki_root_value(config: dict[str, Any]) -> str:
    wiki = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    root = wiki.get("root")
    return root if isinstance(root, str) and root.strip() else "wiki"


def collect_existing_questions(questions_dir: Path, question_status: ModuleType) -> tuple[set[str], dict[str, str]]:
    """Return (existing slugs, normalized question text -> slug)."""
    slugs: set[str] = set()
    texts: dict[str, str] = {}
    if not questions_dir.is_dir():
        return slugs, texts
    for path in sorted(questions_dir.glob("*.md")):
        slugs.add(path.stem)
        frontmatter = question_status.load_frontmatter(path)
        if frontmatter is None or frontmatter.get("type") != "question":
            continue
        text = frontmatter.get("question")
        if not isinstance(text, str) or not text.strip():
            text = frontmatter.get("summary") if isinstance(frontmatter.get("summary"), str) else ""
        normalized = normalize_question_text(text) if text else ""
        if normalized and normalized not in texts:
            texts[normalized] = path.stem
    return slugs, texts


def partition_new_questions(
    items: list[dict[str, str]],
    existing_texts: dict[str, str],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    new_items: list[dict[str, str]] = []
    duplicates: list[dict[str, str]] = []
    seen_in_batch: dict[str, str] = {}
    for item in items:
        normalized = normalize_question_text(item["question"])
        if normalized in existing_texts:
            duplicates.append(
                {
                    "question": item["question"],
                    "duplicate_of": existing_texts[normalized],
                    "reason": "matches an existing question page",
                }
            )
            continue
        if normalized in seen_in_batch:
            duplicates.append(
                {
                    "question": item["question"],
                    "duplicate_of": seen_in_batch[normalized],
                    "reason": "duplicates an earlier question in this batch",
                }
            )
            continue
        seen_in_batch[normalized] = item["slug"]
        new_items.append(item)
    return new_items, duplicates


def count_open_questions(questions_dir: Path, question_status: ModuleType) -> int:
    if not questions_dir.is_dir():
        return 0
    records = question_status.collect_questions(questions_dir)
    return sum(1 for record in records if record.get("status") == "open")


def enforce_intake_limits(
    *,
    project_root: Path,
    config: dict[str, Any],
    questions_dir: Path,
    question_status: ModuleType,
    new_items: list[dict[str, str]],
    now: datetime,
) -> None:
    if not new_items:
        return

    new_count = len(new_items)
    max_open_questions_total = positive_int_config(
        config,
        "max_open_questions_total",
        DEFAULT_MAX_OPEN_QUESTIONS_TOTAL,
    )
    open_questions_total = count_open_questions(questions_dir, question_status)
    projected_total = open_questions_total + new_count
    if projected_total > max_open_questions_total:
        raise IntakeLimitExceeded(
            f"Intake total cap exceeded: open questions total would be {projected_total}, "
            f"limit is {max_open_questions_total}.",
            error_code="INTAKE_TOTAL_CAP_EXCEEDED",
            details={
                "open_questions_total": open_questions_total,
                "new_questions": new_count,
                "max_open_questions_total": max_open_questions_total,
            },
        )

    max_intake_per_hour = positive_int_config(config, "max_intake_per_hour", DEFAULT_MAX_INTAKE_PER_HOUR)
    recent = recent_intake_summary(project_root / "log.md", now=now, window_seconds=INTAKE_WINDOW_SECONDS)
    questions_created_last_hour = int(recent["questions_created_last_hour"])
    projected_hourly = questions_created_last_hour + new_count
    if projected_hourly > max_intake_per_hour:
        raise IntakeLimitExceeded(
            f"Intake rate limit exceeded: {questions_created_last_hour} question(s) in the last hour plus "
            f"{new_count} new question(s) exceeds run.max_intake_per_hour {max_intake_per_hour}.",
            error_code="INTAKE_RATE_LIMITED",
            details={
                "questions_created_last_hour": questions_created_last_hour,
                "new_questions": new_count,
                "max_intake_per_hour": max_intake_per_hour,
                "window_seconds": INTAKE_WINDOW_SECONDS,
            },
        )


def update_index_questions(index_path: Path, config: dict[str, Any], created: list[dict[str, str]]) -> bool:
    """Insert created question rows into the index.md Questions table."""
    if not created or not index_path.is_file():
        return False
    text = index_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    heading = "## Questions"
    try:
        heading_index = lines.index(heading)
    except ValueError:
        return False
    section_end = len(lines)
    for offset, line in enumerate(lines[heading_index + 1 :], start=heading_index + 1):
        if line.startswith("## "):
            section_end = offset
            break
    last_row = None
    for offset in range(heading_index + 1, section_end):
        if lines[offset].startswith("|"):
            last_row = offset
    if last_row is None:
        return False
    root = wiki_root_value(config)
    updated = datetime.now(timezone.utc).date().isoformat()
    init = load_sibling_module("init_research_workspace")
    rows = []
    for item in created:
        page_rel = f"{root}/{init.QUESTION_WIKI_DIR}/{item['slug']}.md"
        summary = init.escape_table_cell(item.get("summary") or item["question"])
        rows.append(f"| [{page_rel}]({page_rel}) | {summary} | {updated} | |")
    if lines[last_row].strip() == INDEX_PLACEHOLDER_ROW:
        lines[last_row : last_row + 1] = rows
    else:
        lines[last_row + 1 : last_row + 1] = rows
    index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return True


def render_log_entry(
    created: list[dict[str, str]],
    duplicates: list[dict[str, str]],
    handoff: dict[str, str],
    batch_label: str,
    created_at: datetime,
) -> str:
    date_text = created_at.astimezone(timezone.utc).date().isoformat()
    origin_counts: dict[str, int] = {}
    for item in created:
        origin_counts[item["origin"]] = origin_counts.get(item["origin"], 0) + 1
    origin_summary = ", ".join(f"{origin}={count}" for origin, count in sorted(origin_counts.items()))
    handoff_line = ""
    if handoff:
        handoff_summary = ", ".join(f"{key}: {value}" for key, value in sorted(handoff.items()))
        handoff_line = f"- Handoff: {handoff_summary}.\n"
    return (
        f"## [{date_text}] intake | Injected question batch\n\n"
        f"- Created at: {timestamp_utc(created_at)}.\n"
        f"- Added: {len(created)} open question page(s) ({origin_summary}).\n"
        f"- Skipped duplicates: {len(duplicates)}.\n"
        f"{handoff_line}"
        f"- Batch source: {batch_label}.\n"
    )


def build_report(
    *,
    questions_dir_label: str,
    created: list[dict[str, str]],
    duplicates: list[dict[str, str]],
    handoff: dict[str, str],
    submitted: int,
    dry_run: bool,
    index_updated: bool,
    log_appended: bool,
    include_content: bool,
    rendered_pages: dict[str, str],
    handoff_signature_status: str | None,
) -> dict[str, Any]:
    created_records = []
    for item in created:
        record: dict[str, Any] = {
            "slug": item["slug"],
            "path": f"{questions_dir_label}/{item['slug']}.md",
            "question": item["question"],
            "priority": item["priority"],
            "origin": item["origin"],
        }
        if include_content:
            record["content"] = rendered_pages[item["slug"]]
        created_records.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": dry_run,
        "handoff": handoff or None,
        "handoff_signature_status": handoff_signature_status,
        "questions_dir": questions_dir_label,
        "counts": {
            "submitted": submitted,
            "created": len(created_records),
            "skipped_duplicates": len(duplicates),
        },
        "created": created_records,
        "skipped_duplicates": duplicates,
        "index_updated": index_updated,
        "log_appended": log_appended,
    }


def render_text_report(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "Question Intake Report",
        "======================",
        f"Questions directory: {report['questions_dir']}",
        f"Submitted: {counts['submitted']}, created: {counts['created']}, "
        f"skipped duplicates: {counts['skipped_duplicates']}",
    ]
    if report["handoff"]:
        handoff_summary = ", ".join(f"{key}: {value}" for key, value in sorted(report["handoff"].items()))
        lines.append(f"Handoff: {handoff_summary}")
    if report["created"]:
        lines.append("")
        lines.append("Created:")
        for record in report["created"]:
            lines.append(f"- [{record['priority']}] {record['path']}: {record['question']}")
    if report["skipped_duplicates"]:
        lines.append("")
        lines.append("Skipped duplicates:")
        for record in report["skipped_duplicates"]:
            lines.append(f"- {record['question']} (duplicate of {record['duplicate_of']})")
    return "\n".join(lines).rstrip() + "\n"


def run_intake_document(
    project_root: Path,
    batch: dict[str, Any],
    *,
    dry_run: bool,
    from_file_label: str,
) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    config = load_config(project_root)
    init = load_sibling_module("init_research_workspace")
    question_status = load_sibling_module("question_status")

    handoff, signature = validate_batch_envelope(batch)
    verification = verify_handoff_signature(handoff or None, signature, handoff_secret(project_root))
    if verification.error_code is not None:
        raise IntakeValidationError(
            verification.message or "Handoff signature verification failed.",
            error_code=verification.error_code,
            details=verification.details or {},
        )

    questions_dir = question_status.questions_directory(project_root, config)
    existing_slugs, existing_texts = collect_existing_questions(questions_dir, question_status)

    validate_intake_field_lengths(batch["questions"])
    items = init.normalize_question_items(
        batch["questions"],
        allowed_keys=INTAKE_QUESTION_ITEM_KEYS,
        error_prefix="question batch",
        used_slugs=existing_slugs,
    )
    validate_against_config(config, items, init)
    new_items, duplicates = partition_new_questions(items, existing_texts)
    now = datetime.now(timezone.utc)
    enforce_intake_limits(
        project_root=project_root,
        config=config,
        questions_dir=questions_dir,
        question_status=question_status,
        new_items=new_items,
        now=now,
    )

    rendered_pages: dict[str, str] = {}
    for item in new_items:
        page_question = dict(item)
        page_question["intro"] = INTAKE_PAGE_INTRO
        rendered_pages[item["slug"]] = init.render_question_page(page_question)

    try:
        questions_dir_label = questions_dir.relative_to(project_root).as_posix()
    except ValueError:
        questions_dir_label = questions_dir.as_posix()

    index_updated = False
    log_appended = False
    if not dry_run and new_items:
        questions_dir.mkdir(parents=True, exist_ok=True)
        for item in new_items:
            (questions_dir / f"{item['slug']}.md").write_text(rendered_pages[item["slug"]], encoding="utf-8")
        index_updated = update_index_questions(project_root / "index.md", config, new_items)
        init.append_log_entry(
            project_root / "log.md",
            render_log_entry(new_items, duplicates, handoff, from_file_label, now),
        )
        log_appended = True

    return build_report(
        questions_dir_label=questions_dir_label,
        created=new_items,
        duplicates=duplicates,
        handoff=handoff,
        submitted=len(items),
        dry_run=dry_run,
        index_updated=index_updated,
        log_appended=log_appended,
        include_content=dry_run,
        rendered_pages=rendered_pages,
        handoff_signature_status=verification.status if handoff else None,
    )


def run_intake(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(args.project_root)
    batch = read_batch_document(args.from_file)
    batch_label = "stdin" if args.from_file == "-" else Path(args.from_file).name
    return run_intake_document(
        project_root,
        batch,
        dry_run=args.dry_run,
        from_file_label=batch_label,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.dry_run or args.format == "json")
    try:
        report = run_intake(args)
    except IntakeValidationError as exc:
        return handle_system_exit(
            exc,
            json_mode=json_mode,
            default_exit_code=EXIT_INVALID,
            error_code=exc.error_code,
            details=exc.details,
        )
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)
    if args.dry_run or args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text_report(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
