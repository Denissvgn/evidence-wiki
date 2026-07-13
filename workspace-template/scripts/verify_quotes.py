#!/usr/bin/env python3
"""Verify answer grounding quotes against normalized source records.

The verifier is offline and deterministic. It reads question-page
``grounding`` frontmatter entries, resolves each ``source_id`` to its normalized
record, and checks whether the quoted text maps to one retained occurrence at a
declared title, page, or section anchor. Normalization is limited to deterministic
Unicode, whitespace, punctuation, and line-break hyphenation artifacts; semantic
substitution is never accepted. It performs no network I/O.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to verify grounding quotes") from exc


SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_NOT_VERIFIED = 1
EXIT_INVALID = 2
RESULT_VERIFIED = "verified"
RESULT_QUOTE_NOT_FOUND = "quote_not_found"
RESULT_SOURCE_NOT_NORMALIZED = "source_not_normalized"
RESULT_QUOTE_AMBIGUOUS = "quote_ambiguous"
RESULT_ANCHOR_NOT_FOUND = "anchor_not_found"
RESULT_QUOTE_NOT_AT_ANCHOR = "quote_not_at_anchor"

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_module_loader import load_workspace_module

_SIBLING_CACHE: dict[str, ModuleType] = {}


class VerifyQuotesError(Exception):
    """Fatal grounding verifier error with a stable machine code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify grounded answer quotes against normalized source records.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument("--slug", action="append", required=True, help="Question slug to verify. Repeatable.")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Report format. Defaults to json.")
    parser.add_argument("--output", default=None, help="Write the report to this path instead of stdout.")
    parser.add_argument("--write", action="store_true", help="Record verifier metadata on fully verified questions.")
    parser.add_argument("--verified-by", default=None, help="Verifier agent id required with --write.")
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        raise SystemExit(f"Missing config: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise SystemExit(f"Invalid config: {path}")
    return document


def split_page(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return {}, text
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise VerifyQuotesError("PAGE_INVALID", f"Invalid frontmatter YAML: {exc}") from exc
    body = "\n".join(lines[closing + 1 :])
    return (frontmatter if isinstance(frontmatter, dict) else {}), body


def validate_slug(slug: str) -> str:
    clean_slug = slug.strip()
    if not clean_slug or "/" in clean_slug or "\\" in clean_slug or clean_slug.startswith("."):
        raise VerifyQuotesError("SLUG_INVALID", f"invalid question slug: {slug}", details={"slug": slug})
    return clean_slug


def question_path(project_root: Path, config: dict[str, Any], slug: str) -> Path:
    question_status = load_sibling_module("question_status")
    clean_slug = validate_slug(slug)
    return question_status.questions_directory(project_root, config) / f"{clean_slug}.md"


def workspace_relative(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def normalized_record_path(project_root: Path, config: dict[str, Any], source_id: str) -> tuple[Path, str]:
    normalize = load_sibling_module("normalize_sources")
    _, normalized_rel = normalize.source_paths(config)
    path = project_root / normalized_rel / f"{normalize.safe_source_id(source_id)}.md"
    return path, f"{normalized_rel}/{path.name}"


_QUOTE_NORMALIZATION_TRANSLATION = str.maketrans(
    {
        "‘": "'",  # left single quotation mark
        "’": "'",  # right single quotation mark / apostrophe
        "‚": "'",  # single low-9 quotation mark
        "‛": "'",  # single high-reversed-9 quotation mark
        "“": '"',  # left double quotation mark
        "”": '"',  # right double quotation mark
        "„": '"',  # double low-9 quotation mark
        "‟": '"',  # double high-reversed-9 quotation mark
        "­": "",  # soft hyphen
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
    }
)
_LINE_BREAK_HYPHEN_RE = re.compile(r"-[ \t]*\n[ \t]*")
_PAGE_ANCHOR_RE = re.compile(
    r"(?im)^[ \t]*(?:<!--[ \t]*)?(?:page|p\.)[ \t]*[:#-]?[ \t]*(\d+)(?:[ \t]*-->)?[ \t]*$"
)
_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")


def normalize_quote_text(value: str, *, dehyphenate_line_breaks: bool = False) -> str:
    """Normalize whitespace, case, and common PDF/Unicode extraction artifacts.

    NFKC folds compatibility variants (ligatures, full-width forms); curly
    quotes/apostrophes, dashes, and the soft hyphen are mapped to plain ASCII.
    Callers evaluate both retained-hyphen and dehyphenated line-break variants so
    legitimate compound words and extraction-wrapped words remain distinguishable.
    """
    text = unicodedata.normalize("NFKC", value)
    text = text.translate(_QUOTE_NORMALIZATION_TRANSLATION)
    text = _LINE_BREAK_HYPHEN_RE.sub("" if dehyphenate_line_breaks else "-", text)
    return " ".join(text.split()).casefold()


def quote_match(text: str, quote: str) -> dict[str, Any]:
    exact_count = text.count(quote)
    if exact_count:
        return {"match_type": "exact", "occurrence_count": exact_count}
    for match_type, dehyphenate in (
        ("normalized", False),
        ("normalized_dehyphenated", True),
    ):
        normalized_text = normalize_quote_text(text, dehyphenate_line_breaks=dehyphenate)
        normalized_quote = normalize_quote_text(quote, dehyphenate_line_breaks=dehyphenate)
        count = normalized_text.count(normalized_quote) if normalized_quote else 0
        if count:
            return {"match_type": match_type, "occurrence_count": count}
    return {"match_type": None, "occurrence_count": 0}


def page_anchor(body: str, location_hint: str) -> tuple[str | None, dict[str, Any]] | None:
    requested = re.fullmatch(r"(?:page|p\.)[ \t]*[:#-]?[ \t]*(\d+)", location_hint.strip(), re.IGNORECASE)
    if requested is None:
        return None
    page_number = requested.group(1)
    markers = list(_PAGE_ANCHOR_RE.finditer(body))
    matching = [(index, marker) for index, marker in enumerate(markers) if marker.group(1) == page_number]
    if len(matching) != 1:
        status = "not_found" if not matching else "ambiguous"
        return None, {"type": "page", "label": f"page {page_number}", "status": status}
    index, marker = matching[0]
    end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
    return body[marker.end() : end], {"type": "page", "label": f"page {page_number}", "status": "matched"}


def section_anchor(body: str, location_hint: str) -> tuple[str | None, dict[str, Any]]:
    hint = normalize_quote_text(location_hint)
    headings = list(_HEADING_RE.finditer(body))
    matching = [
        (index, heading)
        for index, heading in enumerate(headings)
        if hint
        and (
            normalize_quote_text(heading.group(2)) == hint
            or hint in normalize_quote_text(heading.group(2))
            or normalize_quote_text(heading.group(2)) in hint
        )
    ]
    if len(matching) != 1:
        status = "not_found" if not matching else "ambiguous"
        return None, {"type": "section", "label": location_hint, "status": status}
    index, heading = matching[0]
    level = len(heading.group(1))
    end = len(body)
    for next_heading in headings[index + 1 :]:
        if len(next_heading.group(1)) <= level:
            end = next_heading.start()
            break
    return body[heading.start() : end], {
        "type": "section",
        "label": heading.group(2).strip(),
        "status": "matched",
    }


def resolve_anchor(
    frontmatter: dict[str, Any],
    body: str,
    location_hint: str | None,
) -> tuple[str | None, dict[str, Any]]:
    if not location_hint:
        return body, {"type": None, "label": None, "status": "not_requested"}
    normalized_hint = normalize_quote_text(location_hint)
    if normalized_hint in {"title", "normalized title", "document title"}:
        title = frontmatter.get("title")
        if isinstance(title, str) and title.strip():
            return title, {"type": "title", "label": "normalized title", "status": "matched"}
        return None, {"type": "title", "label": "normalized title", "status": "not_found"}
    page = page_anchor(body, location_hint)
    if page is not None:
        return page
    return section_anchor(body, location_hint)


def grounding_entries(frontmatter: dict[str, Any], slug: str) -> list[dict[str, str]]:
    raw = frontmatter.get("grounding")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise VerifyQuotesError(
            "GROUNDING_INVALID",
            f"Question {slug} has invalid grounding: expected a list of claim/source/quote entries.",
            details={"slug": slug, "field": "grounding", "actual": type(raw).__name__},
        )
    entries: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise VerifyQuotesError(
                "GROUNDING_INVALID",
                f"Question {slug} grounding[{index}] must be a mapping.",
                details={"slug": slug, "index": index, "actual": type(item).__name__},
            )
        entry: dict[str, str] = {}
        for field in ("claim", "source_id", "quote"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise VerifyQuotesError(
                    "GROUNDING_INVALID",
                    f"Question {slug} grounding[{index}] is missing non-empty {field}.",
                    details={"slug": slug, "index": index, "field": field},
                )
            entry[field] = value.strip()
        location_hint = item.get("location_hint")
        if location_hint is not None:
            if not isinstance(location_hint, str):
                raise VerifyQuotesError(
                    "GROUNDING_INVALID",
                    f"Question {slug} grounding[{index}] location_hint must be a string when present.",
                    details={"slug": slug, "index": index, "field": "location_hint"},
                )
            if location_hint.strip():
                entry["location_hint"] = location_hint.strip()
        entries.append(entry)
    return entries


def normalized_record_content(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_page(text)
    return frontmatter, body if frontmatter else text


def verify_entry(project_root: Path, config: dict[str, Any], entry: dict[str, str]) -> dict[str, Any]:
    source_id = entry["source_id"]
    record_path, record_label = normalized_record_path(project_root, config, source_id)
    result: dict[str, Any] = {
        "claim": entry["claim"],
        "source_id": source_id,
        "quote": entry["quote"],
        "location_hint": entry.get("location_hint"),
        "normalized_record": record_label,
        "artifacts": [record_label],
        "policy": "retained_quote_evidence",
    }
    if not record_path.is_file():
        result["result"] = RESULT_SOURCE_NOT_NORMALIZED
        result["message"] = f"{source_id} has no normalized record at {record_label}."
        result["remediation"] = "Normalize the cited source, then rerun quote verification."
        return result
    frontmatter, body = normalized_record_content(record_path)
    anchor_text, anchor = resolve_anchor(frontmatter, body, entry.get("location_hint"))
    result["anchor"] = anchor
    global_match = quote_match(body, entry["quote"])
    result["global_occurrence_count"] = global_match["occurrence_count"]
    if anchor_text is None:
        result["result"] = RESULT_ANCHOR_NOT_FOUND
        result["match_type"] = global_match["match_type"]
        result["occurrence_count"] = 0
        result["message"] = "The requested page/section anchor was not uniquely resolved in the normalized record."
        result["remediation"] = "Correct the location_hint to a retained page marker, section heading, or normalized title."
        return result
    scoped_match = quote_match(anchor_text, entry["quote"])
    result.update(scoped_match)
    if scoped_match["occurrence_count"] == 1:
        result["result"] = RESULT_VERIFIED
        result["message"] = "Quote maps to one retained occurrence at the requested anchor."
        result["remediation"] = "No remediation required."
    elif scoped_match["occurrence_count"] > 1:
        result["result"] = RESULT_QUOTE_AMBIGUOUS
        result["message"] = "Quote occurs more than once within the selected evidence scope."
        result["remediation"] = "Add a more specific page/section anchor or lengthen the quote without changing its meaning."
    elif global_match["occurrence_count"]:
        result["result"] = RESULT_QUOTE_NOT_AT_ANCHOR
        result["message"] = "Quote exists in the normalized record but not at the requested anchor."
        result["remediation"] = "Correct the location_hint or quote so both identify the same retained evidence span."
    else:
        result["result"] = RESULT_QUOTE_NOT_FOUND
        result["message"] = "Quote was not found in the normalized record after whitespace/case normalization."
        result["remediation"] = "Use a verbatim retained quote or correct the cited source; do not paraphrase inside quote fields."
    return result


def verify_question(
    project_root: Path,
    config: dict[str, Any],
    slug: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    question = path or question_path(project_root, config, slug)
    if not question.is_file():
        raise VerifyQuotesError(
            "QUESTION_UNKNOWN",
            f"Unknown question slug: {slug}",
            details={"slug": slug},
        )
    if frontmatter is None:
        frontmatter, _ = split_page(question.read_text(encoding="utf-8"))
    entries = grounding_entries(frontmatter, slug)
    results = [verify_entry(project_root, config, entry) for entry in entries]
    all_verified = bool(results) and all(result.get("result") == RESULT_VERIFIED for result in results)
    return {
        "slug": slug,
        "question_page": workspace_relative(project_root, question),
        "grounding_count": len(results),
        "all_verified": all_verified,
        "grounding": results,
    }


def build_report(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(project_root)
    seen: set[str] = set()
    slugs: list[str] = []
    for value in args.slug or []:
        slug = value.strip()
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    if not slugs:
        raise VerifyQuotesError("SLUG_INVALID", "At least one --slug value is required.")
    questions = [verify_question(project_root, config, slug) for slug in slugs]
    total_entries = sum(int(question.get("grounding_count", 0) or 0) for question in questions)
    failed_entries = [
        result
        for question in questions
        for result in question.get("grounding", [])
        if isinstance(result, dict) and result.get("result") != RESULT_VERIFIED
    ]
    missing_grounding = [question["slug"] for question in questions if not question.get("grounding")]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_utc(),
        "network_io_executed": False,
        "questions": questions,
        "counts": {
            "questions": len(questions),
            "grounding_entries": total_entries,
            "verified": total_entries - len(failed_entries),
            "failed": len(failed_entries),
            "missing_grounding": len(missing_grounding),
        },
        "overall_result": RESULT_VERIFIED if questions and not failed_entries and not missing_grounding else "not_verified",
    }


def is_top_level_field(line: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_-]+:\s*", line))


def remove_frontmatter_field_block(lines: list[str], key: str) -> list[str]:
    prefix = f"{key}:"
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(prefix):
            index += 1
            while index < len(lines) and not is_top_level_field(lines[index]):
                index += 1
            continue
        output.append(line)
        index += 1
    return output


def set_frontmatter_scalar(lines: list[str], key: str, value: str) -> list[str]:
    lines = remove_frontmatter_field_block(lines, key)
    return [*lines, f"{key}: {json.dumps(value)}"]


def stamp_question_verification(project_root: Path, config: dict[str, Any], slug: str, verified_by: str) -> dict[str, Any]:
    question_claim = load_sibling_module("question_claim")
    question = question_path(project_root, config, slug)
    with question_claim.question_lock(question):
        text = question.read_text(encoding="utf-8")
        parts = question_claim.split_frontmatter_lines(text)
        if parts is None:
            raise VerifyQuotesError("PAGE_INVALID", f"Question {slug} has no frontmatter block.", details={"slug": slug})
        frontmatter = question_claim.frontmatter_mapping(parts[0])
        report = verify_question(project_root, config, slug, frontmatter=frontmatter, path=question)
        if not report["all_verified"]:
            raise VerifyQuotesError(
                "GROUNDING_QUOTE_INVALID",
                f"Question {slug} has grounding quotes that did not verify; refusing to stamp verifier metadata.",
                details={"slug": slug},
            )
        frontmatter_lines, opening, rest = parts
        frontmatter_lines = set_frontmatter_scalar(frontmatter_lines, "verified_by", verified_by)
        frontmatter_lines = set_frontmatter_scalar(frontmatter_lines, "grounding_verified_at", timestamp_utc())
        question_claim.write_page_atomic(question, "\n".join([*opening, *frontmatter_lines, *rest]))
        return report


def write_verification_metadata(project_root: Path, args: argparse.Namespace, report: dict[str, Any]) -> None:
    verified_by = args.verified_by.strip() if isinstance(args.verified_by, str) else ""
    if not verified_by:
        raise VerifyQuotesError(
            "GROUNDING_VERIFIER_REQUIRED",
            "--verified-by is required when --write is set.",
            details={"field": "verified_by"},
        )
    if report.get("overall_result") != RESULT_VERIFIED:
        raise VerifyQuotesError(
            "GROUNDING_QUOTE_INVALID",
            "All grounding quotes must verify before verifier metadata is written.",
        )
    config = load_config(project_root)
    for question in report.get("questions", []):
        if isinstance(question, dict) and isinstance(question.get("slug"), str):
            stamp_question_verification(project_root, config, question["slug"], verified_by)


def render_text(report: dict[str, Any]) -> str:
    lines = ["Grounding Quote Verification", "============================", ""]
    for question in report.get("questions", []):
        lines.append(f"- {question.get('slug')}: {'verified' if question.get('all_verified') else 'not_verified'}")
        for result in question.get("grounding", []):
            lines.append(f"  - {result.get('source_id')}: {result.get('result')} - {result.get('claim')}")
    return "\n".join(lines).rstrip() + "\n"


def render_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    return render_text(report)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        report = build_report(project_root, args)
        if args.write:
            write_verification_metadata(project_root, args, report)
    except VerifyQuotesError as exc:
        emit_error(str(exc), json_mode=json_mode, error_code=exc.error_code, details=exc.details)
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)
    rendered = render_report(report, args.format)
    if args.output:
        Path(args.output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return EXIT_OK if report.get("overall_result") == RESULT_VERIFIED else EXIT_NOT_VERIFIED


if __name__ == "__main__":
    raise SystemExit(main())
