#!/usr/bin/env python3
"""Config-driven research wiki health checks."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc


MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
WIKILINK_RE = re.compile(r"!?\[\[(?P<target>[^\]\n]+)\]\]")
DATAVIEW_FROM_RE = re.compile(r"\bFROM\b(?P<body>.*)", re.IGNORECASE)
QUOTED_PATH_RE = re.compile(r"""["']([^"']+)["']""")
PROMPT_INJECTION_PHRASES = (
    (
        "ignore previous instructions",
        re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions\b", re.IGNORECASE),
    ),
    (
        "disregard previous instructions",
        re.compile(r"\bdisregard\s+(?:all\s+)?previous\s+instructions\b", re.IGNORECASE),
    ),
)
BASE64_BLOB_RE = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{256,}={0,2}(?![A-Za-z0-9+/=])")
PROMPT_INJECTION_STRUCTURAL_PATTERNS = (
    (
        "markdown heading with instruction-control wording",
        re.compile(
            r"(?m)^\s{0,3}#{1,6}\s+.*\b(?:instructions?|system\s+(?:prompt|message)|agent\s+instructions?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role/control tag",
        re.compile(
            r"<\s*/?\s*(?:system|developer|assistant|agent)[-_]?"
            r"(?:reminder|message|prompt|instructions?)\b[^>]*>",
            re.IGNORECASE,
        ),
    ),
    (
        "role/tool mention",
        re.compile(r"(?<![\w@.-])@(?:system|assistant|developer|user|agent|tool|browser|python)\b(?![\w.-])"),
    ),
    (
        "json tool-call shape",
        re.compile(
            r"\{(?=[\s\S]{0,4000}\"(?:tool_call|tool_calls|function_call)\"\s*:)"
            r"(?=[\s\S]{0,4000}\"(?:name|function|tool)\"\s*:)"
            r"(?=[\s\S]{0,4000}\"(?:arguments|input)\"\s*:)[\s\S]{0,4000}\}",
            re.IGNORECASE,
        ),
    ),
)
SUPPORTED_FRONTMATTER_FIELD_TYPES = {"string", "string_list", "scalar", "boolean"}
INTEGRATION_PAGE_TYPES = {"claim", "concept", "decision", "method", "system", "synthesis"}
QUESTION_WIKI_DIR = "questions"
DEFAULT_CLAIM_STALENESS_HOURS = 24
CODEBASE_UNTRUSTED_INPUT_ACKNOWLEDGEMENT = "acknowledged"
CODEBASE_KIND = "codebase_architecture"
CODEBASE_VALIDATED_INTAKE_STATUS = "validated"
WEB_CURATION_KINDS = {"html", "web_link", "link"}
CURATION_STATS = (
    "curation_records_checked",
    "curation_cited_records_checked",
    "curation_missing_terms_license",
    "curation_missing_source_note",
    "curation_missing_origin_url",
    "curation_missing_checksum",
    "curation_missing_candidate_id",
)
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_module_loader import load_workspace_module

_script_errors = load_workspace_module(_SCRIPT_DIR, "_script_errors")
emit_error = _script_errors.emit_error
handle_system_exit = _script_errors.handle_system_exit
json_mode_requested = _script_errors.json_mode_requested
_workspace_health = load_workspace_module(_SCRIPT_DIR, "_workspace_health")
evaluate_workspace_health = _workspace_health.evaluate_workspace_health
_workspace_locks = load_workspace_module(_SCRIPT_DIR, "_workspace_locks")
LockUnavailableError = _workspace_locks.LockUnavailableError
workspace_lock = _workspace_locks.workspace_lock
_source_failure_taxonomy = load_workspace_module(_SCRIPT_DIR, "source_failure_taxonomy")
delivery_unusable_evidence_reasons = _source_failure_taxonomy.unusable_evidence_reasons
coverage_manifest = load_workspace_module(_SCRIPT_DIR, "coverage_manifest")


@dataclass
class Issue:
    severity: str
    category: str
    message: str
    files: list[str]
    recommendation: str
    field: str | None = None
    expected: str | None = None
    actual: Any | None = None
    source_id: str | None = None
    expected_path: str | None = None
    code: str | None = None


@dataclass
class ClaimRecord:
    location: str
    path: Path
    subject: str
    predicate: str
    object: str
    source_ids: list[str]
    has_value: bool
    value: Any | None
    unit: str
    scope: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run config-driven research wiki lint checks.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format. Defaults to json.",
    )
    parser.add_argument(
        "--append-log",
        action="store_true",
        help="Append a compact lint summary to log.md.",
    )
    return parser.parse_args()


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "research.yml"
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    config = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(config, dict):
        raise SystemExit(f"Invalid config: {config_path}")
    return config


def config_mapping(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"research.yml {key} must be a mapping")
    return value


def config_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"research.yml {label} must be a list of strings")
    return value


def normalize_frontmatter_type_rules(wiki_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_rules = wiki_config.get("frontmatter_type_rules") or {}
    if not isinstance(raw_rules, dict):
        raise SystemExit("research.yml wiki.frontmatter_type_rules must be a mapping")

    rules: dict[str, dict[str, Any]] = {}
    for page_type, raw_rule in raw_rules.items():
        if not isinstance(page_type, str) or not isinstance(raw_rule, dict):
            raise SystemExit("research.yml wiki.frontmatter_type_rules must map page types to mappings")

        label = f"wiki.frontmatter_type_rules.{page_type}"
        field_types = raw_rule.get("field_types") or {}
        allowed_values = raw_rule.get("allowed_values") or {}
        if not isinstance(field_types, dict) or not all(
            isinstance(field, str)
            and isinstance(field_type, str)
            and field_type in SUPPORTED_FRONTMATTER_FIELD_TYPES
            for field, field_type in field_types.items()
        ):
            raise SystemExit(
                f"research.yml {label}.field_types must map strings to one of: "
                f"{', '.join(sorted(SUPPORTED_FRONTMATTER_FIELD_TYPES))}"
            )
        if not isinstance(allowed_values, dict) or not all(
            isinstance(field, str)
            and isinstance(values, list)
            and all(isinstance(value, str) for value in values)
            for field, values in allowed_values.items()
        ):
            raise SystemExit(f"research.yml {label}.allowed_values must map fields to lists of strings")

        rules[page_type] = {
            "required_fields": config_list(raw_rule.get("required_fields"), f"{label}.required_fields"),
            "field_types": dict(field_types),
            "non_empty_fields": config_list(raw_rule.get("non_empty_fields"), f"{label}.non_empty_fields"),
            "allowed_values": {field: list(values) for field, values in allowed_values.items()},
        }
    return rules


def issue(
    results: dict[str, Any],
    severity: str,
    category: str,
    message: str,
    files: list[str] | None = None,
    recommendation: str = "",
    field: str | None = None,
    expected: str | None = None,
    actual: Any | None = None,
    source_id: str | None = None,
    expected_path: str | None = None,
    code: str | None = None,
) -> None:
    item = asdict(
        Issue(
            severity=severity,
            category=category,
            message=message,
            files=files or [],
            recommendation=recommendation,
            field=field,
            expected=expected,
            actual=actual,
            source_id=source_id,
            expected_path=expected_path,
            code=code,
        )
    )
    results["issues"].append({key: value for key, value in item.items() if value is not None})


def project_relative(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_config_path(
    project_root: Path,
    value: Any,
    label: str,
    results: dict[str, Any],
    *,
    under_sources: bool = False,
) -> tuple[str | None, Path | None]:
    if not isinstance(value, str) or not value.strip():
        issue(
            results,
            "HIGH",
            "config_path",
            f"research.yml {label} must be a non-empty workspace-relative path",
            ["research.yml"],
            f"Set {label} to a workspace-relative path.",
            field=label,
            expected="non-empty workspace-relative path",
            actual=type(value).__name__,
        )
        return None, None
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    reason: str | None = None
    if "://" in normalized or parsed.scheme:
        reason = "must not be a URL"
    elif len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        reason = "must not be an absolute path"
    else:
        relative = PurePosixPath(normalized)
        if relative.is_absolute():
            reason = "must not be an absolute path"
        elif ".." in relative.parts:
            reason = "must not contain '..'"
        elif under_sources and relative.as_posix() != "sources" and not relative.as_posix().startswith("sources/"):
            reason = "must stay under sources/"

    if reason is not None:
        issue(
            results,
            "HIGH",
            "config_path",
            f"research.yml {label} must be a workspace-relative path: {reason}",
            ["research.yml"],
            f"Set {label} to a safe workspace-relative path.",
            field=label,
            expected="workspace-relative path",
            actual=value,
        )
        return None, None

    relative_text = PurePosixPath(normalized).as_posix()
    return relative_text, project_root / relative_text


def safe_source_id(source_id: str) -> str:
    value = source_id.lower().replace(":", "__colon__")
    value = re.sub(r"[/\s]+", "-", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    value = value.replace("__colon__", "--")
    value = value.replace("-.", ".").strip("-")
    return value or "source"


def load_frontmatter(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read file: {exc}"
    # Normalize line endings so CRLF (Windows) and legacy CR files parse correctly.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, "missing YAML frontmatter"
    # Close on the first line that is exactly `---`; a horizontal rule or
    # `---`-prefixed value inside the block must not terminate it early.
    closing_index = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing_index is None:
        return None, "unterminated YAML frontmatter"
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    except yaml.YAMLError as exc:
        return None, f"invalid YAML frontmatter: {exc}"
    if not isinstance(frontmatter, dict):
        return None, "frontmatter must be a mapping"
    return frontmatter, None


def read_manifest(
    manifest_path: Path,
    project_root: Path,
    lifecycle_statuses: set[str],
    results: dict[str, Any],
) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        return []
    try:
        lines = manifest_path.read_text().splitlines()
    except OSError as exc:
        issue(
            results,
            "MEDIUM",
            "source_manifest",
            f"Cannot read source manifest: {exc}",
            [project_relative(project_root, manifest_path)],
            "Fix file permissions or regenerate the source manifest.",
        )
        return []

    valid_records: list[dict[str, Any]] = []
    records = 0
    invalid = 0
    kinds_by_id: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        records += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            invalid += 1
            issue(
                results,
                "HIGH",
                "source_manifest",
                f"Invalid JSONL at line {line_number}: {exc}",
                [project_relative(project_root, manifest_path)],
                "Regenerate or repair the source manifest.",
            )
            continue
        if not isinstance(record, dict):
            invalid += 1
            issue(
                results,
                "HIGH",
                "source_manifest",
                f"Invalid manifest record at line {line_number}: expected object",
                [project_relative(project_root, manifest_path)],
                "Regenerate or repair the source manifest.",
            )
            continue
        field_errors = manifest_required_field_errors(record)
        if field_errors:
            invalid += 1
            issue(
                results,
                "HIGH",
                "source_manifest",
                f"Invalid manifest record at line {line_number}: {', '.join(field_errors)}",
                [project_relative(project_root, manifest_path)],
                "Regenerate or repair the source manifest.",
            )
            continue
        source_id = record["id"]
        kind = record["kind"]
        previous_kind = kinds_by_id.get(source_id)
        if previous_kind is not None and previous_kind != kind:
            invalid += 1
            issue(
                results,
                "HIGH",
                "source_manifest",
                (
                    f"Invalid manifest record at line {line_number}: source ID `{source_id}` has "
                    f"conflicting kind `{kind}` (previously `{previous_kind}`)"
                ),
                [project_relative(project_root, manifest_path)],
                "Keep a source ID bound to one source kind or regenerate the source manifest.",
            )
            continue
        kinds_by_id.setdefault(source_id, kind)
        valid_records.append(record)
        status = record.get("status")
        if isinstance(status, str) and lifecycle_statuses and status not in lifecycle_statuses:
            source_id = record.get("id") if isinstance(record.get("id"), str) else f"line {line_number}"
            issue(
                results,
                "MEDIUM",
                "source_status",
                f"Manifest source {source_id} has unsupported status: {status}",
                [project_relative(project_root, manifest_path)],
                "Use one of the lifecycle statuses configured in research.yml.",
            )

    results["stats"]["manifest_records"] = records
    results["stats"]["manifest_invalid_records"] = invalid
    return valid_records


def check_structure(
    project_root: Path,
    wiki_root: Path,
    required_dirs: list[str],
    raw_roots: list[str],
    results: dict[str, Any],
) -> None:
    if not wiki_root.exists():
        issue(
            results,
            "HIGH",
            "structure",
            f"Missing wiki root: {project_relative(project_root, wiki_root)}",
            [project_relative(project_root, wiki_root)],
            "Create the configured wiki root or update wiki.root in research.yml.",
        )
    elif not wiki_root.is_dir():
        issue(
            results,
            "HIGH",
            "structure",
            f"Configured wiki root is not a directory: {project_relative(project_root, wiki_root)}",
            [project_relative(project_root, wiki_root)],
            "Point wiki.root to a directory.",
        )

    for subdir in required_dirs:
        path = wiki_root / subdir
        if not path.is_dir():
            issue(
                results,
                "HIGH",
                "structure",
                f"Missing configured wiki directory: {project_relative(project_root, path)}",
                [project_relative(project_root, path)],
                "Create the directory or remove it from wiki.required_dirs in research.yml.",
            )

    for raw_root in raw_roots:
        path = project_root / raw_root
        if not path.is_dir():
            issue(
                results,
                "HIGH",
                "raw_sources",
                f"Missing configured raw source root: {raw_root}",
                [raw_root],
                "Create the raw source root or update raw.source_roots in research.yml.",
            )


def markdown_files(wiki_root: Path) -> list[Path]:
    if not wiki_root.is_dir():
        return []
    return sorted(
        (
            path
            for path in wiki_root.rglob("*.md")
            if ".locks" not in path.relative_to(wiki_root).parts
        ),
        key=lambda path: path.as_posix(),
    )


def count_pages(project_root: Path, wiki_root: Path, files: list[Path], results: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for path in files:
        try:
            relative = path.relative_to(wiki_root)
        except ValueError:
            continue
        top_level = relative.parts[0] if len(relative.parts) > 1 else "."
        counts[top_level] = counts.get(top_level, 0) + 1
    results["pages_checked"] = len(files)
    results["stats"]["wiki_pages"] = len(files)
    results["stats"]["wiki_counts"] = dict(sorted(counts.items()))
    results["config"]["wiki_root"] = project_relative(project_root, wiki_root)


def missing_reason(frontmatter: dict[str, Any], field: str) -> str | None:
    if field not in frontmatter:
        return "missing"
    if frontmatter[field] is None:
        return "null"
    return None


def actual_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        item_types = sorted({type(item).__name__ for item in value})
        suffix = f"[{', '.join(item_types)}]" if item_types else "[]"
        return f"list{suffix}"
    return type(value).__name__


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def frontmatter_value_matches_type(value: Any, field_type: str) -> bool:
    if field_type == "string":
        return isinstance(value, str)
    if field_type == "string_list":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if field_type == "scalar":
        return isinstance(value, (str, int, float, bool))
    if field_type == "boolean":
        return isinstance(value, bool)
    return False


def type_label(field_type: str) -> str:
    labels = {
        "string": "string",
        "string_list": "list of strings",
        "scalar": "string, number, or boolean",
        "boolean": "boolean",
    }
    return labels.get(field_type, field_type)


def add_frontmatter_issue(
    results: dict[str, Any],
    label: str,
    severity: str,
    field: str,
    message: str,
    recommendation: str,
    expected: str,
    actual: Any,
) -> None:
    issue(
        results,
        severity,
        "frontmatter",
        f"{label}: {message}",
        [label],
        recommendation,
        field=field,
        expected=expected,
        actual=actual,
    )


def check_frontmatter(
    project_root: Path,
    files: list[Path],
    required_fields: list[str],
    allowed_page_types: set[str],
    type_rules: dict[str, dict[str, Any]],
    results: dict[str, Any],
) -> None:
    for path in files:
        label = project_relative(project_root, path)
        frontmatter, error = load_frontmatter(path)
        if error:
            issue(
                results,
                "LOW",
                "frontmatter",
                f"{label}: {error}",
                [label],
                "Add valid YAML frontmatter with the configured required fields.",
            )
            continue
        if frontmatter is None:
            continue

        page_type = frontmatter.get("type")
        rule = type_rules.get(page_type, {}) if isinstance(page_type, str) else {}
        type_required_fields = set(rule.get("required_fields", []))
        all_required_fields = list(dict.fromkeys([*required_fields, *rule.get("required_fields", [])]))

        for field in all_required_fields:
            reason = missing_reason(frontmatter, field)
            if reason:
                severity = "MEDIUM" if field in type_required_fields else "LOW"
                expected = (
                    f"required for `{page_type}` pages"
                    if field in type_required_fields and isinstance(page_type, str)
                    else "required field"
                )
                add_frontmatter_issue(
                    results,
                    label,
                    severity,
                    field,
                    f"missing required frontmatter field `{field}`",
                    f"Add `{field}` to the page frontmatter.",
                    expected,
                    reason,
                )

        if isinstance(page_type, str):
            if allowed_page_types and page_type not in allowed_page_types:
                add_frontmatter_issue(
                    results,
                    label,
                    "MEDIUM",
                    "type",
                    f"unsupported page type `{page_type}`",
                    "Use one of the page types configured in wiki.allowed_page_types.",
                    "one of: " + ", ".join(sorted(allowed_page_types)),
                    page_type,
                )
        elif "type" in frontmatter:
            add_frontmatter_issue(
                results,
                label,
                "LOW",
                "type",
                "frontmatter field `type` must be a string",
                "Set `type` to one of the configured page types.",
                "string",
                actual_type(page_type),
            )

        for field, field_type in rule.get("field_types", {}).items():
            if field not in frontmatter or frontmatter[field] is None:
                continue
            value = frontmatter[field]
            if not frontmatter_value_matches_type(value, field_type):
                add_frontmatter_issue(
                    results,
                    label,
                    "MEDIUM",
                    field,
                    f"frontmatter field `{field}` has invalid type",
                    f"Set `{field}` to a {type_label(field_type)}.",
                    type_label(field_type),
                    actual_type(value),
                )

        for field in rule.get("non_empty_fields", []):
            if field not in frontmatter or frontmatter[field] is None:
                continue
            if is_empty_value(frontmatter[field]):
                add_frontmatter_issue(
                    results,
                    label,
                    "MEDIUM",
                    field,
                    f"frontmatter field `{field}` must not be empty",
                    f"Add at least one value to `{field}`.",
                    "non-empty value",
                    "empty",
                )

        for field, values in rule.get("allowed_values", {}).items():
            if field not in frontmatter or frontmatter[field] is None:
                continue
            value = frontmatter[field]
            if value not in values:
                add_frontmatter_issue(
                    results,
                    label,
                    "MEDIUM",
                    field,
                    f"frontmatter field `{field}` has unsupported value `{value}`",
                    f"Use one of the configured values for `{field}`.",
                    "one of: " + ", ".join(values),
                    value if isinstance(value, (str, int, float, bool)) else actual_type(value),
                )


def candidate_link_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if not target:
        return None
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    else:
        title_match = re.match(r"(?P<path>.+?\.md(?:#[^\s\"']*)?)(?:\s+[\"'].*)?$", target, re.IGNORECASE)
        target = title_match.group("path") if title_match else target.split(None, 1)[0]
    target = unquote(target)
    parsed = urlparse(target)
    if parsed.scheme in {"http", "https", "mailto", "tel"}:
        return None
    if parsed.scheme and len(parsed.scheme) > 1:
        return None
    path = parsed.path
    if not path or path.startswith("#"):
        return None
    if not path.lower().endswith(".md"):
        return None
    return path


def resolve_markdown_link(source_file: Path, target: str, project_root: Path) -> Path | None:
    path = PurePosixPath(target.replace("\\", "/"))
    if path.is_absolute():
        base = project_root / path.as_posix().lstrip("/")
    elif path.parts and path.parts[0] in {"wiki", "sources", "raw"}:
        base = project_root / path.as_posix()
    else:
        base = source_file.parent / path.as_posix()
    try:
        resolved = base.resolve()
        resolved.relative_to(project_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def files_for_link_checks(project_root: Path, wiki_files: list[Path]) -> list[Path]:
    files = list(wiki_files)
    index_path = project_root / "index.md"
    if index_path.is_file():
        files.append(index_path)
    return sorted(set(files), key=lambda path: path.as_posix())


def check_links(project_root: Path, files: list[Path], results: dict[str, Any]) -> None:
    for path in files:
        label = project_relative(project_root, path)
        try:
            text = path.read_text()
        except OSError as exc:
            issue(
                results,
                "MEDIUM",
                "read_error",
                f"Cannot read {label}: {exc}",
                [label],
                "Fix file permissions or remove the unreadable page.",
            )
            continue
        for match in MARKDOWN_LINK_RE.finditer(text):
            target = candidate_link_target(match.group(1))
            if not target:
                continue
            resolved = resolve_markdown_link(path, target, project_root)
            if resolved is None or not resolved.is_file():
                issue(
                    results,
                    "MEDIUM",
                    "broken_link",
                    f"{label}: broken Markdown link to `{target}`",
                    [label],
                    "Create the linked Markdown file or update the link target.",
                )


def wikilink_note_targets(text: str) -> list[str]:
    """Return note targets from `[[wikilinks]]`, skipping non-Markdown asset embeds."""
    targets: list[str] = []
    for match in WIKILINK_RE.finditer(text):
        # Drop the display alias (`|`) and any heading (`#`) or block (`^`) anchor.
        link = match.group("target").split("|", 1)[0].split("#", 1)[0].split("^", 1)[0].strip()
        if not link:
            continue
        suffix = PurePosixPath(link.replace("\\", "/")).suffix.lower()
        if suffix and suffix != ".md":
            continue  # e.g. ![[figure.png]] embeds an asset, not a wiki note
        targets.append(link)
    return targets


def build_wiki_note_index(project_root: Path, wiki_root: Path, files: list[Path]) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    rel_paths: set[str] = set()
    for path in files:
        names.add(path.stem.lower())
        for base in (wiki_root, project_root):
            try:
                rel = path.relative_to(base).as_posix().lower()
            except ValueError:
                continue
            rel_paths.add(rel)
            if rel.endswith(".md"):
                rel_paths.add(rel[:-3])
    return names, rel_paths


def wikilink_resolves(link: str, names: set[str], rel_paths: set[str]) -> bool:
    candidate = link.replace("\\", "/").lstrip("/").lower()
    if "/" in candidate:
        # Path-style wikilink: match relative to the wiki root or workspace root.
        return candidate in rel_paths or f"{candidate}.md" in rel_paths
    name = candidate[:-3] if candidate.endswith(".md") else candidate
    return name in names or candidate in rel_paths or f"{candidate}.md" in rel_paths


def check_wikilinks(project_root: Path, wiki_root: Path, files: list[Path], results: dict[str, Any]) -> None:
    names, rel_paths = build_wiki_note_index(project_root, wiki_root, files)
    for path in files:
        try:
            text = path.read_text()
        except OSError:
            continue  # unreadable files are already reported by check_links
        label = project_relative(project_root, path)
        for link in wikilink_note_targets(text):
            if not wikilink_resolves(link, names, rel_paths):
                issue(
                    results,
                    "MEDIUM",
                    "broken_wikilink",
                    f"{label}: broken wikilink to `[[{link}]]`",
                    [label],
                    "Create the linked note or fix the wikilink target.",
                )


def dataview_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    in_block = False
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not in_block and stripped.lower().startswith("```dataview"):
            in_block = True
            current = []
            continue
        if in_block and stripped.startswith("```"):
            blocks.append("\n".join(current))
            in_block = False
            current = []
            continue
        if in_block:
            current.append(line)
    return blocks


def dataview_from_paths(text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for block in dataview_blocks(text):
        for line in block.splitlines():
            match = DATAVIEW_FROM_RE.search(line)
            if not match:
                continue
            for raw_path in QUOTED_PATH_RE.findall(match.group("body")):
                path = raw_path.strip().strip("/")
                if path and path not in seen:
                    paths.append(path)
                    seen.add(path)
    return paths


def normalized_dataview_dir(project_root: Path, wiki_root: Path, raw_path: str) -> Path | None:
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    candidate = project_root / path.as_posix()
    try:
        resolved = candidate.resolve()
        resolved.relative_to(wiki_root.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_dir():
        return None
    return resolved


def static_indexed_pages(project_root: Path, index_path: Path, wiki_root: Path, text: str) -> set[Path]:
    indexed: set[Path] = set()
    for match in MARKDOWN_LINK_RE.finditer(text):
        target = candidate_link_target(match.group(1))
        if not target:
            continue
        resolved = resolve_markdown_link(index_path, target, project_root)
        if resolved is None or not resolved.is_file():
            continue
        try:
            resolved.relative_to(wiki_root.resolve())
        except ValueError:
            continue
        indexed.add(resolved)
    return indexed


def dataview_indexed_pages(
    project_root: Path,
    wiki_root: Path,
    index_text: str,
) -> tuple[set[Path], list[str]]:
    pages: set[Path] = set()
    dirs: list[str] = []
    seen_dirs: set[Path] = set()
    for raw_path in dataview_from_paths(index_text):
        directory = normalized_dataview_dir(project_root, wiki_root, raw_path)
        if directory is None or directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        dirs.append(project_relative(project_root, directory))
        pages.update(path.resolve() for path in markdown_files(directory))
    return pages, dirs


def check_index_coverage(
    project_root: Path,
    wiki_root: Path,
    wiki_files: list[Path],
    dataview_aware: bool,
    results: dict[str, Any],
) -> None:
    stats = results["stats"]
    index_path = project_root / "index.md"
    stats["indexed_pages"] = 0
    stats["dataview_indexed_dirs"] = []
    stats["orphan_pages"] = 0

    if not wiki_files:
        return
    if not index_path.is_file():
        issue(
            results,
            "LOW",
            "index",
            "Missing index.md while wiki pages exist",
            ["index.md"],
            "Create index.md with static links or Dataview sections for discoverable wiki pages.",
        )
        stats["orphan_pages"] = len(wiki_files)
        return

    try:
        index_text = index_path.read_text()
    except OSError as exc:
        issue(
            results,
            "MEDIUM",
            "read_error",
            f"Cannot read index.md: {exc}",
            ["index.md"],
            "Fix file permissions or recreate the wiki index.",
        )
        stats["orphan_pages"] = len(wiki_files)
        return

    indexed_pages = static_indexed_pages(project_root, index_path, wiki_root, index_text)
    if dataview_aware:
        dataview_pages, dataview_dirs = dataview_indexed_pages(project_root, wiki_root, index_text)
        indexed_pages.update(dataview_pages)
        stats["dataview_indexed_dirs"] = dataview_dirs

    wiki_page_set = {path.resolve() for path in wiki_files}
    orphan_pages = sorted(wiki_page_set.difference(indexed_pages), key=lambda path: path.as_posix())
    stats["indexed_pages"] = len(wiki_page_set.intersection(indexed_pages))
    stats["orphan_pages"] = len(orphan_pages)

    for path in orphan_pages:
        label = project_relative(project_root, path)
        issue(
            results,
            "LOW",
            "orphan",
            f"Wiki page is not covered by index.md: {label}",
            [label],
            "Add a static index link or a Dataview FROM section covering this page.",
        )


def source_ids_from_frontmatter(frontmatter: dict[str, Any]) -> list[str]:
    source_ids = frontmatter.get("source_ids")
    if not isinstance(source_ids, list):
        return []
    return [source_id.strip() for source_id in source_ids if isinstance(source_id, str) and source_id.strip()]


def manifest_id(record: dict[str, Any]) -> str | None:
    source_id = record.get("id")
    return source_id if isinstance(source_id, str) and source_id else None


def manifest_required_field_errors(record: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source_id = record.get("id")
    if not isinstance(source_id, str) or not source_id.strip():
        errors.append("`id` must be a non-empty string")
    kind = record.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        errors.append("`kind` must be a non-empty string")
    raw_paths = record.get("raw_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        errors.append("`raw_paths` must be a non-empty list of strings")
    elif not all(isinstance(path, str) and path.strip() for path in raw_paths):
        errors.append("`raw_paths` must contain only non-empty strings")
    status = record.get("status")
    if status is not None and not isinstance(status, str):
        errors.append("`status` must be a string when present")
    return errors


def normalized_output_path(normalized_root: Path, source_id: str) -> Path:
    return normalized_root / f"{safe_source_id(source_id)}.md"


def expected_source_note_path(wiki_root: Path, source_id: str) -> Path:
    return wiki_root / "sources" / f"{safe_source_id(source_id)}.md"


def index_manifest_records(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        source_id = manifest_id(record)
        if source_id and source_id not in indexed:
            indexed[source_id] = record
    return indexed


def index_normalized_sources(
    normalized_root: Path,
    expected_paths: dict[Path, str],
) -> tuple[dict[str, list[Path]], set[str], list[Path], list[Path]]:
    indexed: dict[str, list[Path]] = {}
    manual_ids: set[str] = set()
    unknown_paths: list[Path] = []
    failed_paths: list[Path] = []
    if not normalized_root.is_dir():
        return indexed, manual_ids, unknown_paths, failed_paths

    for path in sorted(normalized_root.rglob("*.md"), key=lambda value: value.as_posix()):
        frontmatter, _ = load_frontmatter(path)
        source_id = frontmatter.get("source_id") if isinstance(frontmatter, dict) else None
        if isinstance(frontmatter, dict) and frontmatter.get("status") == "failed":
            failed_paths.append(path)
        if isinstance(source_id, str) and source_id:
            indexed.setdefault(source_id, []).append(path)
            if source_id.startswith("manual:"):
                manual_ids.add(source_id)
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        expected_source_id = expected_paths.get(resolved)
        if expected_source_id:
            indexed.setdefault(expected_source_id, []).append(path)
        else:
            unknown_paths.append(path)
    return indexed, manual_ids, unknown_paths, failed_paths


def index_source_notes(
    wiki_root: Path,
) -> tuple[dict[str, list[Path]], set[str]]:
    indexed: dict[str, list[Path]] = {}
    integrated_ids: set[str] = set()
    source_root = wiki_root / "sources"
    for path in markdown_files(source_root):
        frontmatter, _ = load_frontmatter(path)
        if not isinstance(frontmatter, dict) or frontmatter.get("type") != "source":
            continue
        source_ids = source_ids_from_frontmatter(frontmatter)
        for source_id in source_ids:
            indexed.setdefault(source_id, []).append(path)
        if frontmatter.get("status") == "integrated":
            integrated_ids.update(source_ids)
    return indexed, integrated_ids


def index_integration_citations(wiki_root: Path, wiki_files: list[Path]) -> dict[str, list[Path]]:
    indexed: dict[str, list[Path]] = {}
    source_root = wiki_root / "sources"
    for path in wiki_files:
        try:
            path.relative_to(source_root)
            continue
        except ValueError:
            pass
        frontmatter, _ = load_frontmatter(path)
        if not isinstance(frontmatter, dict):
            continue
        if frontmatter.get("type") not in INTEGRATION_PAGE_TYPES:
            continue
        for source_id in source_ids_from_frontmatter(frontmatter):
            indexed.setdefault(source_id, []).append(path)
    return indexed


def page_references_path(project_root: Path, page: Path, target: Path) -> bool:
    try:
        body = page.read_text(encoding="utf-8").replace("\\", "/")
    except (OSError, UnicodeDecodeError):
        return False
    variants = {
        project_relative(project_root, target),
        target.name,
        target.stem,
    }
    return any(value and value in body for value in variants)


def check_codebase_evidence_links(
    project_root: Path,
    manifest_by_id: dict[str, dict[str, Any]],
    normalized_by_id: dict[str, list[Path]],
    source_notes_by_id: dict[str, list[Path]],
    integration_by_id: dict[str, list[Path]],
    results: dict[str, Any],
) -> None:
    """Validate navigable normalized/maintained links for codebase evidence."""

    checked = 0
    missing = 0
    provenance_incomplete = 0
    inactive_statuses = {"deferred", "rejected", "superseded"}
    for source_id, record in sorted(manifest_by_id.items()):
        if record.get("kind") != CODEBASE_KIND:
            continue
        normalized_paths = normalized_by_id.get(source_id, [])
        if not normalized_paths:
            continue
        validated_paths: list[Path] = []
        for path in normalized_paths:
            frontmatter, _error = load_frontmatter(path)
            if (
                isinstance(frontmatter, dict)
                and frontmatter.get("source_kind") == CODEBASE_KIND
                and frontmatter.get("codebase_intake_status") == CODEBASE_VALIDATED_INTAKE_STATUS
                and frontmatter.get("codebase_execution_scope") == "external_worker_only"
            ):
                validated_paths.append(path)
        if not validated_paths:
            if record.get("status") not in inactive_statuses:
                provenance_incomplete += 1
                issue(
                    results,
                    "MEDIUM",
                    "codebase_artifact_provenance",
                    f"Codebase source lacks a validated external-worker artifact manifest: {source_id}",
                    source_paths_for_result(project_root, normalized_paths),
                    (
                        "Deposit a bounded inert artifact plus artifact-manifest.json from a separately authorized "
                        "worker; the product does not clone repositories or execute adapters."
                    ),
                    field="codebase_intake_status",
                    expected=CODEBASE_VALIDATED_INTAKE_STATUS,
                    actual="legacy_unbound_or_invalid",
                    source_id=source_id,
                )
            continue

        checked += 1
        source_notes = source_notes_by_id.get(source_id, [])
        integrations = integration_by_id.get(source_id, [])
        for note in source_notes:
            if any(page_references_path(project_root, note, path) for path in validated_paths):
                continue
            missing += 1
            issue(
                results,
                "MEDIUM",
                "codebase_evidence_link_missing",
                f"Codebase source note does not link its normalized record: {source_id}",
                [project_relative(project_root, note), *source_paths_for_result(project_root, validated_paths)],
                "Add an explicit Markdown or wiki link from the maintained source note to the normalized record.",
                field="source_ids",
                expected="source note -> normalized record link",
                actual=source_id,
                source_id=source_id,
            )
        for integration in integrations:
            if source_notes and any(
                page_references_path(project_root, integration, note)
                for note in source_notes
            ):
                continue
            missing += 1
            issue(
                results,
                "MEDIUM",
                "codebase_evidence_link_missing",
                f"Maintained codebase interpretation does not link its source note: {source_id}",
                [project_relative(project_root, integration), *source_paths_for_result(project_root, source_notes)],
                "Link the maintained claim, synthesis, or decision to its source note while retaining source_ids.",
                field="source_ids",
                expected="maintained page -> source note link",
                actual=source_id,
                source_id=source_id,
            )
        for note in source_notes:
            for integration in integrations:
                if page_references_path(project_root, note, integration):
                    continue
                missing += 1
                issue(
                    results,
                    "MEDIUM",
                    "codebase_evidence_link_missing",
                    f"Codebase source note is missing a maintained-page backlink: {source_id}",
                    [project_relative(project_root, note), project_relative(project_root, integration)],
                    "Add a backlink from the source note to every maintained claim, synthesis, and decision using it.",
                    field="source_ids",
                    expected="source note -> maintained page backlink",
                    actual=source_id,
                    source_id=source_id,
                )
    results["stats"]["codebase_evidence_records_checked"] = checked
    results["stats"]["codebase_evidence_links_missing"] = missing
    results["stats"]["codebase_artifact_provenance_incomplete"] = provenance_incomplete


def index_cited_source_paths(
    project_root: Path,
    wiki_root: Path,
    wiki_files: list[Path],
    output_root: Path,
) -> dict[str, list[str]]:
    """Index public citation pages, excluding wiki source-note pages."""
    indexed: dict[str, list[str]] = {}
    source_root = wiki_root / "sources"
    paths: list[Path] = list(wiki_files)
    if output_root.is_dir():
        known = {path.resolve() for path in paths}
        for path in markdown_files(output_root):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved not in known:
                paths.append(path)
                known.add(resolved)

    for path in sorted(paths, key=lambda item: item.as_posix()):
        try:
            path.relative_to(source_root)
            continue
        except ValueError:
            pass
        frontmatter, _ = load_frontmatter(path)
        if not isinstance(frontmatter, dict) or frontmatter.get("type") == "source":
            continue
        label = project_relative(project_root, path)
        for source_id in source_ids_from_frontmatter(frontmatter):
            indexed.setdefault(source_id, []).append(label)
    return indexed


def load_selected_candidate_ids_by_request(project_root: Path) -> tuple[dict[str, list[str]], str | None]:
    """Return selected candidate ids keyed by request id; malformed lines are ignored."""
    path = project_root / "sources" / "discovery" / "candidates.jsonl"
    if not path.is_file():
        return {}, None
    selected: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if status not in {"selected", "fetched"}:
            continue
        candidate_id = record.get("candidate_id")
        if not (isinstance(candidate_id, str) and candidate_id.strip()):
            continue
        request_id = record.get("selected_for_request_id") or record.get("selected_request_id") or record.get("request_id")
        if not (isinstance(request_id, str) and request_id.strip()):
            continue
        selected.setdefault(request_id.strip(), []).append(candidate_id.strip())
    return selected, project_relative(project_root, path)


def source_paths_for_result(project_root: Path, paths: list[Path]) -> list[str]:
    return [project_relative(project_root, path) for path in paths]


def normalize_claim_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def normalize_claim_scalar(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".12g")
    if isinstance(value, str):
        return normalize_claim_text(value)
    return None


def claim_required_string(record: dict[str, Any], field: str) -> str | None:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def claim_optional_string(record: dict[str, Any], field: str) -> str:
    value = record.get(field)
    return value if isinstance(value, str) else ""


def source_ids_from_claim(
    record: dict[str, Any],
    inherited_source_ids: list[str],
) -> list[str]:
    source_ids = record.get("source_ids")
    if source_ids is None:
        return inherited_source_ids
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or not all(isinstance(source_id, str) and source_id.strip() for source_id in source_ids)
    ):
        return []
    return [source_id.strip() for source_id in source_ids]


def claim_location(label: str, embedded_index: int | None = None) -> str:
    if embedded_index is None:
        return label
    return f"{label} claims[{embedded_index}]"


def add_claim_invalid_issue(
    results: dict[str, Any],
    label: str,
    file_label: str,
    message: str,
    field: str,
    actual: Any,
) -> None:
    issue(
        results,
        "MEDIUM",
        "claim_invalid",
        f"{label}: {message}",
        [file_label],
        "Repair the structured claim frontmatter before relying on claim checks.",
        field=field,
        expected="valid structured claim field",
        actual=actual,
    )


def claim_record_from_mapping(
    path: Path,
    label: str,
    record: dict[str, Any],
    inherited_source_ids: list[str],
    results: dict[str, Any],
    embedded_index: int | None = None,
    report_invalid: bool = True,
) -> ClaimRecord | None:
    location = claim_location(label, embedded_index)
    subject = claim_required_string(record, "subject")
    predicate = claim_required_string(record, "predicate")
    claim_object = claim_required_string(record, "object")
    if subject is None or predicate is None or claim_object is None:
        if report_invalid:
            missing = [
                field
                for field, value in {
                    "subject": subject,
                    "predicate": predicate,
                    "object": claim_object,
                }.items()
                if value is None
            ]
            add_claim_invalid_issue(
                results,
                location,
                label,
                f"missing or empty required claim field(s): {', '.join(missing)}",
                ",".join(missing),
                "missing",
            )
        return None

    source_ids = source_ids_from_claim(record, inherited_source_ids)
    if not source_ids:
        if report_invalid:
            add_claim_invalid_issue(
                results,
                location,
                label,
                "missing or invalid claim source_ids",
                "source_ids",
                actual_type(record.get("source_ids")),
            )
        return None

    for field in ["unit", "scope"]:
        if field in record and record[field] is not None and not isinstance(record[field], str):
            if report_invalid:
                add_claim_invalid_issue(
                    results,
                    location,
                    label,
                    f"claim field `{field}` must be a string",
                    field,
                    actual_type(record[field]),
                )
            return None

    has_value = "value" in record and record.get("value") is not None
    value = record.get("value") if has_value else None
    if has_value and normalize_claim_scalar(value) is None:
        if report_invalid:
            add_claim_invalid_issue(
                results,
                location,
                label,
                "claim field `value` must be scalar",
                "value",
                actual_type(value),
            )
        return None

    return ClaimRecord(
        location=location,
        path=path,
        subject=subject,
        predicate=predicate,
        object=claim_object,
        source_ids=source_ids,
        has_value=has_value,
        value=value,
        unit=claim_optional_string(record, "unit"),
        scope=claim_optional_string(record, "scope"),
    )


def collect_claims(
    project_root: Path,
    wiki_root: Path,
    wiki_files: list[Path],
    results: dict[str, Any],
) -> list[ClaimRecord]:
    claims: list[ClaimRecord] = []
    claims_root = wiki_root / "claims"
    for path in wiki_files:
        label = project_relative(project_root, path)
        frontmatter, _ = load_frontmatter(path)
        if not isinstance(frontmatter, dict):
            continue

        inherited_source_ids = source_ids_from_frontmatter(frontmatter)
        try:
            path.relative_to(claims_root)
            is_claim_page_path = True
        except ValueError:
            is_claim_page_path = False

        if is_claim_page_path and frontmatter.get("type") == "claim":
            claim = claim_record_from_mapping(
                path,
                label,
                frontmatter,
                inherited_source_ids,
                results,
                report_invalid=False,
            )
            if claim is not None:
                claims.append(claim)

        embedded_claims = frontmatter.get("claims")
        if embedded_claims is None:
            continue
        if not isinstance(embedded_claims, list):
            add_claim_invalid_issue(
                results,
                label,
                label,
                "frontmatter field `claims` must be a list of mappings",
                "claims",
                actual_type(embedded_claims),
            )
            continue
        for index, embedded_claim in enumerate(embedded_claims):
            location = claim_location(label, index)
            if not isinstance(embedded_claim, dict):
                add_claim_invalid_issue(
                    results,
                    location,
                    label,
                    "embedded claim must be a mapping",
                    "claims",
                    actual_type(embedded_claim),
                )
                continue
            claim = claim_record_from_mapping(
                path,
                label,
                embedded_claim,
                inherited_source_ids,
                results,
                embedded_index=index,
            )
            if claim is not None:
                claims.append(claim)
    return claims


def claim_group_key(claim: ClaimRecord) -> tuple[str, str, str, str]:
    return (
        normalize_claim_text(claim.subject),
        normalize_claim_text(claim.predicate),
        normalize_claim_text(claim.unit),
        normalize_claim_text(claim.scope),
    )


def claim_comparable(claim: ClaimRecord) -> tuple[str, str]:
    if claim.has_value:
        normalized_value = normalize_claim_scalar(claim.value)
        return ("value", normalized_value if normalized_value is not None else "")
    return ("object", normalize_claim_text(claim.object))


def unique_claim_paths(project_root: Path, claims: list[ClaimRecord]) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for claim in claims:
        label = project_relative(project_root, claim.path)
        if label not in seen:
            seen.add(label)
            paths.append(label)
    return paths


def unique_claim_source_ids(claims: list[ClaimRecord]) -> list[str]:
    seen: set[str] = set()
    source_ids: list[str] = []
    for claim in claims:
        for source_id in claim.source_ids:
            if source_id not in seen:
                seen.add(source_id)
                source_ids.append(source_id)
    return source_ids


def claim_locations(claims: list[ClaimRecord]) -> list[str]:
    return [claim.location for claim in claims]


def claim_group_label(group_key: tuple[str, str, str, str]) -> str:
    subject, predicate, unit, scope = group_key
    parts = [f"subject={subject}", f"predicate={predicate}"]
    if unit:
        parts.append(f"unit={unit}")
    if scope:
        parts.append(f"scope={scope}")
    return ", ".join(parts)


def claim_comparable_label(comparable: tuple[str, str]) -> str:
    kind, value = comparable
    return f"{kind}:{value}"


def check_claims(
    project_root: Path,
    wiki_root: Path,
    wiki_files: list[Path],
    results: dict[str, Any],
) -> None:
    stats = results["stats"]
    claims = collect_claims(project_root, wiki_root, wiki_files, results)
    groups: dict[tuple[str, str, str, str], list[ClaimRecord]] = {}
    for claim in claims:
        groups.setdefault(claim_group_key(claim), []).append(claim)

    conflict_count = 0
    duplicate_count = 0
    for group_key, group_claims in sorted(groups.items(), key=lambda item: item[0]):
        comparable_groups: dict[tuple[str, str], list[ClaimRecord]] = {}
        for claim in group_claims:
            comparable_groups.setdefault(claim_comparable(claim), []).append(claim)

        if len(comparable_groups) > 1:
            conflict_count += 1
            issue(
                results,
                "HIGH",
                "claim_conflict",
                f"Structured claims conflict for {claim_group_label(group_key)}",
                unique_claim_paths(project_root, group_claims),
                "Review the cited evidence and split scope/unit or correct the conflicting claim value.",
                field="value",
                expected="one value or object for matching subject/predicate/unit/scope",
                actual={
                    "values": sorted(claim_comparable_label(comparable) for comparable in comparable_groups),
                    "source_ids": unique_claim_source_ids(group_claims),
                    "locations": claim_locations(group_claims),
                },
            )

        for comparable, duplicate_claims in sorted(comparable_groups.items(), key=lambda item: item[0]):
            if len(duplicate_claims) <= 1:
                continue
            duplicate_count += 1
            issue(
                results,
                "LOW",
                "claim_near_duplicate",
                f"Structured claims are near-duplicates for {claim_group_label(group_key)}",
                unique_claim_paths(project_root, duplicate_claims),
                "Merge duplicate claims or keep only the strongest evidence-linked claim.",
                field="value",
                expected="single structured claim for each matching claim value",
                actual={
                    "value": claim_comparable_label(comparable),
                    "source_ids": unique_claim_source_ids(duplicate_claims),
                    "locations": claim_locations(duplicate_claims),
                },
            )

    stats["claims_checked"] = len(claims)
    stats["claim_groups"] = len(groups)
    stats["claim_conflicts"] = conflict_count
    stats["claim_near_duplicates"] = duplicate_count
    stats["claim_invalid_records"] = len(
        [item for item in results["issues"] if item.get("category") == "claim_invalid"]
    )


def resolve_answer_page(question_path: Path, answer_page: str) -> Path:
    target = answer_page.split("#", 1)[0].strip()
    return (question_path.parent / target).resolve()


def has_text_field(document: dict[str, Any], key: str) -> bool:
    value = document.get(key)
    return isinstance(value, str) and bool(value.strip())


def is_automated_provenance(provenance: dict[str, Any]) -> bool:
    return has_text_field(provenance, "retrieved_by")


def is_web_curation_record(record: dict[str, Any], provenance: dict[str, Any]) -> bool:
    kind = record.get("kind")
    return kind in WEB_CURATION_KINDS and is_automated_provenance(provenance)


def has_license_or_terms_status(provenance: dict[str, Any]) -> bool:
    if has_text_field(provenance, "license"):
        return True
    if has_text_field(provenance, "terms_url"):
        return True
    if has_text_field(provenance, "terms_note"):
        return True
    return False


def provenance_files(manifest_label: str, provenance: dict[str, Any]) -> list[str]:
    files = [manifest_label]
    sidecar = provenance.get("sidecar_path")
    if isinstance(sidecar, str) and sidecar.strip():
        files.append(sidecar.strip())
    return files


def check_curation_metadata(
    project_root: Path,
    manifest_path: Path,
    manifest_records: list[dict[str, Any]],
    wiki_root: Path,
    wiki_files: list[Path],
    output_root: Path,
    results: dict[str, Any],
) -> None:
    stats = results["stats"]
    manifest_label = project_relative(project_root, manifest_path)
    cited_paths = index_cited_source_paths(project_root, wiki_root, wiki_files, output_root)
    selected_by_request, candidates_label = load_selected_candidate_ids_by_request(project_root)

    for record in manifest_records:
        provenance = record.get("provenance")
        if not isinstance(provenance, dict) or not is_web_curation_record(record, provenance):
            continue
        stats["curation_records_checked"] += 1
        source_id = manifest_id(record) or "<unknown>"
        files = provenance_files(manifest_label, provenance)
        cited_files = cited_paths.get(source_id, [])
        if cited_files:
            stats["curation_cited_records_checked"] += 1

        if not has_license_or_terms_status(provenance):
            stats["curation_missing_terms_license"] += 1
            issue(
                results,
                "LOW",
                "curation_missing_terms_license",
                f"Automated web delivery records no license or terms status: {source_id}",
                files + cited_files,
                "Add `license`, `terms_url`, or `terms_note` to the provenance sidecar.",
                field="provenance.license",
                expected="license, terms_url, or terms_note",
                actual="missing",
            )

        if not cited_files:
            continue

        if not has_text_field(provenance, "notes"):
            stats["curation_missing_source_note"] += 1
            issue(
                results,
                "MEDIUM",
                "curation_missing_source_note",
                f"Cited automated web delivery has no source note or acquisition rationale: {source_id}",
                files + cited_files,
                "Add a short `notes` value explaining why this source was captured.",
                field="provenance.notes",
                expected="short source note or acquisition rationale",
                actual="missing",
                source_id=source_id,
                expected_path=project_relative(project_root, expected_source_note_path(wiki_root, source_id)),
            )
        if not has_text_field(provenance, "origin_url"):
            stats["curation_missing_origin_url"] += 1
            issue(
                results,
                "HIGH",
                "curation_missing_origin_url",
                f"Cited automated web delivery has no origin URL: {source_id}",
                files + cited_files,
                "Add `origin_url` to the provenance sidecar and re-run source_inventory.py.",
                field="provenance.origin_url",
                expected="non-empty origin URL",
                actual="missing",
            )
        checksum = provenance.get("checksum")
        if not (isinstance(checksum, str) and checksum.strip() and provenance.get("checksum_verified") is True):
            stats["curation_missing_checksum"] += 1
            actual = "missing" if not (isinstance(checksum, str) and checksum.strip()) else "unverified"
            issue(
                results,
                "HIGH",
                "curation_missing_checksum",
                f"Cited automated web delivery has no verified checksum: {source_id}",
                files + cited_files,
                "Add a SHA-256 `checksum` sidecar value that verifies against the delivered file.",
                field="provenance.checksum",
                expected="verified sha256 checksum",
                actual=actual,
            )

        request_id = provenance.get("request_id")
        candidate_id = provenance.get("candidate_id")
        if (
            isinstance(request_id, str)
            and request_id.strip()
            and selected_by_request.get(request_id.strip())
            and not (isinstance(candidate_id, str) and candidate_id.strip())
        ):
            stats["curation_missing_candidate_id"] += 1
            candidate_files = files + cited_files
            if candidates_label:
                candidate_files.append(candidates_label)
            issue(
                results,
                "LOW",
                "curation_missing_candidate_id",
                f"Automated web delivery omits selected discovery candidate id: {source_id}",
                candidate_files,
                "Copy the selected candidate id into `provenance.candidate_id`.",
                field="provenance.candidate_id",
                expected=", ".join(selected_by_request[request_id.strip()]),
                actual="missing",
            )


def check_provenance(
    project_root: Path,
    manifest_path: Path,
    manifest_records: list[dict[str, Any]],
    results: dict[str, Any],
) -> None:
    """Automated deliveries (provenance with retrieved_by) must carry a license."""
    stats = results["stats"]
    checked = 0
    missing_license = 0
    unresolved_license = 0
    manifest_label = project_relative(project_root, manifest_path)
    for record in manifest_records:
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            continue
        checked += 1
        check_evidence_usability_override(project_root, manifest_label, record, provenance, results)
        retrieved_by = provenance.get("retrieved_by")
        if not (isinstance(retrieved_by, str) and retrieved_by.strip()):
            continue
        if is_web_curation_record(record, provenance):
            continue
        license_value = provenance.get("license")
        if isinstance(license_value, str) and license_value.strip() and license_value.strip().casefold() != "unresolved":
            continue
        if isinstance(license_value, str) and license_value.strip().casefold() == "unresolved":
            source_id = manifest_id(record) or "<unknown>"
            files = [manifest_label]
            sidecar = provenance.get("sidecar_path")
            if isinstance(sidecar, str) and sidecar.strip():
                files.append(sidecar)
            if isinstance(provenance.get("terms_url"), str) and provenance["terms_url"].strip():
                unresolved_license += 1
                issue(
                    results,
                    "LOW",
                    "provenance_license_unresolved",
                    f"Automated delivery records unresolved license provenance: {source_id}",
                    files,
                    "Run fetch_sources.py openalex enrich for academic acquisitions or replace `unresolved` with a reviewed license.",
                    field="provenance.license",
                    expected="resolved license identifier, with terms_url while unresolved",
                    actual="unresolved",
                )
                continue
        missing_license += 1
        source_id = manifest_id(record) or "<unknown>"
        files = [manifest_label]
        sidecar = provenance.get("sidecar_path")
        if isinstance(sidecar, str) and sidecar.strip():
            files.append(sidecar)
        issue(
            results,
            "MEDIUM",
            "provenance_missing_license",
            f"Automated delivery records no license provenance: {source_id}",
            files,
            "Add `license` to the provenance sidecar and re-run source_inventory.py.",
            field="provenance.license",
            expected="non-empty license identifier",
            actual="empty",
        )
    stats["provenance_records"] = checked
    stats["provenance_missing_license"] = missing_license
    stats["provenance_license_unresolved"] = unresolved_license


def check_openalex_identity_conflicts(
    project_root: Path,
    manifest_path: Path,
    manifest_records: list[dict[str, Any]],
    results: dict[str, Any],
) -> None:
    stats = results["stats"]
    manifest_label = project_relative(project_root, manifest_path)
    count = 0
    for record in manifest_records:
        provenance = record.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("openalex_identity_conflict") is not True:
            continue
        count += 1
        source_id = manifest_id(record) or "<unknown>"
        files = [manifest_label]
        sidecar = provenance.get("sidecar_path")
        if isinstance(sidecar, str) and sidecar.strip():
            files.append(sidecar)
        issue(
            results,
            "LOW",
            "openalex_identity_conflict",
            f"OpenAlex identity conflict recorded for {source_id}.",
            files,
            "Keep the recorded conflict evidence with citation verification artifacts; do not treat the OpenAlex work id as clean identity.",
            field="provenance.openalex_identity_conflict",
            expected="absent unless OpenAlex returned a divergent work record",
            actual="true",
        )
    stats["openalex_identity_conflict"] = count


def incomplete_override_fields(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["mapping"]
    missing: list[str] = []
    if value.get("usable") is not True:
        missing.append("usable: true")
    for key in ("reviewed_by", "reviewed_at", "reason"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            missing.append(key)
    return missing


def check_evidence_usability_override(
    project_root: Path,
    manifest_label: str,
    record: dict[str, Any],
    provenance: dict[str, Any],
    results: dict[str, Any],
) -> None:
    override = provenance.get("evidence_usability_override")
    if override is None:
        return
    stats = results["stats"]
    stats["evidence_usability_overrides"] = int(stats.get("evidence_usability_overrides", 0) or 0) + 1
    source_id = manifest_id(record) or "<unknown>"
    files = [manifest_label]
    sidecar = provenance.get("sidecar_path")
    if isinstance(sidecar, str) and sidecar.strip():
        files.append(sidecar)
    missing = incomplete_override_fields(override)
    if missing:
        issue(
            results,
            "HIGH",
            "evidence_usability_override",
            f"Evidence usability override for {source_id} is incomplete: missing {', '.join(missing)}",
            files,
            "Record usable: true plus reviewed_by, reviewed_at, and reason, then re-run source_inventory.py.",
            field="provenance.evidence_usability_override",
            expected="complete audited override",
            actual=actual_type(override) if not isinstance(override, dict) else ", ".join(missing),
            source_id=source_id,
        )
        return
    delivery_reasons = delivery_unusable_evidence_reasons(provenance)
    if delivery_reasons:
        issue(
            results,
            "HIGH",
            "evidence_usability_override",
            (
                f"Evidence usability override for {source_id} attempts to override a delivery failure: "
                + ", ".join(delivery_reasons)
            ),
            files,
            "Resolve the source delivery failure or keep the source request blocked; overrides only audit source-usability false positives.",
            field="provenance.evidence_usability_override",
            expected="no source_status or delivery_failure_code blocker",
            actual=", ".join(delivery_reasons),
            source_id=source_id,
        )


def check_output_license_status(
    project_root: Path,
    manifest_path: Path,
    manifest_records: list[dict[str, Any]],
    output_root: Path,
    results: dict[str, Any],
) -> None:
    """Fetched sources cited by reusable output pages must surface license status."""
    stats = results["stats"]
    manifest_by_id = index_manifest_records(manifest_records)
    manifest_label = project_relative(project_root, manifest_path)
    checked = 0
    missing = 0

    for path in markdown_files(output_root):
        frontmatter, error = load_frontmatter(path)
        if error is not None or not isinstance(frontmatter, dict):
            continue
        label = project_relative(project_root, path)
        for source_id in dict.fromkeys(source_ids_from_frontmatter(frontmatter)):
            record = manifest_by_id.get(source_id)
            if record is None:
                continue
            provenance = record.get("provenance")
            if not isinstance(provenance, dict):
                continue
            retrieved_by = provenance.get("retrieved_by")
            if not (isinstance(retrieved_by, str) and retrieved_by.strip()):
                continue
            checked += 1
            if is_web_curation_record(record, provenance):
                if has_license_or_terms_status(provenance):
                    continue
            elif has_text_field(provenance, "license"):
                continue
            missing += 1
            files = [label, manifest_label]
            sidecar = provenance.get("sidecar_path")
            if isinstance(sidecar, str) and sidecar.strip():
                files.append(sidecar)
            issue(
                results,
                "LOW",
                "output_license_missing",
                f"Output page cites fetched source without visible license status: {source_id}",
                files,
                "Record a concrete provenance license before relying on this fetched source in reusable outputs, "
                "or surface the license uncertainty in the output handoff.",
                field="source_ids",
                expected="cited fetched source with non-empty provenance.license",
                actual=source_id,
            )

    stats["output_license_records_checked"] = checked
    stats["output_license_missing"] = missing


def is_academic_provider_provenance(provenance: dict[str, Any]) -> bool:
    provider = provenance.get("academic_provider")
    if isinstance(provider, str) and provider.strip() in {"arxiv", "openalex"}:
        return True
    retrieved_by = provenance.get("retrieved_by")
    return isinstance(retrieved_by, str) and retrieved_by.strip() in {
        "fetch_sources.py/arxiv",
        "fetch_sources.py/openalex",
    }


def check_output_academic_publication_metadata(
    project_root: Path,
    manifest_path: Path,
    manifest_records: list[dict[str, Any]],
    output_root: Path,
    results: dict[str, Any],
) -> None:
    """Academic provider-backed output citations must surface venue/status."""
    stats = results["stats"]
    manifest_by_id = index_manifest_records(manifest_records)
    manifest_label = project_relative(project_root, manifest_path)
    checked = 0
    missing = 0

    for path in markdown_files(output_root):
        frontmatter, error = load_frontmatter(path)
        if error is not None or not isinstance(frontmatter, dict):
            continue
        label = project_relative(project_root, path)
        for source_id in dict.fromkeys(source_ids_from_frontmatter(frontmatter)):
            record = manifest_by_id.get(source_id)
            if record is None:
                continue
            provenance = record.get("provenance")
            if not isinstance(provenance, dict) or not is_academic_provider_provenance(provenance):
                continue
            checked += 1
            if has_text_field(provenance, "venue") and has_text_field(provenance, "peer_review_status"):
                continue
            missing += 1
            files = [label, manifest_label]
            sidecar = provenance.get("sidecar_path")
            if isinstance(sidecar, str) and sidecar.strip():
                files.append(sidecar)
            issue(
                results,
                "LOW",
                "academic_metadata_missing",
                f"Output page cites academic source without venue or peer-review status: {source_id}",
                files,
                "Record academic venue and peer-review/publication status in provenance before relying on this "
                "source in reusable outputs.",
                field="source_ids",
                expected="cited academic source with provenance.venue and provenance.peer_review_status",
                actual=source_id,
            )

    stats["academic_metadata_records_checked"] = checked
    stats["academic_metadata_missing"] = missing


def load_source_request_records(
    requests_path: Path,
    project_root: Path,
    results: dict[str, Any],
) -> list[dict[str, Any]]:
    """Read source-request lines, reporting malformed lines instead of failing."""
    records: list[dict[str, Any]] = []
    if not requests_path.is_file():
        return records
    label = project_relative(project_root, requests_path)
    for line_number, line in enumerate(requests_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        if not isinstance(record, dict):
            issue(
                results,
                "MEDIUM",
                "source_request_invalid",
                f"Source request line is not a valid JSON object: {label}:{line_number}",
                [label],
                "Repair or remove the malformed source-request line.",
            )
            continue
        records.append(record)
    return records


def check_source_requests(
    project_root: Path,
    requests_path: Path,
    manifest_records: list[dict[str, Any]],
    wiki_files: list[Path],
    results: dict[str, Any],
) -> None:
    stats = results["stats"]
    requests = load_source_request_records(requests_path, project_root, results)
    requests_label = project_relative(project_root, requests_path)
    manifest_ids = set(index_manifest_records(manifest_records))
    referenced_slugs: set[str] = set()
    status_counts = {"open": 0, "fulfilled": 0}

    for record in requests:
        status = record.get("status")
        if status in status_counts:
            status_counts[status] += 1
            slugs = record.get("question_slugs")
            if isinstance(slugs, list):
                referenced_slugs.update(slug for slug in slugs if isinstance(slug, str))
        if status == "fulfilled":
            source_id = record.get("source_id")
            if not (isinstance(source_id, str) and source_id in manifest_ids):
                request_id = record.get("request_id") if isinstance(record.get("request_id"), str) else "<unknown>"
                issue(
                    results,
                    "MEDIUM",
                    "request_fulfilled_missing_source",
                    f"Fulfilled source request points to a missing manifest source: {request_id}",
                    [requests_label],
                    "Re-run source_inventory.py after delivering the files, or fix the request's source_id.",
                    field="source_id",
                    expected="existing manifest source id",
                    actual=source_id if isinstance(source_id, str) else "missing",
                )

    for path in wiki_files:
        frontmatter, error = load_frontmatter(path)
        if error is not None or not isinstance(frontmatter, dict):
            continue
        if frontmatter.get("type") != "question" or frontmatter.get("status") != "blocked":
            continue
        slug = path.stem
        if slug in referenced_slugs:
            continue
        label = project_relative(project_root, path)
        issue(
            results,
            "LOW",
            "question_blocked_no_request",
            f"Blocked question has no linked source request: {label}",
            [label],
            f"Record the missing evidence: scripts/source_requests.py add --question-slug {slug} ...",
            field="question_slugs",
            expected="open or fulfilled source request referencing this question slug",
            actual="none",
        )

    stats["source_requests_total"] = len(requests)
    stats["source_requests_open"] = status_counts["open"]
    stats["source_requests_fulfilled"] = status_counts["fulfilled"]


def answered_question_is_grounded(question_frontmatter: dict[str, Any], answer_path: Path) -> bool:
    """An answered question is grounded when it or its answer page cites source_ids."""
    if source_ids_from_frontmatter(question_frontmatter):
        return True
    answer_frontmatter, error = load_frontmatter(answer_path)
    if error is not None or not isinstance(answer_frontmatter, dict):
        return False
    return bool(source_ids_from_frontmatter(answer_frontmatter))


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


def claim_staleness_window_hours(config: dict[str, Any]) -> int:
    run_config = config.get("run") if isinstance(config.get("run"), dict) else {}
    value = run_config.get("claim_staleness_hours", DEFAULT_CLAIM_STALENESS_HOURS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_CLAIM_STALENESS_HOURS
    return value


def check_question_claim(
    results: dict[str, Any],
    label: str,
    frontmatter: dict[str, Any],
    staleness_hours: int,
) -> None:
    """Validate claim fields on an in_progress question."""
    claimed_by = frontmatter.get("claimed_by")
    has_agent = isinstance(claimed_by, str) and bool(claimed_by.strip())
    claimed_at = frontmatter.get("claimed_at")
    parsed_at = parse_claim_timestamp(claimed_at)
    if not has_agent or parsed_at is None:
        if not has_agent:
            actual = "missing" if "claimed_by" not in frontmatter else "empty"
        else:
            actual = "missing" if "claimed_at" not in frontmatter else "unparseable"
        issue(
            results,
            "MEDIUM",
            "question_claim_missing",
            f"in_progress question is missing claim fields: {label}",
            [label],
            "Claim questions via scripts/question_claim.py claim --slug ... --agent-id ... "
            "so claimed_by and claimed_at (ISO 8601) are recorded.",
            field="claimed_by" if not has_agent else "claimed_at",
            expected="non-empty claimed_by and ISO 8601 claimed_at while in_progress",
            actual=actual,
        )
        return
    age_hours = (datetime.now(timezone.utc) - parsed_at).total_seconds() / 3600
    if age_hours > staleness_hours:
        issue(
            results,
            "LOW",
            "question_claim_stale",
            f"claim by {claimed_by.strip()} is {age_hours:.1f}h old "
            f"(staleness window: {staleness_hours}h): {label}",
            [label],
            "Verify the agent is still working; an orchestrator can recover via "
            "question_claim.py claim --steal --if-older-than HOURS.",
            field="claimed_at",
            expected=f"claim younger than {staleness_hours}h (run.claim_staleness_hours)",
            actual=f"{age_hours:.1f}h",
        )


def check_answered_question_coverage(
    project_root: Path,
    config: dict[str, Any],
    path: Path,
    frontmatter: dict[str, Any],
    results: dict[str, Any],
) -> None:
    label = project_relative(project_root, path)
    required = frontmatter.get("coverage_required")
    if required is None or required is False:
        return
    if not isinstance(required, bool):
        issue(
            results,
            "HIGH",
            "question_coverage_invalid",
            f"Answered question has invalid coverage_required value: {label}",
            [label],
            "Set `coverage_required: true` or remove the field for questions that do not require coverage.",
            field="coverage_required",
            expected="boolean",
            actual=actual_type(required),
        )
        return

    summary = coverage_manifest.coverage_summary_for_question(project_root, config, path.stem, frontmatter)
    manifest_label = summary.get("coverage_manifest")
    files = [label]
    if isinstance(manifest_label, str) and manifest_label:
        files.append(manifest_label)
    status = summary["coverage_status"]
    if status == "missing":
        issue(
            results,
            "HIGH",
            "question_coverage_missing",
            f"Answered coverage-required question is missing its coverage manifest: {manifest_label}",
            files,
            "Create or select a coverage manifest under sources.coverage_dir and evaluate it before marking the question answered.",
            field="coverage_manifest",
            expected="present coverage manifest",
            actual="missing",
        )
    elif status == "invalid":
        issue(
            results,
            "HIGH",
            "question_coverage_invalid",
            f"Answered coverage-required question has invalid coverage manifest: {manifest_label}",
            files,
            summary.get("error") or "Fix the coverage manifest so it matches docs/coverage-manifest.md.",
            field="coverage_manifest",
            expected="valid coverage manifest for this question slug",
            actual="invalid",
        )
    elif summary.get("coverage_verdict") != "pass":
        issue(
            results,
            "HIGH",
            "question_coverage_blocked",
            f"Answered coverage-required question does not have passing required coverage: {manifest_label}",
            files,
            "Resolve failed required facets with accepted sources or linked source requests before marking the question answered.",
            field="coverage_verdict",
            expected="pass",
            actual=str(summary.get("coverage_verdict") or status),
        )


def valid_grounding_entries(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        for field in ("claim", "source_id", "quote"):
            field_value = item.get(field)
            if not isinstance(field_value, str) or not field_value.strip():
                return False
        location_hint = item.get("location_hint")
        if location_hint is not None and not isinstance(location_hint, str):
            return False
    return True


def check_answered_question_grounding(
    project_root: Path,
    path: Path,
    frontmatter: dict[str, Any],
    results: dict[str, Any],
) -> None:
    label = project_relative(project_root, path)
    if frontmatter.get("coverage_required") is True and not valid_grounding_entries(frontmatter.get("grounding")):
        issue(
            results,
            "HIGH",
            "question_grounding_missing",
            f"Answered coverage-required question is missing valid grounding quotes: {label}",
            [label],
            "Add `grounding` entries with claim, source_id, quote, and optional location_hint before marking the answer publishable.",
            field="grounding",
            expected="non-empty grounding entries with claim/source_id/quote",
            actual=actual_type(frontmatter.get("grounding")) if "grounding" in frontmatter else "missing",
        )
    answered_by = frontmatter.get("answered_by")
    if not isinstance(answered_by, str) or not answered_by.strip():
        answered_by = frontmatter.get("claimed_by")
    verified_by = frontmatter.get("verified_by")
    if (
        isinstance(answered_by, str)
        and answered_by.strip()
        and isinstance(verified_by, str)
        and verified_by.strip()
        and answered_by.strip() == verified_by.strip()
    ):
        issue(
            results,
            "MEDIUM",
            "question_grounding_self_verified",
            f"Answered question was verified by the answering agent: {label}",
            [label],
            "Run scripts/verify_quotes.py --write with a distinct verifier agent before final publication.",
            field="verified_by",
            expected="verifier distinct from answered_by",
            actual=verified_by.strip(),
        )


def check_questions(
    project_root: Path,
    wiki_files: list[Path],
    staleness_hours: int,
    results: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> None:
    config = config or {}
    stats = results["stats"]
    checked = 0
    status_counts: dict[str, int] = {}
    for path in wiki_files:
        frontmatter, error = load_frontmatter(path)
        if error is not None or not isinstance(frontmatter, dict):
            continue
        if frontmatter.get("type") != "question":
            continue
        checked += 1
        label = project_relative(project_root, path)
        status = frontmatter.get("status")
        status_key = status if isinstance(status, str) else "unknown"
        status_counts[status_key] = status_counts.get(status_key, 0) + 1

        if status in {"answered", "human_review"}:
            if status == "human_review":
                issue(
                    results,
                    "MEDIUM",
                    "question_human_review_pending",
                    f"Question answer is pending human review approval: {label}",
                    [label],
                    "Run `question_resolve.py approve --slug SLUG --reviewer REVIEWER` after a reviewer signs off.",
                    field="status",
                    expected="answered with human_review_status: approved",
                    actual="human_review",
                )
            answer_page = frontmatter.get("answer_page")
            if not isinstance(answer_page, str) or not answer_page.strip():
                issue(
                    results,
                    "MEDIUM",
                    "question_unresolved",
                    f"Answered question is missing `answer_page`: {label}",
                    [label],
                    "Link the wiki page that answers the question via `answer_page`, "
                    "or change the status away from `answered`.",
                    field="answer_page",
                    expected="relative link to an existing wiki page",
                    actual="empty",
                )
            else:
                answer_path = resolve_answer_page(path, answer_page)
                if not answer_path.is_file():
                    issue(
                        results,
                        "MEDIUM",
                        "question_answer_missing",
                        f"Question `answer_page` does not resolve to a file: {label}",
                        [label],
                        "Point `answer_page` at an existing wiki page relative to the question page.",
                        field="answer_page",
                        expected="path to an existing wiki page",
                        actual=answer_page,
                    )
                elif not answered_question_is_grounded(frontmatter, answer_path):
                    issue(
                        results,
                        "MEDIUM",
                        "question_answer_ungrounded",
                        f"Answered question cites no source evidence: {label}",
                        [label],
                        "Cite `source_ids` on the question or its answer page so the "
                        "answer is traceable to sources.",
                        field="source_ids",
                        expected="at least one source_id on the question or answer page",
                        actual="empty",
                    )
            check_answered_question_coverage(project_root, config, path, frontmatter, results)
            check_answered_question_grounding(project_root, path, frontmatter, results)
        elif status == "blocked":
            blocked_reason = frontmatter.get("blocked_reason")
            if not isinstance(blocked_reason, str) or not blocked_reason.strip():
                issue(
                    results,
                    "MEDIUM",
                    "question_blocked_reason",
                    f"Blocked question is missing `blocked_reason`: {label}",
                    [label],
                    "Record why the question is blocked and what evidence is needed via "
                    "`blocked_reason`.",
                    field="blocked_reason",
                    expected="non-empty explanation",
                    actual="empty",
                )
        elif status == "in_progress":
            check_question_claim(results, label, frontmatter, staleness_hours)

    stats["questions_checked"] = checked
    stats["question_status_counts"] = status_counts


def check_source_coverage(
    project_root: Path,
    manifest_path: Path,
    manifest_records: list[dict[str, Any]],
    normalized_root: Path,
    wiki_root: Path,
    wiki_files: list[Path],
    results: dict[str, Any],
) -> None:
    stats = results["stats"]
    manifest_by_id = index_manifest_records(manifest_records)
    expected_paths_by_id = {
        source_id: normalized_output_path(normalized_root, source_id)
        for source_id in manifest_by_id
    }
    expected_ids_by_path: dict[Path, str] = {}
    for source_id, path in expected_paths_by_id.items():
        try:
            expected_ids_by_path[path.resolve()] = source_id
        except OSError:
            expected_ids_by_path[path] = source_id

    normalized_by_id, manual_normalized_ids, unknown_normalized_paths, failed_normalized_paths = index_normalized_sources(
        normalized_root,
        expected_ids_by_path,
    )
    for failed_path in failed_normalized_paths:
        label = project_relative(project_root, failed_path)
        issue(
            results,
            "HIGH",
            "pdf_extraction_failed",
            f"Normalized source has status `failed` — extraction produced no usable text: {label}",
            [label],
            "Re-run normalization after verifying the source file, or supply manually extracted text.",
        )

    # Warn about normalized PDF records whose inferred title is uncertain.
    for norm_path in sorted(normalized_root.rglob("*.md"), key=lambda p: p.as_posix()) if normalized_root.is_dir() else []:
        fm, _ = load_frontmatter(norm_path)
        if not isinstance(fm, dict):
            continue
        if fm.get("status") == "failed":
            continue
        if fm.get("extraction_method") != "pdf_text":
            continue
        if fm.get("needs_ocr") is True:
            label = project_relative(project_root, norm_path)
            issue(
                results,
                "LOW",
                "pdf_needs_ocr",
                f"Normalized PDF is likely scanned or image-only (`needs_ocr: true`): {label}",
                [label],
                "Run OCR out of band and deliver the extracted text alongside the PDF, "
                "or accept the degraded record. See docs/normalized-source-format.md.",
            )
        tc = fm.get("title_confidence")
        if tc in ("low", "none"):
            label = project_relative(project_root, norm_path)
            issue(
                results,
                "WARNING",
                "pdf_title_uncertain",
                f"PDF title inference produced low-confidence result (`title_confidence: {tc}`): {label}",
                [label],
                "Verify the `title:` field in the normalized record and correct it if needed.",
            )

    source_notes_by_id, note_integrated_ids = index_source_notes(wiki_root)
    integration_by_id = index_integration_citations(wiki_root, wiki_files)
    valid_source_ids = set(manifest_by_id) | manual_normalized_ids

    missing_normalized = 0
    missing_source_notes = 0
    missing_integrations = 0
    lifecycle_counts = {
        "discovered": 0,
        "normalized": 0,
        "noted": 0,
        "integrated": 0,
        "deferred": 0,
        "rejected": 0,
        "superseded": 0,
    }
    inactive_statuses = {"deferred", "rejected", "superseded"}
    coverage_rows: list[dict[str, Any]] = []

    for source_id, record in sorted(manifest_by_id.items()):
        expected_path = expected_paths_by_id[source_id]
        normalized_paths = normalized_by_id.get(source_id, [])
        normalized = bool(normalized_paths) or expected_path.is_file()
        noted = source_id in source_notes_by_id
        integrated = source_id in integration_by_id
        manifest_status = record.get("status") if isinstance(record.get("status"), str) else None
        intentionally_inactive = manifest_status in inactive_statuses

        if intentionally_inactive:
            effective_status = manifest_status
        elif integrated:
            effective_status = "integrated"
        elif noted:
            effective_status = "noted"
        elif normalized:
            effective_status = "normalized"
        else:
            effective_status = "discovered"
        if effective_status not in lifecycle_counts:
            lifecycle_counts[effective_status] = 0
        lifecycle_counts[effective_status] += 1

        if not normalized and not intentionally_inactive:
            missing_normalized += 1
            issue(
                results,
                "LOW",
                "source_missing_normalized",
                f"Manifest source is missing normalized record: {source_id}",
                [project_relative(project_root, manifest_path), project_relative(project_root, expected_path)],
                "Run source normalization for this source or mark it rejected/superseded.",
                field="source_id",
                expected=project_relative(project_root, expected_path),
                actual=source_id,
            )
        if normalized and not noted and not intentionally_inactive:
            missing_source_notes += 1
            files = normalized_paths or [expected_path]
            issue(
                results,
                "LOW",
                "normalized_missing_source_note",
                f"Normalized source is missing a wiki source note: {source_id}",
                source_paths_for_result(project_root, files),
                "Create a wiki/sources note with this source ID in frontmatter.",
                field="source_ids",
                expected="source note citing this source ID",
                actual=source_id,
                source_id=source_id,
                expected_path=project_relative(project_root, expected_source_note_path(wiki_root, source_id)),
            )
        if (manifest_status == "integrated" or source_id in note_integrated_ids) and not integrated:
            missing_integrations += 1
            issue(
                results,
                "MEDIUM",
                "integrated_missing_citation",
                f"Source is marked integrated but no integration page cites it: {source_id}",
                source_paths_for_result(project_root, source_notes_by_id.get(source_id, [])) or [
                    project_relative(project_root, manifest_path)
                ],
                "Cite this source ID from a concept, method, system, or synthesis page, or lower its status.",
                field="source_ids",
                expected="citation from concept/method/system/synthesis page",
                actual=source_id,
            )

        coverage_rows.append(
            {
                "source_id": source_id,
                "manifest_status": manifest_status,
                "effective_status": effective_status,
                "normalized_path": project_relative(project_root, expected_path),
                "source_notes": source_paths_for_result(project_root, source_notes_by_id.get(source_id, [])),
                "integration_pages": source_paths_for_result(project_root, integration_by_id.get(source_id, [])),
            }
        )

    for source_id, paths in sorted(normalized_by_id.items()):
        if source_id in manifest_by_id:
            continue
        if source_id.startswith("manual:"):
            if source_id not in source_notes_by_id:
                missing_source_notes += 1
                issue(
                    results,
                    "LOW",
                    "normalized_missing_source_note",
                    f"Manual normalized source is missing a wiki source note: {source_id}",
                    source_paths_for_result(project_root, paths),
                    "Create a wiki/sources note with this manual source ID in frontmatter.",
                    field="source_ids",
                    expected="source note citing this manual source ID",
                    actual=source_id,
                    source_id=source_id,
                    expected_path=project_relative(project_root, expected_source_note_path(wiki_root, source_id)),
                )
            continue
        issue(
            results,
            "LOW",
            "normalized_orphan",
            f"Normalized source has no matching manifest record: {source_id}",
            source_paths_for_result(project_root, paths),
            "Add the source to the manifest or remove the stale normalized record.",
            field="source_id",
            expected="manifest record or manual:* source ID",
            actual=source_id,
        )

    for path in unknown_normalized_paths:
        issue(
            results,
            "LOW",
            "normalized_orphan",
            f"Normalized source cannot be matched to a manifest source: {project_relative(project_root, path)}",
            [project_relative(project_root, path)],
            "Add source_id frontmatter or remove the stale normalized record.",
            field="source_id",
            expected="source_id frontmatter or expected normalized filename",
            actual="missing",
        )

    for source_id, paths in sorted(source_notes_by_id.items()):
        if source_id in valid_source_ids:
            continue
        issue(
            results,
            "LOW",
            "source_note_unknown_source",
            f"Source note cites an unknown source ID: {source_id}",
            source_paths_for_result(project_root, paths),
            "Add the source to the manifest/normalized records or fix the source_ids value.",
            field="source_ids",
            expected="manifest source ID or normalized manual source ID",
            actual=source_id,
        )

    check_codebase_evidence_links(
        project_root,
        manifest_by_id,
        normalized_by_id,
        source_notes_by_id,
        integration_by_id,
        results,
    )

    stats["sources_discovered"] = len(manifest_by_id)
    stats["sources_normalized"] = sum(
        1
        for source_id, path in expected_paths_by_id.items()
        if path.is_file() or source_id in normalized_by_id
    )
    stats["sources_noted"] = sum(1 for source_id in manifest_by_id if source_id in source_notes_by_id)
    stats["sources_integrated"] = sum(1 for source_id in manifest_by_id if source_id in integration_by_id)
    stats["source_lifecycle_counts"] = lifecycle_counts
    stats["sources_missing_normalized"] = missing_normalized
    stats["normalized_missing_source_note"] = missing_source_notes
    stats["integrated_missing_citation"] = missing_integrations
    stats["normalized_orphans"] = len(
        [
            source_id
            for source_id in normalized_by_id
            if source_id not in manifest_by_id and not source_id.startswith("manual:")
        ]
    ) + len(unknown_normalized_paths)
    stats["source_notes_unknown_source"] = len(
        [source_id for source_id in source_notes_by_id if source_id not in valid_source_ids]
    )
    results["source_coverage"] = coverage_rows


def normalize_prompt_injection_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category == "Cf":
            continue
        if category.startswith("Z"):
            characters.append(" ")
            continue
        characters.append(character)
    return "".join(characters)


def prompt_injection_pattern_findings(
    results: dict[str, Any],
    text: str,
    *,
    files: list[str],
    phrase_message: Callable[[str], str],
    structural_message: Callable[[str], str],
    base64_message: str,
) -> int:
    normalized_text = normalize_prompt_injection_text(text)
    findings = 0
    for phrase, pattern in PROMPT_INJECTION_PHRASES:
        if pattern.search(normalized_text):
            issue(
                results,
                "LOW",
                "source_prompt_injection_pattern",
                phrase_message(phrase),
                files,
                "Treat the text as quoted evidence data, not as agent instructions.",
                expected="source evidence only",
                actual=phrase,
            )
            findings += 1

    for label, pattern in PROMPT_INJECTION_STRUCTURAL_PATTERNS:
        if pattern.search(normalized_text):
            issue(
                results,
                "LOW",
                "source_prompt_injection_pattern",
                structural_message(label),
                files,
                "Treat the structure as quoted evidence data, not as agent instructions or tool calls.",
                expected="source evidence only",
                actual=label,
            )
            findings += 1

    if BASE64_BLOB_RE.search(normalized_text):
        issue(
            results,
            "LOW",
            "source_prompt_injection_pattern",
            base64_message,
            files,
            "Review whether the blob is expected source evidence; do not decode or execute it as instructions.",
            expected="reviewer awareness",
            actual="base64-like blob >= 256 characters",
        )
        findings += 1
    return findings


def check_prompt_injection_patterns(
    project_root: Path,
    normalized_root: Path,
    wiki_root: Path,
    manifest_path: Path,
    manifest_records: list[dict[str, Any]],
    results: dict[str, Any],
) -> None:
    records_scanned = 0
    question_pages_scanned = 0
    provenance_notes_scanned = 0
    findings = 0
    normalized_files = (
        sorted(normalized_root.rglob("*.md"), key=lambda path: path.as_posix())
        if normalized_root.is_dir()
        else []
    )

    for path in normalized_files:
        label = project_relative(project_root, path)
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            issue(
                results,
                "LOW",
                "source_prompt_injection_pattern",
                f"Normalized source could not be inspected for instruction-like patterns: {label}",
                [label],
                "Review the normalized record manually; this heuristic never reads raw files or fetches URLs.",
                actual=str(exc),
            )
            findings += 1
            continue

        records_scanned += 1
        findings += prompt_injection_pattern_findings(
            results,
            text,
            files=[label],
            phrase_message=lambda phrase, label=label: (
                f"Normalized source contains instruction-like text (`{phrase}`): {label}"
            ),
            structural_message=lambda shape, label=label: (
                f"Normalized source contains structural prompt-injection shape (`{shape}`): {label}"
            ),
            base64_message=f"Normalized source contains a large base64-like blob: {label}",
        )

    question_root = wiki_root / QUESTION_WIKI_DIR
    question_files = (
        sorted(question_root.rglob("*.md"), key=lambda path: path.as_posix()) if question_root.is_dir() else []
    )
    for path in question_files:
        label = project_relative(project_root, path)
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            issue(
                results,
                "LOW",
                "source_prompt_injection_pattern",
                f"Question page could not be inspected for instruction-like patterns: {label}",
                [label],
                "Review the question page manually; this heuristic never reads raw files or fetches URLs.",
                actual=str(exc),
            )
            findings += 1
            continue

        question_pages_scanned += 1
        findings += prompt_injection_pattern_findings(
            results,
            text,
            files=[label],
            phrase_message=lambda phrase, label=label: (
                f"Question page contains instruction-like text (`{phrase}`): {label}"
            ),
            structural_message=lambda shape, label=label: (
                f"Question page contains structural prompt-injection shape (`{shape}`): {label}"
            ),
            base64_message=f"Question page contains a large base64-like blob: {label}",
        )

    manifest_label = project_relative(project_root, manifest_path)
    for record in manifest_records:
        provenance = record.get("provenance")
        if not isinstance(provenance, dict):
            continue
        notes = provenance.get("notes")
        if not (isinstance(notes, str) and notes.strip()):
            continue
        provenance_notes_scanned += 1
        source_id = manifest_id(record) or "<unknown>"
        files = [manifest_label]
        sidecar = provenance.get("sidecar_path")
        if isinstance(sidecar, str) and sidecar.strip():
            files.append(sidecar)
        findings += prompt_injection_pattern_findings(
            results,
            notes,
            files=files,
            phrase_message=lambda phrase, source_id=source_id: (
                f"Provenance notes contain instruction-like text (`{phrase}`) for {source_id}: {manifest_label}"
            ),
            structural_message=lambda shape, source_id=source_id: (
                f"Provenance notes contain structural prompt-injection shape (`{shape}`) "
                f"for {source_id}: {manifest_label}"
            ),
            base64_message=f"Provenance notes contain a large base64-like blob for {source_id}: {manifest_label}",
        )

    results["stats"]["prompt_injection_records_scanned"] = records_scanned
    results["stats"]["prompt_injection_question_pages_scanned"] = question_pages_scanned
    results["stats"]["prompt_injection_provenance_notes_scanned"] = provenance_notes_scanned
    results["stats"]["prompt_injection_findings"] = findings


def check_codebase_analysis_untrusted_input_acknowledgement(config: dict[str, Any], results: dict[str, Any]) -> None:
    integrations = config_mapping(config, "integrations")
    codebase = integrations.get("codebase_analysis")
    if not isinstance(codebase, dict) or codebase.get("enabled") is not True:
        return
    acknowledgement = codebase.get("untrusted_input")
    if acknowledgement == CODEBASE_UNTRUSTED_INPUT_ACKNOWLEDGEMENT:
        return
    issue(
        results,
        "LOW",
        "codebase_untrusted_input",
        "enabled codebase analysis requires acknowledgement that raw/code/ is untrusted input",
        ["research.yml"],
        "Treat raw/code/ as untrusted input; set integrations.codebase_analysis.untrusted_input: acknowledged only after choosing an adapter safe for untrusted input.",
        field="integrations.codebase_analysis.untrusted_input",
        expected=CODEBASE_UNTRUSTED_INPUT_ACKNOWLEDGEMENT,
        actual=acknowledgement,
    )


def check_codebase_execution_scope(config: dict[str, Any], results: dict[str, Any]) -> None:
    """Assert that codebase support is inert artifact intake, not execution."""

    results["config"]["codebase_execution_scope"] = "external_artifact_only"
    results["stats"]["codebase_product_execution"] = False
    integrations = config_mapping(config, "integrations")
    codebase = integrations.get("codebase_analysis")
    if not isinstance(codebase, dict) or codebase.get("enabled") is not True:
        return
    command = codebase.get("command")
    if command not in (None, "", []):
        issue(
            results,
            "LOW",
            "codebase_execution_scope",
            "codebase_analysis.command is legacy display-only configuration; product-side execution is not shipped",
            ["research.yml"],
            (
                "Set integrations.codebase_analysis.command to null and have a separately authorized external worker "
                "deposit an inert artifact plus artifact-manifest.json."
            ),
            field="integrations.codebase_analysis.command",
            expected=None,
            actual=command,
        )
    unsafe_fields = {
        "read_only": codebase.get("read_only") is not True,
        "install_hooks": codebase.get("install_hooks") is not False,
        "background_sync": codebase.get("background_sync") is not False,
    }
    for field, unsafe in unsafe_fields.items():
        if not unsafe:
            continue
        expected = True if field == "read_only" else False
        issue(
            results,
            "HIGH",
            "codebase_execution_scope",
            f"unsafe codebase intake setting is outside the shipped nonexecution scope: {field}",
            ["research.yml"],
            "Restore read-only, plugin-free/no-hook, no-background defaults before ingesting code artifacts.",
            field=f"integrations.codebase_analysis.{field}",
            expected=expected,
            actual=codebase.get(field),
        )


def severity_order(config: dict[str, Any]) -> list[str]:
    lint_config = config_mapping(config, "lint")
    levels = config_list(lint_config.get("severity_levels"), "lint.severity_levels")
    return levels or ["HIGH", "MEDIUM", "LOW"]


def issue_counts(results: dict[str, Any], levels: list[str]) -> dict[str, int]:
    counts = {level: 0 for level in levels}
    for item in results["issues"]:
        severity = item.get("severity")
        if isinstance(severity, str):
            counts[severity] = counts.get(severity, 0) + 1
    return counts


def generate_recommendations(results: dict[str, Any]) -> None:
    categories = {item["category"] for item in results["issues"]}
    recommendations: list[str] = []
    if "config_path" in categories:
        recommendations.append("Keep configured paths workspace-relative and inside the research workspace.")
    if "structure" in categories:
        recommendations.append("Align wiki.required_dirs with the filesystem.")
    if "raw_sources" in categories:
        recommendations.append("Create or reconfigure missing raw source roots.")
    if "frontmatter" in categories:
        recommendations.append("Repair wiki page frontmatter before ingestion or synthesis.")
    if "broken_link" in categories:
        recommendations.append("Fix broken Markdown links before relying on the wiki index.")
    if "broken_wikilink" in categories:
        recommendations.append("Fix broken [[wikilinks]] or create the linked notes.")
    if "index" in categories or "orphan" in categories:
        recommendations.append("Add static index links or Dataview FROM sections for orphan wiki pages.")
    if "source_manifest" in categories or "source_status" in categories:
        recommendations.append("Regenerate or repair the source manifest.")
    if categories.intersection(
        {
            "source_missing_normalized",
            "normalized_missing_source_note",
            "integrated_missing_citation",
            "normalized_orphan",
            "source_note_unknown_source",
        }
    ):
        recommendations.append(
            "Advance source coverage from manifest to normalized records, source notes, and cited synthesis pages."
        )
    if categories.intersection({"claim_invalid", "claim_conflict", "claim_near_duplicate"}):
        recommendations.append(
            "Review structured claims for missing fields, conflicts, or duplicate evidence records."
        )
    if categories.intersection(
        {
            "question_unresolved",
            "question_answer_missing",
            "question_blocked_reason",
            "question_answer_ungrounded",
            "question_claim_missing",
            "question_claim_stale",
            "question_coverage_missing",
            "question_coverage_blocked",
            "question_coverage_invalid",
        }
    ):
        recommendations.append(
            "Resolve question task records: link answered questions to answer pages, "
            "ground answers in cited source_ids, explain blocked questions, and keep "
            "in_progress claims recorded and fresh."
        )
    if categories.intersection({"question_coverage_missing", "question_coverage_blocked", "question_coverage_invalid"}):
        recommendations.append(
            "Repair coverage-required answered questions: create valid coverage manifests and resolve failed required facets."
        )
    if "provenance_missing_license" in categories:
        recommendations.append(
            "Record license metadata in provenance sidecars for automated deliveries."
        )
    if categories.intersection(
        {
            "curation_missing_terms_license",
            "curation_missing_source_note",
            "curation_missing_origin_url",
            "curation_missing_checksum",
            "curation_missing_candidate_id",
        }
    ):
        recommendations.append(
            "Complete curation metadata for automated web evidence before treating cited captures as publication-ready."
        )
    if "output_license_missing" in categories:
        recommendations.append(
            "Surface license status before citing fetched sources in reusable output pages."
        )
    if "academic_metadata_missing" in categories:
        recommendations.append(
            "Surface academic venue and publication status before citing provider-backed papers in reusable output pages."
        )
    if "codebase_untrusted_input" in categories:
        recommendations.append(
            "Acknowledge the raw/code untrusted-input boundary only after selecting a safe codebase-analysis adapter."
        )
    if categories.intersection(
        {"source_request_invalid", "request_fulfilled_missing_source", "question_blocked_no_request"}
    ):
        recommendations.append(
            "Repair the source-request artifact: link blocked questions to requests and "
            "keep fulfilled requests pointing at existing manifest sources."
        )
    if not recommendations:
        recommendations.append("Wiki health checks passed for the enabled lint rules.")
    results["recommendations"] = recommendations


def run_checks(project_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    workspace_health = evaluate_workspace_health(project_root)
    raw_config = config_mapping(config, "raw")
    sources_config = config_mapping(config, "sources")
    wiki_config = config_mapping(config, "wiki")
    lint_config = config_mapping(config, "lint")
    outputs_config = config_mapping(config, "outputs")

    wiki_root_value = wiki_config.get("root", "wiki")
    manifest_path_value = sources_config.get("manifest_path", "sources/manifest.jsonl")
    normalized_dir_value = sources_config.get("normalized_dir", "sources/normalized")
    source_requests_value = sources_config.get("source_requests_path", "sources/source-requests.jsonl")
    outputs_default_dir_value = outputs_config.get("default_dir", "wiki/outputs")
    required_dirs = config_list(wiki_config.get("required_dirs"), "wiki.required_dirs")
    allowed_page_types = set(config_list(wiki_config.get("allowed_page_types"), "wiki.allowed_page_types"))
    required_fields = config_list(wiki_config.get("frontmatter_required"), "wiki.frontmatter_required")
    type_rules = normalize_frontmatter_type_rules(wiki_config)
    raw_roots = config_list(raw_config.get("source_roots"), "raw.source_roots")
    lifecycle_statuses = set(config_list(sources_config.get("lifecycle_statuses"), "sources.lifecycle_statuses"))

    results: dict[str, Any] = {
        "pages_checked": 0,
        "stats": {
            "manifest_records": 0,
            "manifest_invalid_records": 0,
            "prompt_injection_records_scanned": 0,
            "prompt_injection_question_pages_scanned": 0,
            "prompt_injection_provenance_notes_scanned": 0,
            "prompt_injection_findings": 0,
            "output_license_records_checked": 0,
            "output_license_missing": 0,
            "academic_metadata_records_checked": 0,
            "academic_metadata_missing": 0,
            "openalex_identity_conflict": 0,
            **{stat: 0 for stat in CURATION_STATS},
        },
        "issues": [],
        "recommendations": [],
        "workspace_health": workspace_health,
        "config": {
            "required_dirs": required_dirs,
            "allowed_page_types": sorted(allowed_page_types),
            "frontmatter_required": required_fields,
            "frontmatter_type_rules": sorted(type_rules),
            "raw_source_roots": raw_roots,
            "manifest_path": str(manifest_path_value),
            "normalized_dir": str(normalized_dir_value),
            "outputs_default_dir": str(outputs_default_dir_value),
            "enabled_checks": {
                "structure": bool(lint_config.get("validate_structure", True)),
                "frontmatter": bool(lint_config.get("validate_frontmatter", True)),
                "links": bool(lint_config.get("validate_links", True)),
                "dataview_aware": bool(lint_config.get("dataview_aware", False)),
                "source_manifest": bool(lint_config.get("validate_source_coverage", True)),
                "claims": bool(lint_config.get("validate_claims", True)),
                "questions": bool(lint_config.get("validate_questions", True)),
                "provenance": bool(lint_config.get("validate_provenance", True)),
                "curation_metadata": bool(lint_config.get("validate_curation_metadata", True)),
                "source_requests": bool(lint_config.get("validate_source_requests", True)),
                "output_license_status": bool(lint_config.get("validate_output_license_status", True)),
                "academic_publication_metadata": bool(
                    lint_config.get("validate_academic_publication_metadata", True)
                ),
                "prompt_injection_patterns": bool(lint_config.get("detect_prompt_injection_patterns", True)),
            },
        },
    }

    wiki_root_text, wiki_root = resolve_config_path(project_root, wiki_root_value, "wiki.root", results)
    manifest_path_text, manifest_path = resolve_config_path(
        project_root,
        manifest_path_value,
        "sources.manifest_path",
        results,
    )
    normalized_dir_text, normalized_root = resolve_config_path(
        project_root,
        normalized_dir_value,
        "sources.normalized_dir",
        results,
        under_sources=True,
    )
    source_requests_text, source_requests_path = resolve_config_path(
        project_root,
        source_requests_value,
        "sources.source_requests_path",
        results,
        under_sources=True,
    )
    outputs_default_dir_text, output_root = resolve_config_path(
        project_root,
        outputs_default_dir_value,
        "outputs.default_dir",
        results,
    )
    validated_raw_roots: list[str] = []
    for raw_root in raw_roots:
        raw_root_text, _ = resolve_config_path(project_root, raw_root, "raw.source_roots", results)
        if raw_root_text is not None:
            validated_raw_roots.append(raw_root_text)

    if wiki_root_text is not None:
        results["config"]["wiki_root"] = wiki_root_text
    if manifest_path_text is not None:
        results["config"]["manifest_path"] = manifest_path_text
    if normalized_dir_text is not None:
        results["config"]["normalized_dir"] = normalized_dir_text
    if source_requests_text is not None:
        results["config"]["source_requests_path"] = source_requests_text
    if outputs_default_dir_text is not None:
        results["config"]["outputs_default_dir"] = outputs_default_dir_text
    results["config"]["raw_source_roots"] = validated_raw_roots

    check_codebase_analysis_untrusted_input_acknowledgement(config, results)
    check_codebase_execution_scope(config, results)

    wiki_root = wiki_root or project_root / "__invalid_wiki_root__"
    manifest_path = manifest_path or project_root / "__invalid_manifest_path__"
    normalized_root = normalized_root or project_root / "__invalid_normalized_dir__"
    source_requests_path = source_requests_path or project_root / "__invalid_source_requests_path__"
    output_root = output_root or project_root / "__invalid_outputs_default_dir__"

    wiki_files = markdown_files(wiki_root)
    count_pages(project_root, wiki_root, wiki_files, results)

    if lint_config.get("validate_structure", True):
        check_structure(project_root, wiki_root, required_dirs, validated_raw_roots, results)
    if lint_config.get("validate_frontmatter", True):
        check_frontmatter(project_root, wiki_files, required_fields, allowed_page_types, type_rules, results)
    if lint_config.get("validate_links", True):
        link_files = files_for_link_checks(project_root, wiki_files)
        check_links(project_root, link_files, results)
        check_wikilinks(project_root, wiki_root, link_files, results)
        check_index_coverage(
            project_root,
            wiki_root,
            wiki_files,
            bool(lint_config.get("dataview_aware", False)),
            results,
        )

    validate_source_coverage = bool(lint_config.get("validate_source_coverage", True))
    validate_provenance = bool(lint_config.get("validate_provenance", True))
    validate_curation_metadata = bool(lint_config.get("validate_curation_metadata", True))
    validate_source_requests = bool(lint_config.get("validate_source_requests", True))
    validate_output_license_status = bool(lint_config.get("validate_output_license_status", True))
    validate_academic_publication_metadata = bool(
        lint_config.get("validate_academic_publication_metadata", True)
    )
    detect_prompt_injection_patterns = bool(lint_config.get("detect_prompt_injection_patterns", True))
    manifest_records: list[dict[str, Any]] = []
    if (
        validate_source_coverage
        or validate_provenance
        or validate_curation_metadata
        or validate_source_requests
        or validate_output_license_status
        or validate_academic_publication_metadata
        or detect_prompt_injection_patterns
    ):
        manifest_records = read_manifest(manifest_path, project_root, lifecycle_statuses, results)
    if validate_source_coverage:
        check_source_coverage(
            project_root,
            manifest_path,
            manifest_records,
            normalized_root,
            wiki_root,
            wiki_files,
            results,
        )
    if validate_provenance:
        check_provenance(project_root, manifest_path, manifest_records, results)
        check_openalex_identity_conflicts(project_root, manifest_path, manifest_records, results)
    if validate_curation_metadata:
        check_curation_metadata(project_root, manifest_path, manifest_records, wiki_root, wiki_files, output_root, results)
    if validate_output_license_status:
        check_output_license_status(project_root, manifest_path, manifest_records, output_root, results)
    if validate_academic_publication_metadata:
        check_output_academic_publication_metadata(project_root, manifest_path, manifest_records, output_root, results)
    if validate_source_requests:
        check_source_requests(project_root, source_requests_path, manifest_records, wiki_files, results)
    if detect_prompt_injection_patterns:
        check_prompt_injection_patterns(project_root, normalized_root, wiki_root, manifest_path, manifest_records, results)
    if lint_config.get("validate_claims", True):
        check_claims(project_root, wiki_root, wiki_files, results)
    if lint_config.get("validate_questions", True):
        check_questions(project_root, wiki_files, claim_staleness_window_hours(config), results, config)
    levels = severity_order(config)
    results["stats"]["issue_counts"] = issue_counts(results, levels)
    generate_recommendations(results)
    return results


def format_stats(stats: dict[str, Any]) -> str:
    counts = stats.get("wiki_counts")
    if not isinstance(counts, dict) or not counts:
        return "none"
    return ", ".join(f"{key}: {value}" for key, value in counts.items())


def format_text_report(results: dict[str, Any], levels: list[str]) -> str:
    lines = [
        "Research Wiki Lint Report",
        "=========================",
        f"Pages checked: {results['pages_checked']}",
        f"Wiki stats: {format_stats(results['stats'])}",
        f"Manifest records: {results['stats'].get('manifest_records', 0)}",
        "",
    ]
    issues = results["issues"]
    if issues:
        lines.append("Issues found:")
        for severity in levels:
            severity_issues = [item for item in issues if item["severity"] == severity]
            if not severity_issues:
                continue
            lines.append("")
            lines.append(f"### {severity}")
            for item in severity_issues:
                files = ", ".join(item["files"][:2]) if item["files"] else ""
                suffix = f" [{files}]" if files else ""
                code = f"{item['code']}: " if item.get("code") else ""
                lines.append(f"- {code}{item['message']}{suffix}")
    else:
        lines.append("No issues found.")

    lines.append("")
    lines.append("Recommendations:")
    for recommendation in results["recommendations"]:
        lines.append(f"- {recommendation}")
    return "\n".join(lines)


def invalid_workspace_results(project_root: Path, workspace_health: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {
        "pages_checked": 0,
        "stats": {},
        "issues": [],
        "recommendations": [],
        "config": {},
        "workspace_health": workspace_health,
    }
    for item in workspace_health["findings"]:
        issue(
            results,
            item["severity"],
            "workspace_health",
            item["message"],
            item["artifacts"],
            item["remediation"],
            code=item["code"],
        )
    levels = ["HIGH", "MEDIUM", "LOW"]
    results["stats"]["issue_counts"] = issue_counts(results, levels)
    results["recommendations"] = sorted(
        {item["remediation"] for item in workspace_health["findings"]}
    )
    return results


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def single_line_log_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def format_issue_summary(stats: dict[str, Any], levels: list[str]) -> str:
    counts = stats.get("issue_counts")
    if not isinstance(counts, dict):
        counts = {}
    return " ".join(f"{level.lower()}={counts.get(level, 0)}" for level in levels)


def format_source_coverage_summary(stats: dict[str, Any]) -> str:
    if "sources_discovered" not in stats:
        return "disabled"
    return " ".join(
        [
            f"discovered={stats.get('sources_discovered', 0)}",
            f"normalized={stats.get('sources_normalized', 0)}",
            f"noted={stats.get('sources_noted', 0)}",
            f"integrated={stats.get('sources_integrated', 0)}",
            f"missing_normalized={stats.get('sources_missing_normalized', 0)}",
            f"missing_source_note={stats.get('normalized_missing_source_note', 0)}",
            f"missing_integration={stats.get('integrated_missing_citation', 0)}",
        ]
    )


def format_log_recommendations(results: dict[str, Any]) -> list[str]:
    recommendations = results.get("recommendations")
    if not isinstance(recommendations, list):
        return []
    return [single_line_log_value(item) for item in recommendations if isinstance(item, str) and item.strip()]


def render_log_entry(results: dict[str, Any], timestamp: str, levels: list[str]) -> str:
    date_text = timestamp.split("T", 1)[0]
    stats = results["stats"]
    recommendations = format_log_recommendations(results)
    lines = [
        f"## [{date_text}] lint | Wiki health check\n\n"
        f"- pages_checked: {results['pages_checked']}\n"
        f"- manifest_records: {stats.get('manifest_records', 0)}\n"
        f"- issues: {format_issue_summary(stats, levels)}\n"
        f"- source_coverage: {format_source_coverage_summary(stats)}\n"
        f"- recommendations: {len(recommendations)}\n"
    ]
    lines.extend(f"- recommendation: {recommendation}\n" for recommendation in recommendations)
    return "".join(lines)


LOG_HEADER = "# Research Wiki Activity Log\n\n"


def append_log_entry(log_path: Path, entry: str) -> None:
    """Append a rendered log entry atomically under the workspace log lock.

    Concurrent inventory/normalize/lint runs can append to log.md at the same
    time; the shared lock plus append-only writes keep entries from
    interleaving or clobbering each other.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with workspace_lock(log_path.parent / ".locks" / "log.lock", purpose="activity log append"):
        handle = log_path.open("a+", encoding="utf-8")
        try:
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
        finally:
            handle.close()


def append_log(project_root: Path, results: dict[str, Any], levels: list[str]) -> None:
    append_log_entry(project_root / "log.md", render_log_entry(results, timestamp_utc(), levels))


def main() -> int:
    args = parse_args()
    json_mode = json_mode_requested(None, default_json=args.format == "json")
    project_root = Path(args.project_root).resolve()
    workspace_health = evaluate_workspace_health(project_root)
    try:
        if not (project_root / "research.yml").is_file() or "RESEARCH_CONFIG_INVALID" in workspace_health["finding_codes"]:
            results = invalid_workspace_results(project_root, workspace_health)
            levels = ["HIGH", "MEDIUM", "LOW"]
        else:
            config = load_config(project_root)
            results = run_checks(project_root, config)
            levels = severity_order(config)
    except LockUnavailableError as exc:
        emit_error(str(exc), json_mode=json_mode, error_code=exc.error_code, details=exc.details)
        return 2
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=2)

    if args.format == "json":
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(format_text_report(results, levels))

    if args.append_log:
        try:
            append_log(project_root, results, levels)
        except LockUnavailableError as exc:
            emit_error(str(exc), json_mode=json_mode, error_code=exc.error_code, details=exc.details)
            return 2

    return 0 if workspace_health["materially_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
