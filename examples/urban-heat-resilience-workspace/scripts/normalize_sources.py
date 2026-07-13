#!/usr/bin/env python3
"""Normalize source manifest records into agent-readable Markdown."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc


NORMALIZER_NAME = "normalize_sources.py"
NORMALIZER_VERSION = 1
OVERRIDABLE_EVIDENCE_USABILITY_REASONS = {"html_javascript_shell"}
MAX_INCLUDE_DEPTH = 24
MAX_UNRESOLVED_MACROS = 30
PDF_MIN_USEFUL_CHARS = 200
# Scanned/image-only PDF heuristic: when pdftotext ran successfully but yielded
# fewer extracted characters per page than this, the PDF likely needs OCR.
PDF_MIN_CHARS_PER_PAGE = 100
PDF_LAYOUT_TEXT_CACHE: dict[str, str] = {}
# HTML extraction reads at most this many bytes; larger pages are truncated
# with a parse warning. No JS rendering, no remote asset fetching.
HTML_MAX_BYTES = 2_000_000
HTML_EXTENSIONS = {".html", ".htm", ".xhtml"}
HTML_SKIP_TAGS = {"script", "style", "nav", "noscript", "template", "svg", "iframe"}
HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
}
HTML_MAX_OUTLINE_ENTRIES = 80
HTML_MAX_LINKS = 100
# Tabular extraction caps: bytes read from a CSV/TSV file and sample rows
# rendered into the normalized record.
TABLE_MAX_BYTES = 5_000_000
TABLE_SAMPLE_ROWS = 20
TABLE_MAX_CELL_CHARS = 80
TABLE_TEXT_EXTENSIONS = {".csv", ".tsv"}

SECTION_COMMANDS = {
    "part": 1,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
INLINE_COMMANDS_WITH_TEXT = {
    "ac",
    "acl",
    "acs",
    "autoref",
    "Cref",
    "cref",
    "emph",
    "href",
    "mbox",
    "nameref",
    "ref",
    "S",
    "sectionref",
    "textbf",
    "textcolor",
    "textit",
    "texorpdfstring",
    "textsc",
    "textsuperscript",
    "texttt",
    "url",
}
DROP_COMMANDS_WITH_ARGS = {
    "addbibresource",
    "affil",
    "author",
    "bibliography",
    "bibliographystyle",
    "caption",
    "cite",
    "citep",
    "citet",
    "documentclass",
    "footnote",
    "graphicspath",
    "icmlaffiliation",
    "icmlauthor",
    "icmlcorrespondingauthor",
    "icmlkeywords",
    "icmltitle",
    "includegraphics",
    "label",
    "newcommand",
    "renewcommand",
    "thanks",
    "title",
    "usepackage",
}
DROP_LINE_COMMANDS = {
    "date",
    "maketitle",
    "newpage",
    "pagebreak",
    "pagestyle",
    "pdfoutput",
    "setcounter",
    "thispagestyle",
    "vspace",
}
LATEX_SYMBOLS = {
    r"\&": "&",
    r"\%": "%",
    r"\$": "$",
    r"\#": "#",
    r"\_": "_",
    r"\{": "{",
    r"\}": "}",
    r"\quad": " ",
    r"\qquad": " ",
    r"\textbackslash": "\\",
    r"~": " ",
}
ENVIRONMENTS_TO_DROP = {
    "algorithm",
    "algorithmic",
    "algorithm2e",
    "align",
    "align*",
    "center",
    "equation",
    "equation*",
    "figure",
    "figure*",
    "lstlisting",
    "picture",
    "table",
    "table*",
    "tabular",
    "tabular*",
    "tabularx",
    "tikzpicture",
}
URL_RE = re.compile(r"https?://[^\s<>{}\"']+")
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$", re.IGNORECASE)
LINK_KINDS = {"repo_link", "web_link"}
CODEBASE_KIND = "codebase_architecture"
CODEBASE_ANALYSIS_DEFAULT_OUTPUT_DIR = "sources/code_wikis"
CODEBASE_JSON_ARTIFACTS = ("context.json", "extract.json", "summary.json")
CODEBASE_TEXT_ARTIFACTS = ("context.md", "extract-summary.md", "summary.md", "README.md", "context.txt")
CODEBASE_ARTIFACT_MANIFEST = "artifact-manifest.json"
CODEBASE_ARTIFACT_MANIFEST_SCHEMA_VERSION = "1"
CODEBASE_MAX_ARTIFACT_FILES = 128
CODEBASE_MAX_ARTIFACT_ENTRIES = 512
CODEBASE_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
CODEBASE_SUPPORTED_ARTIFACT_SUFFIXES = {".json", ".md", ".txt"}
NORMALIZATION_REPORT_SCHEMA_VERSION = "1.0"
NORMALIZATION_REPORT_DOCUMENT_TYPE = "source_normalization_report"

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import emit_error, handle_system_exit
from _workspace_locks import LockUnavailableError, workspace_lock
from source_failure_taxonomy import unusable_evidence_reasons as delivery_unusable_evidence_reasons


@dataclass
class IncludeResult:
    text: str
    included_paths: list[str]
    warnings: list[str]


@dataclass
class MediaReference:
    kind: str
    caption: str | None = None
    label: str | None = None
    graphics: list[str] | None = None


@dataclass
class NormalizedSource:
    record: dict[str, Any]
    extraction_method: str
    title: str | None
    authors: list[str]
    abstract: str | None
    outline: list[tuple[int, str]]
    extracted_text: str
    media: list[MediaReference]
    links: list[str]
    bibliography_files: list[str]
    included_paths: list[str]
    warnings: list[str]
    title_confidence: str = "high"
    abstract_confidence: str = "high"
    needs_ocr: bool = False
    title_source: str | None = None
    extracted_title: str | None = None


@dataclass
class CodebaseArtifactIntake:
    status: str
    artifact_paths: list[str]
    preferred_artifact: Path | None
    manifest_path: str | None
    checksums: list[dict[str, Any]]
    provenance: dict[str, Any] | None
    warnings: list[str]


@dataclass
class PdfAbstractExtraction:
    text: str | None
    confidence: str
    recovered_by_fallback: bool = False


@dataclass
class EligibleRecord:
    record: dict[str, Any]
    method: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize manifest records into Markdown.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--source-id",
        action="append",
        default=[],
        metavar="ID",
        help="Normalize one manifest source ID. Repeat to normalize a selected subset.",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help=(
            "Consider every eligible manifest source. Missing and changed (stale) outputs are "
            "(re)generated; unchanged outputs are skipped unless --force is supplied."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned normalization actions without extracting content or writing files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite selected normalized records that already exist, even when unchanged.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for the normalization run summary. Defaults to text.",
    )
    parser.add_argument(
        "--append-log",
        action="store_true",
        help="Append a compact normalization run summary to log.md.",
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


def validate_generated_sources_path(value: Any, label: str) -> str:
    relative = validate_workspace_relative_path(value, label).rstrip("/")
    if relative != "sources" and not relative.startswith("sources/"):
        raise SystemExit(f"research.yml {label} must be under the generated sources/ directory: {value}")
    return relative


def source_paths(config: dict[str, Any]) -> tuple[str, str]:
    sources_config = config.get("sources") or {}
    if not isinstance(sources_config, dict):
        raise SystemExit("research.yml sources must be a mapping")
    manifest_path = validate_workspace_relative_path(
        sources_config.get("manifest_path", "sources/manifest.jsonl"),
        "sources.manifest_path",
    )
    normalized_dir = validate_generated_sources_path(
        sources_config.get("normalized_dir", "sources/normalized"),
        "sources.normalized_dir",
    )
    return manifest_path, normalized_dir


def codebase_analysis_config(config: dict[str, Any]) -> dict[str, Any]:
    integrations = config.get("integrations") or {}
    if not isinstance(integrations, dict):
        return {}
    codebase = integrations.get("codebase_analysis") or {}
    return codebase if isinstance(codebase, dict) else {}


def codebase_output_dir(config: dict[str, Any]) -> str:
    value = codebase_analysis_config(config).get("output_dir")
    if isinstance(value, str) and value.strip():
        return validate_generated_sources_path(value, "integrations.codebase_analysis.output_dir")
    return CODEBASE_ANALYSIS_DEFAULT_OUTPUT_DIR


def codebase_provider_from_config(config: dict[str, Any]) -> str | None:
    provider = codebase_analysis_config(config).get("provider")
    return provider.strip() if isinstance(provider, str) and provider.strip() and provider.strip() != "none" else None


def load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    if not manifest_path.exists():
        raise SystemExit(f"Missing manifest: {manifest_path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(manifest_path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL in {manifest_path}:{line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise SystemExit(f"Invalid manifest record in {manifest_path}:{line_number}: expected object")
        records.append(record)
    return records


def safe_source_id(source_id: str) -> str:
    value = source_id.lower().replace(":", "__colon__")
    value = re.sub(r"[/\s]+", "-", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    value = value.replace("__colon__", "--")
    value = value.replace("-.", ".").strip("-")
    return value or "source"


def unique_values(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_output_frontmatter(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    # Close on the first line that is exactly `---` so a horizontal rule or a
    # `---`-prefixed value inside the block does not truncate parsing early.
    closing_index = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing_index is None:
        return {}
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
    except yaml.YAMLError:
        return {}
    return frontmatter if isinstance(frontmatter, dict) else {}


def existing_created_date(path: Path) -> str | None:
    created = read_output_frontmatter(path).get("created")
    if isinstance(created, str):
        return created
    if hasattr(created, "isoformat"):
        return str(created.isoformat())
    return None


def stored_raw_fingerprint(path: Path) -> str | None:
    value = read_output_frontmatter(path).get("raw_fingerprint")
    return value if isinstance(value, str) and value else None


def record_raw_fingerprint(record: dict[str, Any]) -> str | None:
    value = record.get("raw_fingerprint")
    return value if isinstance(value, str) and value else None


def is_stale(record: dict[str, Any], output_path: Path) -> bool:
    """True when the raw inputs changed since the normalized record was written.

    Returns False when the manifest carries no raw fingerprint (links/codebase or
    a pre-fingerprint manifest), so those records keep skip-if-exists behavior.
    A stored fingerprint that is missing or different counts as stale, which also
    backfills fingerprints into records written before this signal existed.
    """
    manifest_fingerprint = record_raw_fingerprint(record)
    if manifest_fingerprint is None:
        return False
    return stored_raw_fingerprint(output_path) != manifest_fingerprint


def record_id(record: dict[str, Any]) -> str:
    source_id = record.get("id")
    return source_id if isinstance(source_id, str) and source_id else "source"


def is_latex_record(record: dict[str, Any]) -> bool:
    return (
        record.get("kind") == "paper"
        and isinstance(record.get("latex_root"), str)
        and isinstance(record.get("entrypoint"), str)
    )


def is_usable_latex_record(project_root: Path, record: dict[str, Any]) -> bool:
    if not is_latex_record(record):
        return False
    latex_root = project_root / str(record["latex_root"])
    entrypoint = latex_root / str(record["entrypoint"])
    return entrypoint.is_file()


def raw_pdf_value(record: dict[str, Any]) -> str | None:
    raw_pdf = record.get("raw_pdf")
    if isinstance(raw_pdf, str) and raw_pdf.lower().endswith(".pdf"):
        return raw_pdf
    raw_paths = record.get("raw_paths")
    if isinstance(raw_paths, list):
        for raw_path in raw_paths:
            if isinstance(raw_path, str) and raw_path.lower().endswith(".pdf"):
                return raw_path
    return None


def is_pdf_fallback_record(project_root: Path, record: dict[str, Any]) -> bool:
    if raw_pdf_value(record) is None:
        return False
    if record.get("kind") == "pdf":
        return True
    return not is_usable_latex_record(project_root, record)


def safe_workspace_path(project_root: Path, relative_path: str) -> Path | None:
    raw = relative_path.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if not raw or "://" in normalized or parsed.scheme:
        return None
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return None
    return project_root / path.as_posix()


def record_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def complete_evidence_usability_override(provenance: dict[str, Any]) -> dict[str, Any] | None:
    value = provenance.get("evidence_usability_override")
    if not isinstance(value, dict) or value.get("usable") is not True:
        return None
    for key in ("reviewed_by", "reviewed_at", "reason"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            return None
    return value


def record_unusable_evidence_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    explicit = record.get("unusable_evidence_reasons")
    if isinstance(explicit, list):
        reasons.extend(reason for reason in explicit if isinstance(reason, str) and reason.strip())
    provenance = record.get("provenance")
    if isinstance(provenance, dict):
        reasons.extend(delivery_unusable_evidence_reasons(provenance))
        override = complete_evidence_usability_override(provenance)
        if override is not None and reasons:
            remaining = [
                reason for reason in reasons if reason not in OVERRIDABLE_EVIDENCE_USABILITY_REASONS
            ]
            if len(remaining) != len(reasons) and not remaining:
                provenance["evidence_usability_override_applied"] = True
            reasons = remaining
    if record.get("evidence_usable") is False and not reasons:
        if not (isinstance(provenance, dict) and provenance.get("evidence_usability_override_applied") is True):
            reasons.append("evidence_usable:false")
    return unique_values(reasons)


def apply_record_usability_override(record: dict[str, Any]) -> None:
    reasons = record_unusable_evidence_reasons(record)
    if reasons:
        record["evidence_usable"] = False
        record["unusable_evidence_reasons"] = reasons
        return
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and provenance.get("evidence_usability_override_applied") is True:
        record["evidence_usable"] = True
        record.pop("unusable_evidence_reasons", None)


def set_record_unusable_evidence(record: dict[str, Any], reasons: list[str]) -> None:
    cleaned = unique_values([reason for reason in reasons if isinstance(reason, str) and reason.strip()])
    if not cleaned:
        return
    record["evidence_usable"] = False
    record["unusable_evidence_reasons"] = cleaned
    apply_record_usability_override(record)


def record_url(record: dict[str, Any]) -> str | None:
    url = record.get("url")
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def is_link_record(record: dict[str, Any]) -> bool:
    return record.get("kind") in LINK_KINDS and record_url(record) is not None


def is_codebase_record(record: dict[str, Any]) -> bool:
    return record.get("kind") == CODEBASE_KIND


def link_provider(record: dict[str, Any]) -> str | None:
    metadata = record_metadata(record)
    host = metadata.get("host")
    if not isinstance(host, str):
        url = record_url(record)
        host = urlparse(url).netloc.lower().removeprefix("www.") if url else ""
    if record.get("kind") == "repo_link" and host == "github.com":
        return "github"
    return host or None


def repo_full_name(record: dict[str, Any]) -> str | None:
    value = record_metadata(record).get("repo_full_name")
    return value if isinstance(value, str) and value else None


def codebase_repo(record: dict[str, Any]) -> str | None:
    repo = repo_full_name(record)
    if repo:
        return repo
    metadata = record_metadata(record)
    for key in ("codebase_repo", "repo_name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    url = record_url(record)
    return url


def codebase_provider(record: dict[str, Any]) -> str | None:
    metadata = record_metadata(record)
    provider = metadata.get("provider") or metadata.get("codebase_tool")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    source_type = metadata.get("codebase_source_type")
    if source_type == "local_repo":
        return "local"
    if source_type == "code_archive":
        return "archive"
    return link_provider(record)


def link_title(record: dict[str, Any]) -> str:
    url = record_url(record)
    repo = repo_full_name(record)
    if repo:
        return repo
    if url:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        return f"{parsed.netloc.lower().removeprefix('www.')}/{path}".rstrip("/")
    return str(record.get("id", "link"))


def codebase_title(record: dict[str, Any]) -> str:
    repo = codebase_repo(record)
    if repo:
        return repo
    raw_values = record.get("raw_paths")
    if isinstance(raw_values, list):
        for raw_path in raw_values:
            if isinstance(raw_path, str) and raw_path.strip():
                return PurePosixPath(raw_path).name or raw_path
    return str(record.get("id", "codebase"))


def html_raw_path(record: dict[str, Any]) -> str | None:
    raw_paths = record.get("raw_paths")
    if isinstance(raw_paths, list):
        for raw_path in raw_paths:
            if isinstance(raw_path, str) and PurePosixPath(raw_path).suffix.lower() in HTML_EXTENSIONS:
                return raw_path
    return None


def table_raw_path(record: dict[str, Any]) -> str | None:
    raw_paths = record.get("raw_paths")
    if isinstance(raw_paths, list):
        for raw_path in raw_paths:
            if isinstance(raw_path, str) and PurePosixPath(raw_path).suffix.lower() in TABLE_TEXT_EXTENSIONS:
                return raw_path
    return None


def normalization_method(project_root: Path, record: dict[str, Any]) -> str | None:
    if is_codebase_record(record):
        return "codebase"
    if is_latex_record(record):
        if is_usable_latex_record(project_root, record) or raw_pdf_value(record) is None:
            return "latex"
        if is_pdf_fallback_record(project_root, record):
            return "pdf"
        return None
    if is_pdf_fallback_record(project_root, record):
        return "pdf"
    if is_link_record(record):
        return "link"
    if record.get("kind") == "html" and html_raw_path(record) is not None:
        return "html"
    if record.get("kind") == "table" and table_raw_path(record) is not None:
        return "table"
    return None


def eligible_records(project_root: Path, records: list[dict[str, Any]]) -> list[EligibleRecord]:
    eligible: list[EligibleRecord] = []
    for record in records:
        method = normalization_method(project_root, record)
        if method:
            eligible.append(EligibleRecord(record=record, method=method))
    return eligible


def records_by_source_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        source_id = record.get("id")
        if isinstance(source_id, str) and source_id not in indexed:
            indexed[source_id] = record
    return indexed


def eligible_by_source_id(records: list[EligibleRecord]) -> dict[str, EligibleRecord]:
    indexed: dict[str, EligibleRecord] = {}
    for item in records:
        source_id = record_id(item.record)
        if source_id not in indexed:
            indexed[source_id] = item
    return indexed


def normalized_output_path_for_record(record: dict[str, Any], normalized_root: Path) -> Path:
    return normalized_root / f"{safe_source_id(record_id(record))}.md"


def select_eligible_records(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    eligible: list[EligibleRecord],
    normalized_root: Path,
) -> tuple[list[EligibleRecord], int, str]:
    by_id = records_by_source_id(records)
    eligible_by_id = eligible_by_source_id(eligible)
    if args.source_id:
        selected: list[EligibleRecord] = []
        for source_id in unique_values(args.source_id):
            if source_id not in by_id:
                raise SystemExit(f"Unknown source id: {source_id}")
            item = eligible_by_id.get(source_id)
            if not item:
                raise SystemExit(f"Source id is not eligible for normalization: {source_id}")
            selected.append(item)
        return selected, 0, "source_id"

    skipped_unsupported = len(records) - len(eligible)
    if args.all:
        return eligible, skipped_unsupported, "all"

    pending: list[EligibleRecord] = []
    for item in eligible:
        output_path = normalized_output_path_for_record(item.record, normalized_root)
        if not output_path.exists() or is_stale(item.record, output_path):
            pending.append(item)
    return pending, skipped_unsupported, "pending"


def normalize_selected_record(
    project_root: Path,
    config: dict[str, Any],
    item: EligibleRecord,
    pdftotext_path: str | None,
) -> NormalizedSource:
    if item.method == "latex":
        return normalize_latex_record(project_root, item.record)
    if item.method == "pdf":
        if not pdftotext_path:
            raise RuntimeError("PDF text extraction requires pdftotext")
        return normalize_pdf_record(project_root, item.record, pdftotext_path)
    if item.method == "link":
        return normalize_link_record(item.record)
    if item.method == "html":
        return normalize_html_record(project_root, item.record)
    if item.method == "table":
        return normalize_table_record(project_root, item.record)
    if item.method == "codebase":
        return normalize_codebase_record(project_root, config, item.record)
    raise RuntimeError(f"Unsupported normalization method: {item.method}")


def resolve_include_path(current_file: Path, include_name: str) -> Path:
    candidate = include_name.strip().replace("\\", "/")
    path = PurePosixPath(candidate)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe include path: {include_name}")
    resolved = current_file.parent / path.as_posix()
    if resolved.is_file() or resolved.name.endswith(".tex"):
        return resolved
    return Path(str(resolved) + ".tex")


def strip_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        escaped = False
        kept: list[str] = []
        for char in line:
            if char == "%" and not escaped:
                break
            kept.append(char)
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        lines.append("".join(kept))
    return "\n".join(lines)


def read_latex_with_includes(
    project_root: Path,
    latex_root: Path,
    path: Path,
    visited: set[Path] | None = None,
    depth: int = 0,
) -> IncludeResult:
    visited = visited or set()
    warnings: list[str] = []
    included_paths: list[str] = []
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path

    rel_label = relative_label(project_root, path)
    if depth > MAX_INCLUDE_DEPTH:
        return IncludeResult(
            text=f"\n\n[Include skipped: {rel_label}]\n\n",
            included_paths=[],
            warnings=[f"{rel_label}: include depth exceeded {MAX_INCLUDE_DEPTH}"],
        )
    if resolved_path in visited:
        return IncludeResult(
            text=f"\n\n[Include skipped: {rel_label}]\n\n",
            included_paths=[],
            warnings=[f"{rel_label}: cyclic include skipped"],
        )
    if not path.is_file():
        return IncludeResult(
            text=f"\n\n[Missing include: {rel_label}]\n\n",
            included_paths=[],
            warnings=[f"{rel_label}: include file not found"],
        )
    if not path.resolve().is_relative_to(latex_root.resolve()):
        return IncludeResult(
            text=f"\n\n[Include skipped outside bundle: {rel_label}]\n\n",
            included_paths=[],
            warnings=[f"{rel_label}: include path resolves outside LaTeX bundle"],
        )

    visited.add(resolved_path)
    included_paths.append(rel_label)
    try:
        text = path.read_text(errors="ignore")
    except OSError as exc:
        return IncludeResult(
            text=f"\n\n[Unreadable include: {rel_label}]\n\n",
            included_paths=[],
            warnings=[f"{rel_label}: cannot read include file: {exc}"],
        )

    text = strip_comments(text)

    def replace_include(match: re.Match[str]) -> str:
        command = match.group(1)
        include_name = match.group(2)
        try:
            include_path = resolve_include_path(path, include_name)
        except ValueError as exc:
            warnings.append(f"{rel_label}: {exc}")
            return f"\n\n[Skipped unsafe {command}: {include_name}]\n\n"
        result = read_latex_with_includes(project_root, latex_root, include_path, visited, depth + 1)
        included_paths.extend(result.included_paths)
        warnings.extend(result.warnings)
        include_label = relative_label(project_root, include_path)
        return f"\n\n% BEGIN {command} {include_label}\n{result.text}\n% END {command} {include_label}\n\n"

    expanded = re.sub(r"\\(input|include)\s*\{([^{}]+)\}", replace_include, text)
    return IncludeResult(expanded, unique_values(included_paths), unique_values(warnings))


def relative_label(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def balanced_argument(text: str, command: str, start: int = 0) -> tuple[str, int] | None:
    match = re.search(rf"\\{re.escape(command)}\*?\s*(?:\[[^\]]*\]\s*)*\{{", text[start:])
    if not match:
        return None
    open_index = start + match.end() - 1
    depth = 0
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index], index + 1
    return None


def first_balanced_argument(text: str, commands: list[str]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for command in commands:
        match = re.search(rf"\\{re.escape(command)}\*?\s*(?:\[[^\]]*\]\s*)*\{{", text)
        if not match:
            continue
        value = balanced_argument(text, command)
        if value:
            candidates.append((match.start(), value[0]))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def all_balanced_arguments(text: str, command: str) -> list[str]:
    values: list[str] = []
    start = 0
    while True:
        match = re.search(rf"\\{re.escape(command)}\*?\s*(?:\[[^\]]*\]\s*)*\{{", text[start:])
        if not match:
            break
        absolute_start = start + match.start()
        result = balanced_argument(text, command, absolute_start)
        if not result:
            break
        values.append(result[0])
        start = result[1]
    return values


def environment_blocks(text: str, environment: str) -> list[str]:
    pattern = re.compile(
        rf"\\begin\{{{re.escape(environment)}\}}(?P<body>.*?)\\end\{{{re.escape(environment)}\}}",
        re.DOTALL,
    )
    return [match.group("body") for match in pattern.finditer(text)]


def first_environment(text: str, environments: list[str]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for environment in environments:
        pattern = re.compile(
            rf"\\begin\{{{re.escape(environment)}\}}(?P<body>.*?)\\end\{{{re.escape(environment)}\}}",
            re.DOTALL,
        )
        match = pattern.search(text)
        if match:
            candidates.append((match.start(), match.group("body")))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def document_body(text: str) -> str:
    begin = re.search(r"\\begin\{document\}", text)
    if begin:
        text = text[begin.end() :]
    end = re.search(r"\\end\{document\}", text)
    if end:
        text = text[: end.start()]
    return text


def latex_to_plain(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\$\$(.*?)\$\$", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\$(.*?)\$", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.*?)\\\]", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\\\((.*?)\\\)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\\\\(?:\[[^\]]*\])?", "\n", text)

    for symbol, replacement in LATEX_SYMBOLS.items():
        text = text.replace(symbol, replacement)

    text = replace_known_inline_commands(text)
    text = drop_known_commands(text)
    text = re.sub(r"\\begin\{[^{}]+\}", "\n", text)
    text = re.sub(r"\\end\{[^{}]+\}", "\n", text)
    text = re.sub(r"\\[a-zA-Z@]+(\*?)", lambda match: "\\" + match.group(0).lstrip("\\") if match.group(0) else "", text)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.strip().splitlines()).strip()


def replace_known_inline_commands(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for command in sorted(INLINE_COMMANDS_WITH_TEXT):
            result = replace_command_with_last_argument(text, command)
            if result != text:
                changed = True
                text = result
    return text


def replace_command_with_last_argument(text: str, command: str) -> str:
    pattern = re.compile(rf"\\{re.escape(command)}\*?\s*(?:\[[^\]]*\]\s*)*\{{")
    output: list[str] = []
    index = 0
    while True:
        match = pattern.search(text, index)
        if not match:
            output.append(text[index:])
            break
        result = balanced_argument(text, command, match.start())
        if not result:
            output.append(text[index:])
            break
        argument, end = result
        second_argument = None
        if command in {"href", "texorpdfstring", "textcolor"}:
            after = re.match(r"\s*\{", text[end:])
            if after:
                next_result = read_braced_at(text, end + after.end() - 1)
                if next_result:
                    second_argument, end = next_result
        output.append(text[index : match.start()])
        output.append(second_argument if second_argument is not None else argument)
        index = end
    return "".join(output)


def read_braced_at(text: str, open_index: int) -> tuple[str, int] | None:
    if open_index >= len(text) or text[open_index] != "{":
        return None
    depth = 0
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index], index + 1
    return None


def drop_known_commands(text: str) -> str:
    for command in sorted(DROP_COMMANDS_WITH_ARGS):
        text = drop_command_arguments(text, command)
    for command in sorted(DROP_LINE_COMMANDS):
        text = re.sub(rf"\\{re.escape(command)}\*?(?:\s*\[[^\]]*\])?(?:\s*\{{[^{{}}]*\}})?", " ", text)
    return text


def drop_command_arguments(text: str, command: str) -> str:
    pattern = re.compile(rf"\\{re.escape(command)}\*?\s*(?:\[[^\]]*\]\s*)*\{{")
    output: list[str] = []
    index = 0
    while True:
        match = pattern.search(text, index)
        if not match:
            output.append(text[index:])
            break
        result = balanced_argument(text, command, match.start())
        if not result:
            output.append(text[index:])
            break
        output.append(text[index : match.start()])
        index = result[1]
    return "".join(output)


def normalize_title(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    title = metadata_to_plain(value)
    title = re.sub(r"\s+", " ", title).strip()
    return title or fallback


def metadata_to_plain(text: str) -> str:
    for command in ("color", "definecolor"):
        text = drop_command_arguments(text, command)
    text = re.sub(
        r"\\(?:bfseries|itshape|scshape|sffamily|rmfamily|ttfamily|small|footnotesize|scriptsize|normalsize|large|Large|LARGE|huge|Huge)\b",
        " ",
        text,
    )
    text = latex_to_plain(text)
    text = re.sub(r"\\[A-Za-z@]+", " ", text)
    return single_line(text)


def extract_authors(text: str) -> list[str]:
    author_block = first_balanced_argument(text, ["author"])
    authors: list[str] = []
    if author_block:
        authors = split_author_block(author_block)
    if authors:
        return authors

    icml_authors = all_balanced_arguments(text, "icmlauthor")
    for author in icml_authors:
        plain = latex_to_plain(author)
        if plain:
            authors.append(plain)
    return unique_values(authors)


def split_author_block(author_block: str) -> list[str]:
    cleaned = author_block
    raw_lines = re.split(r"\\\\", cleaned)
    author_lines: list[str] = []
    for line in raw_lines:
        line_lower = line.lower()
        looks_like_affiliation = (
            "$^" in line
            or "\\texttt" in line_lower
            or "college" in line_lower
            or "department" in line_lower
            or "gmail" in line_lower
            or "laboratory" in line_lower
            or "ministry" in line_lower
            or "school" in line_lower
            or "university" in line_lower
        )
        if looks_like_affiliation and author_lines:
            break
        author_lines.append(line)
    cleaned = "\n".join(author_lines)
    cleaned = re.sub(r"\\thanks\s*\{.*?\}", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\\texttt\s*\{.*?\}", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\$[^$]*\$", "", cleaned)
    cleaned = re.sub(r"\\\[[^\]]*\]", "", cleaned)
    cleaned = cleaned.replace(r"\and", "\n")
    cleaned = cleaned.replace(r"\qquad", "\n")
    cleaned = cleaned.replace(r"\quad", "\n")
    cleaned = re.sub(r"\\\\", "\n", cleaned)
    parts = [part.strip() for part in re.split(r"\n|,|\band\b", cleaned) if part.strip()]
    authors: list[str] = []
    for part in parts:
        plain = metadata_to_plain(part)
        plain = re.sub(r"\s*\d+(?:\s*[,;*]\s*\d+|\s*[*])*$", "", plain)
        plain = re.sub(r"\s+", " ", plain).strip(" ,;")
        if not plain:
            continue
        if plain.strip("* ") == "":
            continue
        plain_lower = plain.lower()
        if (
            "@" in plain
            or ".edu" in plain_lower
            or "college" in plain_lower
            or "department" in plain_lower
            or "gmail" in plain_lower
            or "laboratory" in plain_lower
            or "ministry" in plain_lower
            or "school" in plain_lower
            or "university" in plain_lower
        ):
            continue
        if len(plain) > 100:
            continue
        authors.append(plain)
    return unique_values(authors)


def extract_outline(text: str) -> list[tuple[int, str]]:
    outline: list[tuple[int, str]] = []
    pattern = re.compile(
        r"\\(?P<command>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?"
        r"\s*(?:\[[^\]]*\]\s*)?\{",
    )
    index = 0
    while True:
        match = pattern.search(text, index)
        if not match:
            break
        command = match.group("command")
        result = balanced_argument(text, command, match.start())
        if not result:
            index = match.end()
            continue
        title = latex_to_plain(result[0])
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            outline.append((SECTION_COMMANDS[command], title))
        index = result[1]
    return outline


def convert_headings(text: str) -> str:
    pattern = re.compile(
        r"\\(?P<command>part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?"
        r"\s*(?:\[[^\]]*\]\s*)?\{",
    )
    output: list[str] = []
    index = 0
    while True:
        match = pattern.search(text, index)
        if not match:
            output.append(text[index:])
            break
        command = match.group("command")
        result = balanced_argument(text, command, match.start())
        if not result:
            output.append(text[index : match.end()])
            index = match.end()
            continue
        title = latex_to_plain(result[0])
        title = re.sub(r"\s+", " ", title).strip()
        hashes = "#" * min(6, SECTION_COMMANDS[command] + 1)
        output.append(text[index : match.start()])
        output.append(f"\n\n{hashes} {title}\n\n" if title else "\n\n")
        index = result[1]
    return "".join(output)


def remove_environment(text: str, environment: str) -> str:
    pattern = re.compile(
        rf"\\begin\{{{re.escape(environment)}\}}.*?\\end\{{{re.escape(environment)}\}}",
        re.DOTALL,
    )
    return pattern.sub("\n\n", text)


def extract_media(text: str) -> list[MediaReference]:
    media: list[MediaReference] = []
    for kind in ("figure", "figure*", "table", "table*"):
        for block in environment_blocks(text, kind):
            caption = first_balanced_argument(block, ["caption"])
            label = first_balanced_argument(block, ["label"])
            graphics = all_balanced_arguments(block, "includegraphics")
            media.append(
                MediaReference(
                    kind="figure" if kind.startswith("figure") else "table",
                    caption=single_line(latex_to_plain(caption)) if caption else None,
                    label=single_line(latex_to_plain(label)) if label else None,
                    graphics=[single_line(latex_to_plain(value)) for value in graphics] or None,
                )
            )
    return media


def extract_bibliography_files(text: str) -> list[str]:
    values: list[str] = []
    for command in ("bibliography", "addbibresource"):
        for value in all_balanced_arguments(text, command):
            for item in value.split(","):
                clean = latex_to_plain(item).strip()
                if clean:
                    values.append(clean)
    return unique_values(values)


def normalize_arxiv_identifier(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = latex_to_plain(value).strip().lower()
    text = text.removeprefix("arxiv:").strip()
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "https://arxiv.org/pdf/", "http://arxiv.org/pdf/"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
            break
    text = text.removesuffix(".pdf").strip()
    text = text.rstrip(".,;:)]}")
    return text if ARXIV_ID_RE.match(text) else None


def normalize_doi_identifier(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = latex_to_plain(value).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
            break
    text = re.sub(r"\s+", "", text).rstrip(".,;:)]}")
    return text or None


def doi_from_record(record: dict[str, Any]) -> str | None:
    doi = record.get("doi")
    if isinstance(doi, str) and doi.strip():
        return doi
    metadata = record_metadata(record)
    doi = metadata.get("doi")
    return doi if isinstance(doi, str) and doi.strip() else None


def manifest_reference_index(records: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for record in records:
        source_id = record.get("id")
        if not isinstance(source_id, str) or not source_id:
            continue
        arxiv_id = arxiv_id_from_record(record)
        if arxiv_id:
            index.setdefault(("arxiv", normalize_arxiv_identifier(arxiv_id) or arxiv_id.lower()), source_id)
        if source_id.startswith("paper:"):
            source_arxiv_id = normalize_arxiv_identifier(source_id.removeprefix("paper:"))
            if source_arxiv_id:
                index.setdefault(("arxiv", source_arxiv_id), source_id)
        raw_pdf = raw_pdf_value(record)
        if raw_pdf:
            raw_pdf_arxiv_id = normalize_arxiv_identifier(PurePosixPath(raw_pdf).stem)
            if raw_pdf_arxiv_id:
                index.setdefault(("arxiv", raw_pdf_arxiv_id), source_id)
        record_doi = normalize_doi_identifier(doi_from_record(record))
        if record_doi:
            index.setdefault(("doi", record_doi), source_id)
        if source_id.lower().startswith("doi:"):
            source_doi = normalize_doi_identifier(source_id)
            if source_doi:
                index.setdefault(("doi", source_doi), source_id)
    return index


def resolve_bibliography_path(latex_root: Path, bibliography_file: str) -> Path | None:
    candidate = bibliography_file.strip().replace("\\", "/")
    parsed = urlparse(candidate)
    if not candidate or "://" in candidate or parsed.scheme:
        return None
    path = PurePosixPath(candidate)
    if path.is_absolute() or ".." in path.parts:
        return None
    resolved = latex_root / path.as_posix()
    if resolved.suffix.lower() != ".bib":
        resolved = Path(str(resolved) + ".bib")
    try:
        if not resolved.resolve().is_relative_to(latex_root.resolve()):
            return None
    except OSError:
        return None
    return resolved


def bibtex_entries(text: str) -> list[str]:
    entries: list[str] = []
    start = 0
    while True:
        match = re.search(r"@\w+\s*\{", text[start:], re.IGNORECASE)
        if not match:
            break
        open_index = start + match.end() - 1
        depth = 0
        escaped = False
        for index in range(open_index, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    entries.append(text[open_index + 1 : index])
                    start = index + 1
                    break
        else:
            break
    return entries


def parse_bibtex_value(text: str, start: int) -> tuple[str, int] | None:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text):
        return None
    opener = text[start]
    if opener == "{":
        depth = 0
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start + 1 : index], index + 1
        return None
    if opener == '"':
        escaped = False
        for index in range(start + 1, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                return text[start + 1 : index], index + 1
        return None
    match = re.match(r"[^,\s}]+", text[start:])
    if not match:
        return None
    return match.group(0), start + match.end()


def parse_bibtex_fields(entry: str) -> dict[str, str]:
    body = entry.split(",", 1)[1] if "," in entry else entry
    fields: dict[str, str] = {}
    start = 0
    while start < len(body):
        match = re.search(r"(?P<name>[A-Za-z][A-Za-z0-9_-]*)\s*=", body[start:])
        if not match:
            break
        name = match.group("name").lower()
        value_start = start + match.end()
        parsed = parse_bibtex_value(body, value_start)
        if parsed is None:
            start = value_start + 1
            continue
        value, start = parsed
        fields[name] = single_line(latex_to_plain(value))
    return fields


def bibtex_reference_identifiers(fields: dict[str, str]) -> list[tuple[str, str]]:
    identifiers: list[tuple[str, str]] = []
    doi = normalize_doi_identifier(fields.get("doi"))
    if doi:
        identifiers.append(("doi", doi))
    for key in ("arxiv", "arxivid"):
        arxiv_id = normalize_arxiv_identifier(fields.get(key))
        if arxiv_id:
            identifiers.append(("arxiv", arxiv_id))
    eprint = fields.get("eprint")
    eprint_kind = " ".join(
        value
        for value in (fields.get("archiveprefix"), fields.get("eprinttype"))
        if value
    ).lower()
    if eprint and ("arxiv" in eprint_kind or normalize_arxiv_identifier(eprint) is not None):
        arxiv_id = normalize_arxiv_identifier(eprint)
        if arxiv_id:
            identifiers.append(("arxiv", arxiv_id))
    return unique_values(identifiers)


def matched_reference_source_ids(
    source: NormalizedSource,
    manifest_records: list[dict[str, Any]] | None,
    project_root: Path | None,
) -> list[str]:
    if not manifest_records or project_root is None or not source.bibliography_files:
        return []
    latex_root_value = source.record.get("latex_root")
    if not isinstance(latex_root_value, str):
        return []
    latex_root = project_root / latex_root_value
    index = manifest_reference_index(manifest_records)
    current_source_id = record_id(source.record)
    matches: list[str] = []
    for bibliography_file in source.bibliography_files:
        bib_path = resolve_bibliography_path(latex_root, bibliography_file)
        if bib_path is None or not bib_path.is_file():
            continue
        try:
            entries = bibtex_entries(bib_path.read_text(errors="ignore"))
        except OSError:
            continue
        for entry in entries:
            fields = parse_bibtex_fields(entry)
            for identifier in bibtex_reference_identifiers(fields):
                target_source_id = index.get(identifier)
                if target_source_id and target_source_id != current_source_id:
                    matches.append(target_source_id)
    return unique_values(matches)


def extract_links(text: str) -> list[str]:
    links = URL_RE.findall(text)
    for value in all_balanced_arguments(text, "url"):
        if value.startswith("http://") or value.startswith("https://"):
            links.append(value.strip())
    start = 0
    while True:
        match = re.search(r"\\href\s*\{", text[start:])
        if not match:
            break
        absolute_start = start + match.start()
        first = balanced_argument(text, "href", absolute_start)
        if not first:
            break
        if first[0].startswith("http://") or first[0].startswith("https://"):
            links.append(first[0].strip())
        start = first[1]
    return unique_values([link.rstrip(".,;:)]}") for link in links])


def extract_unresolved_macros(text: str) -> list[str]:
    macros: list[str] = []
    for macro in re.findall(r"\\[A-Za-z@]+", text):
        name = macro[1:]
        if name in INLINE_COMMANDS_WITH_TEXT or name in DROP_COMMANDS_WITH_ARGS or name in DROP_LINE_COMMANDS:
            continue
        if name in {"begin", "end"} or name in SECTION_COMMANDS:
            continue
        macros.append(macro)
    return unique_values(macros)[:MAX_UNRESOLVED_MACROS]


def prepare_extracted_text(text: str, abstract: str | None) -> str:
    body = document_body(text)
    if abstract:
        body = re.sub(r"\\begin\{abstract\}.*?\\end\{abstract\}", "\n\n", body, count=1, flags=re.DOTALL)
    for environment in ENVIRONMENTS_TO_DROP:
        body = remove_environment(body, environment)
    body = convert_headings(body)
    body = re.sub(r"\\item(?:\s*\[[^\]]*\])?", "\n- ", body)
    body = drop_known_commands(body)
    body = latex_to_plain(body)
    body = normalize_markdown_spacing(body)
    return body or "None extracted."


def normalize_markdown_spacing(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        cleaned.append(line)
        previous_blank = blank
    return "\n".join(cleaned).strip()


def single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_latex_record(project_root: Path, record: dict[str, Any]) -> NormalizedSource:
    source_id = str(record.get("id", "unknown"))
    warnings: list[str] = []
    latex_root_value = record.get("latex_root")
    entrypoint_value = record.get("entrypoint")
    if not isinstance(latex_root_value, str) or not isinstance(entrypoint_value, str):
        return NormalizedSource(
            record=record,
            extraction_method="latex",
            title=None,
            authors=[],
            abstract=None,
            outline=[],
            extracted_text="None extracted.",
            media=[],
            links=[],
            bibliography_files=[],
            included_paths=[],
            warnings=[f"{source_id}: missing latex_root or entrypoint"],
        )

    latex_root = project_root / latex_root_value
    entrypoint = latex_root / entrypoint_value
    include_result = read_latex_with_includes(project_root, latex_root, entrypoint)
    warnings.extend(include_result.warnings)
    text = include_result.text

    title = normalize_title(first_balanced_argument(text, ["title", "icmltitle"]), source_id)
    authors = extract_authors(text)
    abstract_block = first_environment(text, ["abstract"])
    abstract = latex_to_plain(abstract_block) if abstract_block else None
    abstract = normalize_markdown_spacing(abstract) if abstract else None
    outline = extract_outline(document_body(text))
    media = extract_media(text)
    links = extract_links(text)
    bibliography_files = extract_bibliography_files(text)
    extracted_text = prepare_extracted_text(text, abstract)
    unresolved = extract_unresolved_macros(extracted_text)

    if not title or title == source_id:
        warnings.append(f"{source_id}: title not extracted")
    if not abstract:
        warnings.append(f"{source_id}: abstract not extracted")
    if not outline:
        warnings.append(f"{source_id}: outline not extracted")
    if unresolved:
        warnings.append(f"{source_id}: unresolved LaTeX commands preserved: {', '.join(unresolved)}")

    return NormalizedSource(
        record=record,
        extraction_method="latex",
        title=title,
        authors=authors,
        abstract=abstract,
        outline=outline,
        extracted_text=extracted_text,
        media=media,
        links=links,
        bibliography_files=bibliography_files,
        included_paths=include_result.included_paths,
        warnings=unique_values(warnings),
    )


def manifest_warnings(record: dict[str, Any]) -> list[str]:
    metadata = record_metadata(record)
    warnings = metadata.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [warning for warning in warnings if isinstance(warning, str)]


def run_pdftotext(
    pdftotext_path: str,
    pdf_path: Path,
    pdf_label: str,
    *,
    layout: bool,
) -> tuple[str, list[str], bool]:
    args = [pdftotext_path, "-enc", "UTF-8"]
    if layout:
        args.append("-layout")
    args.extend([str(pdf_path), "-"])
    pass_name = "layout" if layout else "reading-order"
    try:
        result = subprocess.run(  # noqa: S603 - pdftotext path is resolved/configured explicitly, shell=False
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except OSError as exc:
        return "", [f"{pdf_label}: cannot run pdftotext: {exc}"], False
    except subprocess.TimeoutExpired:
        return "", [f"{pdf_label}: pdftotext {pass_name} pass timed out after 120 seconds"], False

    warnings: list[str] = []
    if result.stderr.strip():
        warnings.append(f"{pdf_label}: pdftotext {pass_name} warning: {single_line(result.stderr.strip())}")
    if result.returncode != 0:
        warnings.append(f"{pdf_label}: pdftotext {pass_name} pass exited with code {result.returncode}")
        return "", warnings, False
    if not result.stdout.strip():
        warnings.append(f"{pdf_label}: pdftotext {pass_name} pass produced no output")
        return result.stdout, warnings, True
    return result.stdout, warnings, True


def extract_pdf_text(pdftotext_path: str, pdf_path: Path, pdf_label: str) -> tuple[str, list[str], bool]:
    """Return (raw pdftotext output, warnings, extractor_ran).

    ``extractor_ran`` is True when pdftotext executed and exited 0, even when
    it produced no text — that distinction feeds the scanned-PDF (needs OCR)
    heuristic, which must not fire for environment or execution failures.
    """
    reading_text, warnings, reading_ran = run_pdftotext(pdftotext_path, pdf_path, pdf_label, layout=False)
    layout_text, layout_warnings, layout_ran = run_pdftotext(pdftotext_path, pdf_path, pdf_label, layout=True)
    warnings.extend(layout_warnings)
    if layout_ran and layout_text.strip():
        PDF_LAYOUT_TEXT_CACHE[pdf_label] = layout_text
    if reading_ran:
        return reading_text, warnings, True
    if layout_ran:
        return layout_text, warnings, True
    return "", warnings, False


def normalize_pdf_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\f", "\n\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"-\n\s*(?=[a-z])", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.splitlines()]
    cleaned: list[str] = []
    previous_blank = False
    for line in lines:
        if re.fullmatch(r"arXiv:\d{4}\.\d{4,5}v\d+(?:\s+\[[^\]]+\])?(?:\s+\d{1,2}\s+\w{3}\s+\d{4})?", line, re.IGNORECASE):
            continue
        blank = not line
        if blank and previous_blank:
            continue
        cleaned.append(line)
        previous_blank = blank
    return "\n".join(cleaned).strip()


def meaningful_pdf_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        clean = single_line(line)
        if not clean:
            continue
        if re.fullmatch(r"\d+", clean):
            continue
        lines.append(clean)
    return lines


def is_pdf_metadata_line(line: str) -> bool:
    lower = line.lower()
    return (
        lower.startswith("arxiv:")
        or lower.startswith("copyright")
        or lower.startswith("published as ")
        or lower.startswith("published ")
        or lower.startswith("contents")
        or "@" in line
    )


def is_likely_author_or_affiliation_line(line: str) -> bool:
    lower = line.lower()
    return (
        line.count(",") >= 2
        or " university" in f" {lower}"
        or " institute" in f" {lower}"
        or " systems" in f" {lower}"
        or " laboratory" in f" {lower}"
        or " school " in f" {lower} "
        or re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\d", line) is not None
        or re.search(r"\b[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][A-Za-z.'-]+\s+\d\b", line) is not None
    )


def truncate_inline_abstract(line: str) -> tuple[str, bool]:
    match = re.search(r"\s+Abstract\b", line)
    if match is None:
        return line, False
    return line[: match.start()].strip(), True


def has_letter_spaced_pdf_title_signal(text: str) -> bool:
    first_fragments = list(re.finditer(r"\b([A-Z])\s+([A-Z]{2,})(?=[-\s]|$)", text))
    hyphen_fragments = list(re.finditer(r"(?<=-)([A-Z])\s+([A-Z]{2,})(?=[-\s]|$)", text))
    if hyphen_fragments:
        return True
    return any(
        text[left.end() : right.start()].strip() == ""
        for left, right in zip(first_fragments, first_fragments[1:], strict=False)
    )


def collapse_letter_spaced_pdf_title(text: str) -> tuple[str, bool]:
    if not has_letter_spaced_pdf_title_signal(text):
        return text, False
    original = text
    collapsed = re.sub(r"\b([A-Z])\s+([A-Z]{2,})(?=[-\s]|$)", r"\1\2", text)
    collapsed = re.sub(r"(?<=-)([A-Z])\s+([A-Z]{2,})(?=[-\s]|$)", r"\1\2", collapsed)
    return collapsed, collapsed != original


def infer_pdf_title(text: str, source_id: str) -> tuple[str, str]:
    """Return (title, confidence) where confidence is 'high', 'low', or 'none'."""
    lines = meaningful_pdf_lines(text)
    search_limit = min(len(lines), 80)
    start_index: int | None = None
    for index in range(search_limit):
        line = lines[index]
        normalized = re.sub(r"\s+", "", line).lower()
        if normalized in {"abstract", "a bstract", "introduction", "contents"}:
            break
        if len(line) < 8 or is_pdf_metadata_line(line):
            continue
        line, truncated = truncate_inline_abstract(line)
        if len(line) < 8:
            if truncated:
                break
            continue
        start_index = index
        break
    if start_index is None:
        return source_id, "none"

    first_line, truncated_inline = truncate_inline_abstract(lines[start_index])
    title_lines = [first_line]
    for line in lines[start_index + 1 : min(len(lines), start_index + 4)]:
        normalized = re.sub(r"\s+", "", line).lower()
        if normalized in {"abstract", "a bstract", "contents", "introduction", "1introduction"}:
            break
        line, truncated = truncate_inline_abstract(line)
        if truncated:
            truncated_inline = True
            if line:
                title_lines.append(line)
            break
        if is_pdf_metadata_line(line) or is_likely_author_or_affiliation_line(line):
            break
        title_lines.append(line)
    title = " ".join(title_lines)
    title = re.sub(r"\s+", " ", title).strip(" -")
    title, collapsed_spaced_caps = collapse_letter_spaced_pdf_title(title)
    if not title:
        return source_id, "none"
    if truncated_inline or collapsed_spaced_caps:
        confidence = "low"
    elif len(title_lines) >= 2 or len(title) > 20:
        confidence = "high"
    else:
        confidence = "low"
    return title, confidence


def provider_identity_metadata(record: dict[str, Any]) -> tuple[str, list[str]] | None:
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return None
    if provenance.get("retrieved_by") not in {"fetch_sources.py/arxiv", "fetch_sources.py/openalex"}:
        return None
    title = provenance.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    authors_value = provenance.get("authors")
    authors = [
        single_line(author)
        for author in authors_value
        if isinstance(author, str) and author.strip()
    ] if isinstance(authors_value, list) else []
    return single_line(title), unique_values(authors)


def extract_pdf_abstract_details(text: str) -> PdfAbstractExtraction:
    lines = meaningful_pdf_lines(text)
    start: int | None = None
    for index, line in enumerate(lines[:200]):
        normalized = re.sub(r"[^a-z]", "", line.lower())
        if normalized == "abstract":
            start = index + 1
            break
    if start is None:
        return PdfAbstractExtraction(None, "none")

    abstract_lines: list[str] = []
    stopped_on_heading = False
    for line in lines[start:]:
        normalized = re.sub(r"\s+", " ", line).strip()
        heading_key = re.sub(r"[^a-z0-9]", "", normalized.lower())
        if heading_key in {"1introduction", "introduction", "contents"}:
            stopped_on_heading = True
            break
        if re.match(r"^\d+(\.\d+)*\s+[A-Z][A-Za-z0-9 ,:;()/-]{3,}$", normalized):
            stopped_on_heading = True
            break
        abstract_lines.append(normalized)
        if len(" ".join(abstract_lines)) > 3000:
            break
    abstract = normalize_markdown_spacing(" ".join(abstract_lines))
    if abstract:
        return PdfAbstractExtraction(abstract, "high")
    if stopped_on_heading:
        for line in lines[start + 1 : start + 200]:
            candidate = re.sub(r"\s+", " ", line).strip()
            heading_key = re.sub(r"[^a-z0-9]", "", candidate.lower())
            if heading_key in {"abstract", "contents", "references"}:
                continue
            if re.match(r"^\d+(\.\d+)*\s+[A-Z][A-Za-z0-9 ,:;()/-]{3,}$", candidate):
                continue
            if len(candidate) >= 80 and len(re.findall(r"[.!?](?:\s|$)", candidate)) >= 2:
                return PdfAbstractExtraction(normalize_markdown_spacing(candidate), "low", recovered_by_fallback=True)
    return PdfAbstractExtraction(None, "none")


def extract_pdf_abstract(text: str) -> str | None:
    return extract_pdf_abstract_details(text).text



def extract_pdf_outline(text: str) -> list[tuple[int, str]]:
    outline: list[tuple[int, str]] = []
    seen: set[str] = set()
    for line in meaningful_pdf_lines(text):
        clean = single_line(line)
        match = re.match(r"^(?P<num>\d+(?:\.\d+)*)\.?\s+(?P<title>[A-Z][A-Za-z0-9 ,:;()/-]{3,})$", clean)
        if match:
            title = single_line(match.group("title"))
            if title.lower() in {"introduction", "abstract"}:
                pass
            level = min(6, 1 + match.group("num").count("."))
        elif re.match(r"^[A-Z][A-Z0-9 ,:;()/-]{8,}$", clean) and len(clean) <= 100:
            title = clean.title()
            level = 2
        else:
            continue
        key = title.lower()
        if key not in seen:
            outline.append((level, title))
            seen.add(key)
        if len(outline) >= 80:
            break
    return outline


def extract_pdf_media(text: str) -> list[MediaReference]:
    media: list[MediaReference] = []
    pattern = re.compile(r"\b(?P<kind>Figure|Table)\s+(?P<label>[A-Za-z0-9.:-]+)\s*[:.-]\s*(?P<caption>.+)")
    for line in meaningful_pdf_lines(text):
        match = pattern.search(line)
        if not match:
            continue
        media.append(
            MediaReference(
                kind=match.group("kind").lower(),
                caption=single_line(match.group("caption")),
                label=single_line(match.group("label")),
                graphics=None,
            )
        )
        if len(media) >= 50:
            break
    return media


def normalize_pdf_record(project_root: Path, record: dict[str, Any], pdftotext_path: str) -> NormalizedSource:
    source_id = str(record.get("id", "unknown"))
    warnings = manifest_warnings(record)
    raw_pdf = raw_pdf_value(record)
    if not raw_pdf:
        return NormalizedSource(
            record=record,
            extraction_method="pdf_text",
            title=source_id,
            authors=[],
            abstract=None,
            outline=[],
            extracted_text="None extracted.",
            media=[],
            links=[],
            bibliography_files=[],
            included_paths=[],
            warnings=unique_values([*warnings, f"{source_id}: raw PDF path not found"]),
        )

    pdf_path = safe_workspace_path(project_root, raw_pdf)
    if pdf_path is None or not pdf_path.is_file():
        return NormalizedSource(
            record=record,
            extraction_method="pdf_text",
            title=source_id,
            authors=[],
            abstract=None,
            outline=[],
            extracted_text="None extracted.",
            media=[],
            links=[],
            bibliography_files=[],
            included_paths=[],
            warnings=unique_values([*warnings, f"{source_id}: raw PDF file not found: {raw_pdf}"]),
        )

    extracted_text, extraction_warnings, extractor_ran = extract_pdf_text(pdftotext_path, pdf_path, raw_pdf)
    warnings.extend(extraction_warnings)
    # pdftotext terminates every page with a form feed, so the raw output
    # carries the page count even when no text was extracted.
    page_count = max(1, extracted_text.count("\f"))
    normalized_text = normalize_pdf_text(extracted_text)
    title, title_confidence = infer_pdf_title(normalized_text, source_id)
    extracted_title = title
    title_source = "pdf_inference"
    authors: list[str] = []
    provider_identity = provider_identity_metadata(record)
    if provider_identity is not None:
        title, authors = provider_identity
        title_source = "provider"
    abstract_result = extract_pdf_abstract_details(normalized_text)
    abstract = abstract_result.text
    outline = extract_pdf_outline(normalized_text)
    layout_text = PDF_LAYOUT_TEXT_CACHE.get(raw_pdf, extracted_text)
    media = extract_pdf_media(normalize_pdf_text(layout_text))
    links = extract_links(normalized_text)

    needs_ocr = extractor_ran and len(normalized_text) < PDF_MIN_CHARS_PER_PAGE * page_count
    if needs_ocr:
        warnings.append(
            f"{source_id}: extracted {len(normalized_text)} character(s) across {page_count} page(s); "
            "likely a scanned or image-only PDF (needs OCR)"
        )
    if len(normalized_text) < PDF_MIN_USEFUL_CHARS:
        warnings.append(f"{source_id}: pdftotext produced no useful text")
        if not normalized_text.strip():
            normalized_text = "None extracted."
    if title == source_id:
        warnings.append(f"{source_id}: title inferred from source ID")
    if abstract_result.recovered_by_fallback:
        warnings.append(f"{source_id}: abstract recovery fallback used after PDF heading reordering")
    if not abstract:
        warnings.append(f"{source_id}: abstract not extracted from PDF text")
    if not outline:
        warnings.append(f"{source_id}: outline not extracted from PDF text")

    return NormalizedSource(
        record=record,
        extraction_method="pdf_text",
        title=title,
        authors=authors,
        abstract=abstract,
        outline=outline,
        extracted_text=normalized_text,
        media=media,
        links=links,
        bibliography_files=[],
        included_paths=[],
        warnings=unique_values(warnings),
        title_confidence=title_confidence,
        abstract_confidence=abstract_result.confidence,
        needs_ocr=needs_ocr,
        title_source=title_source,
        extracted_title=extracted_title,
    )


def normalize_link_record(record: dict[str, Any]) -> NormalizedSource:
    source_id = str(record.get("id", "unknown"))
    url = record_url(record)
    kind = record.get("kind")
    method = "link_stub" if kind == "repo_link" else "web_stub"
    title = link_title(record)
    warnings = manifest_warnings(record)
    if not url:
        warnings.append(f"{source_id}: URL not found")
    if kind == "repo_link":
        abstract = "Repository link stub. Network content has not been fetched."
        outline = [(2, "Repository URL"), (2, "Raw link evidence")]
    else:
        abstract = "Web link stub. Network content has not been fetched."
        outline = [(2, "Web URL"), (2, "Raw link evidence")]

    return NormalizedSource(
        record=record,
        extraction_method=method,
        title=title,
        authors=[],
        abstract=abstract,
        outline=outline,
        extracted_text="None extracted.",
        media=[],
        links=[url] if url else [],
        bibliography_files=[],
        included_paths=[],
        warnings=unique_values(warnings),
    )


class HTMLContentExtractor(HTMLParser):
    """Deterministic stdlib extraction of title, outline, links, and body text.

    Boundaries: no JS rendering, no remote asset fetching. Content inside
    script/style/nav (and other non-content tags) is dropped.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.outline: list[tuple[int, str]] = []
        self.links: list[str] = []
        self.text_chunks: list[str] = []
        self.meta_description: str | None = None
        self.unbalanced_skip_tags = False
        self._skip_depth = 0
        self._in_title = False
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HTML_SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in {"h1", "h2", "h3"}:
            self._flush_heading()
            self._heading_level = int(tag[1])
            return
        if tag == "a":
            href = next((value for name, value in attrs if name == "href" and value), None)
            if href and href.startswith(("http://", "https://")) and len(self.links) < HTML_MAX_LINKS:
                if href not in self.links:
                    self.links.append(href)
        if tag == "meta" and self.meta_description is None:
            attributes = {name: value for name, value in attrs if value is not None}
            if attributes.get("name", "").lower() == "description" and attributes.get("content", "").strip():
                self.meta_description = " ".join(attributes["content"].split())
        if tag in HTML_BLOCK_TAGS:
            self.text_chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag in HTML_SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_endtag(self, tag: str) -> None:
        if tag in HTML_SKIP_TAGS:
            if self._skip_depth == 0:
                self.unbalanced_skip_tags = True
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in {"h1", "h2", "h3"}:
            self._flush_heading()
            return
        if tag in HTML_BLOCK_TAGS:
            self.text_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data.strip():
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._heading_level is not None:
            self._heading_parts.append(data)
        self.text_chunks.append(data)

    def _flush_heading(self) -> None:
        if self._heading_level is None:
            return
        heading = " ".join(" ".join(self._heading_parts).split())
        if heading and len(self.outline) < HTML_MAX_OUTLINE_ENTRIES:
            self.outline.append((self._heading_level + 1, heading))
        self._heading_level = None
        self._heading_parts = []

    def close(self) -> None:  # noqa: D102 - inherited behavior plus heading flush
        self._flush_heading()
        if self._skip_depth:
            self.unbalanced_skip_tags = True
        super().close()


def normalize_html_body_text(chunks: list[str]) -> str:
    text = "".join(chunks)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    collapsed: list[str] = []
    previous_blank = True
    for line in lines:
        blank = not line
        if blank and previous_blank:
            continue
        collapsed.append(line)
        previous_blank = blank
    return "\n".join(collapsed).strip()


def strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]*>", " ", text)


def read_html_text(html_path: Path, relative_path: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        data = html_path.read_bytes()
    except OSError as exc:
        return "", [f"{relative_path}: cannot read HTML file: {exc}"]
    if len(data) > HTML_MAX_BYTES:
        warnings.append(
            f"{relative_path}: HTML file exceeds {HTML_MAX_BYTES} bytes; extraction truncated"
        )
        data = data[:HTML_MAX_BYTES]
    return data.decode("utf-8", errors="replace"), warnings


def html_unusable_evidence_reasons(title: str, body_text: str, raw_html: str) -> list[str]:
    reasons: list[str] = []
    visible = normalize_markdown_spacing(body_text)
    haystack = single_line(f"{title} {visible}").lower()
    raw_lower = raw_html.lower()

    if re.search(r"\b404\b|page not found|not found", haystack):
        reasons.append("html_error_page:not_found")
    elif len(visible) < 1000 and re.search(
        r"service unavailable|temporarily unavailable|maintenance|error page|official .* unavailable",
        haystack,
    ):
        reasons.append("html_error_page:official_error_page")

    script_count = len(re.findall(r"<script\b", raw_lower))
    javascript_required = re.search(
        r"(enable|requires?|need).{0,80}javascript|javascript.{0,80}(required|disabled|enable)",
        f"{raw_lower}\n{haystack}",
    )
    content_thin = len(visible) < 200
    if (javascript_required and content_thin) or (script_count >= 2 and content_thin):
        reasons.append("html_javascript_shell")
    return unique_values(reasons)


def normalize_html_record(project_root: Path, record: dict[str, Any]) -> NormalizedSource:
    source_id = record_id(record)
    warnings = manifest_warnings(record)
    raw_path = html_raw_path(record)
    html_path = safe_workspace_path(project_root, raw_path) if raw_path else None
    if raw_path is None or html_path is None or not html_path.is_file():
        warnings.append(f"{source_id}: raw HTML file not found: {raw_path or '<missing>'}")
        return NormalizedSource(
            record=record,
            extraction_method="html_text",
            title=source_id,
            authors=[],
            abstract=None,
            outline=[],
            extracted_text="None extracted.",
            media=[],
            links=[],
            bibliography_files=[],
            included_paths=[],
            warnings=unique_values(warnings),
        )

    text, read_warnings = read_html_text(html_path, raw_path)
    warnings.extend(read_warnings)
    extractor = HTMLContentExtractor()
    try:
        extractor.feed(text)
        extractor.close()
        body_text = normalize_html_body_text(extractor.text_chunks)
    except Exception as exc:  # html.parser is lenient; guard against pathological input
        warnings.append(f"{raw_path}: malformed HTML markup ({exc}); degraded to tag-stripped text")
        body_text = normalize_html_body_text([strip_html_tags(text)])
    if extractor.unbalanced_skip_tags:
        warnings.append(f"{raw_path}: malformed HTML markup (unbalanced script/style/nav tags)")

    title = " ".join(" ".join(extractor.title_parts).split())
    title_confidence = "high"
    if not title:
        first_heading = next((heading for _, heading in extractor.outline), None)
        title = first_heading or PurePosixPath(raw_path).stem
        title_confidence = "low" if first_heading else "none"
        warnings.append(f"{raw_path}: no <title> element; title inferred from {'first heading' if first_heading else 'file name'}")
    unusable_reasons = html_unusable_evidence_reasons(title, body_text, text)
    if unusable_reasons:
        set_record_unusable_evidence(record, [*record_unusable_evidence_reasons(record), *unusable_reasons])
        warnings.extend(f"{source_id}: unusable evidence: {reason}" for reason in unusable_reasons)
    if not body_text:
        warnings.append(f"{raw_path}: no visible body text extracted")
        body_text = "None extracted."

    return NormalizedSource(
        record=record,
        extraction_method="html_text",
        title=title,
        authors=[],
        abstract=extractor.meta_description,
        outline=extractor.outline,
        extracted_text=body_text,
        media=[],
        links=extractor.links,
        bibliography_files=[],
        included_paths=[],
        warnings=unique_values(warnings),
        title_confidence=title_confidence,
    )


def escape_table_cell(value: str) -> str:
    cell = " ".join(value.split()).replace("|", "\\|")
    if len(cell) > TABLE_MAX_CELL_CHARS:
        cell = cell[: TABLE_MAX_CELL_CHARS - 1] + "…"
    return cell


def infer_table_delimiter(sample: str, suffix: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return "\t" if suffix == ".tsv" else ","


def read_table_text(table_path: Path, relative_path: str) -> tuple[str, bool, list[str]]:
    warnings: list[str] = []
    try:
        data = table_path.read_bytes()
    except OSError as exc:
        return "", False, [f"{relative_path}: cannot read table file: {exc}"]
    truncated = len(data) > TABLE_MAX_BYTES
    if truncated:
        warnings.append(
            f"{relative_path}: table file exceeds {TABLE_MAX_BYTES} bytes; row scan truncated"
        )
        data = data[:TABLE_MAX_BYTES]
    text = data.decode("utf-8-sig", errors="replace")
    if truncated:
        # Drop the (likely partial) final line so the reader never sees torn rows.
        text = text[: text.rfind("\n") + 1] if "\n" in text else ""
    return text, truncated, warnings


def render_sample_table(header: list[str], sample_rows: list[list[str]]) -> list[str]:
    header_cells = [escape_table_cell(cell) or " " for cell in header]
    lines = [
        "| " + " | ".join(header_cells) + " |",
        "|" + "|".join("------" for _ in header_cells) + "|",
    ]
    for row in sample_rows:
        cells = [escape_table_cell(cell) for cell in row[: len(header_cells)]]
        cells.extend("" for _ in range(len(header_cells) - len(cells)))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def normalize_table_record(project_root: Path, record: dict[str, Any]) -> NormalizedSource:
    source_id = record_id(record)
    warnings = manifest_warnings(record)
    raw_path = table_raw_path(record)
    table_path = safe_workspace_path(project_root, raw_path) if raw_path else None
    if raw_path is None or table_path is None or not table_path.is_file():
        warnings.append(f"{source_id}: raw table file not found: {raw_path or '<missing>'}")
        return NormalizedSource(
            record=record,
            extraction_method="table_text",
            title=source_id,
            authors=[],
            abstract=None,
            outline=[],
            extracted_text="None extracted.",
            media=[],
            links=[],
            bibliography_files=[],
            included_paths=[],
            warnings=unique_values(warnings),
        )

    text, truncated, read_warnings = read_table_text(table_path, raw_path)
    warnings.extend(read_warnings)
    suffix = PurePosixPath(raw_path).suffix.lower()
    title = PurePosixPath(raw_path).name
    if not text.strip():
        warnings.append(f"{raw_path}: table file has no parseable rows")
        return NormalizedSource(
            record=record,
            extraction_method="table_text",
            title=title,
            authors=[],
            abstract=None,
            outline=[],
            extracted_text="None extracted.",
            media=[],
            links=[],
            bibliography_files=[],
            included_paths=[],
            warnings=unique_values(warnings),
        )

    delimiter = infer_table_delimiter(text[:8192], suffix)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header: list[str] = []
    sample_rows: list[list[str]] = []
    row_count = 0
    ragged_rows = 0
    first_ragged_line: int | None = None
    for line_number, row in enumerate(reader, start=1):
        if not header:
            header = [cell.strip() for cell in row]
            continue
        row_count += 1
        if len(row) != len(header):
            ragged_rows += 1
            if first_ragged_line is None:
                first_ragged_line = line_number
        if len(sample_rows) < TABLE_SAMPLE_ROWS:
            sample_rows.append(row)
    if ragged_rows:
        warnings.append(
            f"{raw_path}: {ragged_rows} row(s) do not match the {len(header)}-column header "
            f"(first at line {first_ragged_line})"
        )

    delimiter_label = "tab" if delimiter == "\t" else f"`{delimiter}`"
    row_count_label = f"at least {row_count} (truncated)" if truncated else str(row_count)
    text_lines = [
        f"Columns ({len(header)}): " + ", ".join(escape_table_cell(cell) for cell in header),
        f"Delimiter: {delimiter_label}",
        f"Data rows: {row_count_label}",
        "",
        f"Sample rows (first {len(sample_rows)}):",
        "",
        *render_sample_table(header, sample_rows),
    ]
    return NormalizedSource(
        record=record,
        extraction_method="table_text",
        title=title,
        authors=[],
        abstract=f"Tabular data with {len(header)} column(s) and {row_count_label} data row(s).",
        outline=[(2, "Columns"), (2, "Sample Rows")],
        extracted_text="\n".join(text_lines),
        media=[],
        links=[],
        bibliography_files=[],
        included_paths=[],
        warnings=unique_values(warnings),
    )


def artifact_dir_for_record(project_root: Path, config: dict[str, Any], record: dict[str, Any]) -> tuple[Path | None, str]:
    metadata = record_metadata(record)
    expected = f"{codebase_output_dir(config)}/{safe_source_id(record_id(record))}"
    relative = metadata.get("codebase_output_dir")
    if not isinstance(relative, str) or not relative.strip():
        relative = expected
    if relative.strip().replace("\\", "/").rstrip("/") != expected:
        return None, relative
    path = safe_workspace_path(project_root, relative)
    if path is None:
        return None, relative
    return path, relative


def codebase_artifact_path_is_contained(artifact_dir: Path, path: Path) -> bool:
    """Reject symlinks and paths resolving outside one deposited artifact root."""

    if artifact_dir.is_symlink() or path.is_symlink():
        return False
    try:
        relative = path.relative_to(artifact_dir)
        resolved_root = artifact_dir.resolve()
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    current = artifact_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return resolved == resolved_root or resolved_root in resolved.parents


def codebase_artifact_root_is_contained(project_root: Path, artifact_dir: Path) -> bool:
    try:
        relative = artifact_dir.relative_to(project_root)
        resolved_project = project_root.resolve()
        resolved_artifact = artifact_dir.resolve()
    except (OSError, ValueError):
        return False
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return resolved_artifact == resolved_project or resolved_project in resolved_artifact.parents


def artifact_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def preferred_artifact_file(artifact_dir: Path, artifact_files: list[Path]) -> Path | None:
    indexed = {path.relative_to(artifact_dir).as_posix(): path for path in artifact_files}
    for filename in (*CODEBASE_JSON_ARTIFACTS, *CODEBASE_TEXT_ARTIFACTS):
        candidate = indexed.get(filename)
        if candidate is not None:
            return candidate
    return artifact_files[0] if artifact_files else None


def codebase_manifest_errors(
    manifest: Any,
    *,
    project_root: Path,
    source_id: str,
    artifact_dir: Path,
    artifact_files: list[Path],
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any] | None]:
    errors: list[str] = []
    checksums: list[dict[str, Any]] = []
    if not isinstance(manifest, dict):
        return ["artifact manifest must be a JSON object"], checksums, None
    if manifest.get("schema_version") != CODEBASE_ARTIFACT_MANIFEST_SCHEMA_VERSION:
        errors.append(
            f"artifact manifest schema_version must be {CODEBASE_ARTIFACT_MANIFEST_SCHEMA_VERSION!r}"
        )
    if manifest.get("artifact_kind") != "codebase_evidence":
        errors.append("artifact manifest artifact_kind must be 'codebase_evidence'")
    if manifest.get("source_id") != source_id:
        errors.append(f"artifact manifest source_id must match {source_id!r}")
    generated_at = manifest.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        errors.append("artifact manifest generated_at must be a non-empty string")
    producer = manifest.get("producer")
    if not isinstance(producer, dict) or not all(
        isinstance(producer.get(field), str) and producer[field].strip()
        for field in ("name", "version")
    ):
        errors.append("artifact manifest producer must name the external worker and version")

    invocation = manifest.get("invocation")
    if not isinstance(invocation, dict):
        errors.append("artifact manifest invocation must be an object")
    else:
        argv = invocation.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item.strip() for item in argv
        ):
            errors.append("artifact manifest invocation.argv must be a non-empty structured string list")
        if invocation.get("executed_by") != "external_worker":
            errors.append("artifact manifest invocation.executed_by must be 'external_worker'")
        for field in ("plugins_enabled", "hooks_enabled", "network_access"):
            if invocation.get(field) is not False:
                errors.append(f"artifact manifest invocation.{field} must be false")

    declared = manifest.get("files")
    declared_by_path: dict[str, dict[str, Any]] = {}
    if not isinstance(declared, list) or not declared:
        errors.append("artifact manifest files must be a non-empty list")
    else:
        if len(declared) > CODEBASE_MAX_ARTIFACT_FILES:
            errors.append(
                f"artifact manifest files exceed limit {CODEBASE_MAX_ARTIFACT_FILES}"
            )
        for index, item in enumerate(declared[: CODEBASE_MAX_ARTIFACT_FILES + 1]):
            if not isinstance(item, dict):
                errors.append(f"artifact manifest files[{index}] must be an object")
                continue
            relative_text = item.get("path")
            if not isinstance(relative_text, str) or not relative_text.strip():
                errors.append(f"artifact manifest files[{index}].path must be non-empty")
                continue
            normalized = relative_text.strip().replace("\\", "/")
            relative = PurePosixPath(normalized)
            if relative.is_absolute() or ".." in relative.parts or normalized == CODEBASE_ARTIFACT_MANIFEST:
                errors.append(f"artifact manifest files[{index}].path is unsafe: {relative_text!r}")
                continue
            if normalized in declared_by_path:
                errors.append(f"artifact manifest repeats file path {normalized!r}")
                continue
            declared_by_path[normalized] = item

    actual_by_path = {
        path.relative_to(artifact_dir).as_posix(): path
        for path in artifact_files
    }
    if set(declared_by_path) != set(actual_by_path):
        missing = sorted(set(actual_by_path) - set(declared_by_path))
        extra = sorted(set(declared_by_path) - set(actual_by_path))
        if missing:
            errors.append("artifact manifest omits deposited file(s): " + ", ".join(missing))
        if extra:
            errors.append("artifact manifest lists missing file(s): " + ", ".join(extra))
    for relative, path in sorted(actual_by_path.items()):
        item = declared_by_path.get(relative)
        if item is None:
            continue
        try:
            size_bytes = path.stat().st_size
            checksum = artifact_sha256(path)
        except OSError as exc:
            errors.append(f"cannot verify deposited artifact {relative}: {exc}")
            continue
        if item.get("size_bytes") != size_bytes:
            errors.append(f"artifact manifest size mismatch for {relative}")
        if item.get("sha256") != checksum:
            errors.append(f"artifact manifest checksum mismatch for {relative}")
        checksums.append(
            {
                "path": relative_label(project_root, path),
                "sha256": checksum,
                "size_bytes": size_bytes,
            }
        )

    provenance = None
    if isinstance(producer, dict) and isinstance(invocation, dict) and isinstance(generated_at, str):
        provenance = {
            "trust": "self_asserted_external_worker",
            "producer": {
                "name": producer.get("name"),
                "version": producer.get("version"),
            },
            "generated_at": generated_at,
            "invocation": {
                "argv": invocation.get("argv"),
                "executed_by": invocation.get("executed_by"),
                "plugins_enabled": invocation.get("plugins_enabled"),
                "hooks_enabled": invocation.get("hooks_enabled"),
                "network_access": invocation.get("network_access"),
            },
        }
    return errors, checksums, provenance


def inspect_codebase_artifact_bundle(
    project_root: Path,
    artifact_dir: Path | None,
    source_id: str,
) -> CodebaseArtifactIntake:
    if artifact_dir is None or not artifact_dir.exists():
        return CodebaseArtifactIntake("missing", [], None, None, [], None, [])
    artifact_label = relative_label(project_root, artifact_dir)
    if (
        not artifact_dir.is_dir()
        or artifact_dir.is_symlink()
        or not codebase_artifact_root_is_contained(project_root, artifact_dir)
    ):
        return CodebaseArtifactIntake(
            "invalid",
            [],
            None,
            None,
            [],
            None,
            [f"{artifact_label}: codebase artifact root must be a regular in-workspace directory"],
        )

    warnings: list[str] = []
    artifact_files: list[Path] = []
    manifest_path: Path | None = None
    total_bytes = 0
    seen_files = 0
    seen_entries = 0
    unsafe = False
    for path in artifact_dir.rglob("*"):
        seen_entries += 1
        if seen_entries > CODEBASE_MAX_ARTIFACT_ENTRIES:
            warnings.append(
                f"{artifact_label}: artifact entries exceed limit {CODEBASE_MAX_ARTIFACT_ENTRIES}"
            )
            unsafe = True
            break
        relative = path.relative_to(artifact_dir)
        relative_text = relative.as_posix()
        if any(part.startswith(".") for part in relative.parts):
            warnings.append(f"{artifact_label}/{relative_text}: hidden codebase artifact refused")
            unsafe = True
            continue
        if path.is_symlink() or (path.exists() and not codebase_artifact_path_is_contained(artifact_dir, path)):
            warnings.append(f"{artifact_label}/{relative_text}: symlinked or escaping codebase artifact refused")
            unsafe = True
            continue
        if not path.is_file():
            continue
        seen_files += 1
        if seen_files > CODEBASE_MAX_ARTIFACT_FILES:
            warnings.append(
                f"{artifact_label}: artifact file count exceeds limit {CODEBASE_MAX_ARTIFACT_FILES}"
            )
            unsafe = True
            break
        try:
            total_bytes += path.stat().st_size
        except OSError as exc:
            warnings.append(f"{artifact_label}/{relative_text}: cannot stat codebase artifact: {exc}")
            unsafe = True
            continue
        if total_bytes > CODEBASE_MAX_ARTIFACT_BYTES:
            warnings.append(
                f"{artifact_label}: artifact bytes exceed limit {CODEBASE_MAX_ARTIFACT_BYTES}"
            )
            unsafe = True
            break
        if relative_text == CODEBASE_ARTIFACT_MANIFEST:
            manifest_path = path
            continue
        if path.suffix.lower() not in CODEBASE_SUPPORTED_ARTIFACT_SUFFIXES:
            warnings.append(f"{artifact_label}/{relative_text}: executable or unsupported artifact refused")
            unsafe = True
            continue
        artifact_files.append(path)

    artifact_files.sort(key=lambda path: path.relative_to(artifact_dir).as_posix())
    artifact_paths = [relative_label(project_root, path) for path in artifact_files]
    checksums: list[dict[str, Any]] = []
    for path in artifact_files:
        try:
            checksums.append(
                {
                    "path": relative_label(project_root, path),
                    "sha256": artifact_sha256(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        except OSError as exc:
            warnings.append(f"{relative_label(project_root, path)}: cannot hash codebase artifact: {exc}")
            unsafe = True

    preferred = preferred_artifact_file(artifact_dir, artifact_files)
    if manifest_path is None:
        if unsafe:
            status = "invalid"
            preferred = None
        else:
            status = "legacy_unbound"
            warnings.append(
                f"{artifact_label}: artifact-manifest.json is missing; artifact remains legacy/unbound evidence"
            )
        return CodebaseArtifactIntake(status, artifact_paths, preferred, None, checksums, None, warnings)

    manifest_label = relative_label(project_root, manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"{manifest_label}: invalid codebase artifact manifest: {exc}")
        return CodebaseArtifactIntake("invalid", artifact_paths, None, manifest_label, checksums, None, warnings)
    errors, verified_checksums, provenance = codebase_manifest_errors(
        manifest,
        project_root=project_root,
        source_id=source_id,
        artifact_dir=artifact_dir,
        artifact_files=artifact_files,
    )
    warnings.extend(f"{manifest_label}: {error}" for error in errors)
    if unsafe or errors or preferred is None:
        return CodebaseArtifactIntake("invalid", artifact_paths, None, manifest_label, checksums, provenance, warnings)
    return CodebaseArtifactIntake(
        "validated",
        artifact_paths,
        preferred,
        manifest_label,
        verified_checksums,
        provenance,
        warnings,
    )


def json_summary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("summary", "description", "overview", "abstract"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return normalize_markdown_spacing(item)
    return None


def json_outline(value: Any) -> list[tuple[int, str]]:
    if isinstance(value, dict):
        keys = [key for key in value if isinstance(key, str) and key not in {"summary", "description", "overview"}]
        return [(2, key.replace("_", " ").title()) for key in keys[:20]]
    if isinstance(value, list):
        return [(2, "List Items")]
    return []


def json_warnings(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    warnings = value.get("warnings") or value.get("parse_warnings")
    if not isinstance(warnings, list):
        return []
    return [warning for warning in warnings if isinstance(warning, str) and warning.strip()]


def read_codebase_artifact(project_root: Path, artifact_path: Path) -> tuple[str, str | None, list[tuple[int, str]], list[str], list[str]]:
    relative_path = relative_label(project_root, artifact_path)
    try:
        text = artifact_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return "None extracted.", None, [], [], [f"{relative_path}: cannot read codebase artifact: {exc}"]
    if artifact_path.suffix.lower() == ".json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            return text or "None extracted.", None, [], extract_links(text), [f"{relative_path}: invalid JSON: {exc}"]
        rendered = json.dumps(value, indent=2, sort_keys=True)
        summary = json_summary(value) or "Structured codebase adapter artifact."
        return rendered, summary, json_outline(value), extract_links(rendered), json_warnings(value)
    normalized = normalize_markdown_spacing(text)
    summary = next((line.strip("# ") for line in normalized.splitlines() if line.strip()), None)
    return normalized or "None extracted.", summary, extract_pdf_outline(normalized), extract_links(normalized), []


def normalize_codebase_record(project_root: Path, config: dict[str, Any], record: dict[str, Any]) -> NormalizedSource:
    source_id = record_id(record)
    metadata = record.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        record["metadata"] = metadata
    warnings = manifest_warnings(record)
    provider = metadata.get("codebase_tool") if isinstance(metadata.get("codebase_tool"), str) else None
    if provider is None:
        provider = codebase_provider_from_config(config)
        if provider:
            metadata["codebase_tool"] = provider
    artifact_dir, artifact_dir_label = artifact_dir_for_record(project_root, config, record)
    expected_artifact_dir = f"{codebase_output_dir(config)}/{safe_source_id(source_id)}"
    if artifact_dir is None and artifact_dir_label != expected_artifact_dir:
        warnings.append(
            f"{source_id}: codebase_output_dir {artifact_dir_label!r} does not match reserved path "
            f"{expected_artifact_dir!r}; refusing artifact lookup"
        )
    intake = inspect_codebase_artifact_bundle(project_root, artifact_dir, source_id)
    metadata["codebase_artifact_paths"] = intake.artifact_paths
    metadata["codebase_intake_status"] = intake.status
    metadata["codebase_execution_scope"] = "external_worker_only"
    metadata["codebase_artifact_manifest"] = intake.manifest_path
    metadata["codebase_artifact_checksums"] = intake.checksums
    metadata["codebase_artifact_provenance"] = intake.provenance
    warnings.extend(intake.warnings)
    title = codebase_title(record)
    links = [record_url(record)] if record_url(record) else []

    raw_intake = metadata.get("codebase_intake")
    if isinstance(raw_intake, dict) and raw_intake.get("bounded") is False:
        warnings.append(
            f"{source_id}: raw code snapshot exceeds the inventory bound and cannot be normalized"
        )
        intake.preferred_artifact = None
        metadata["codebase_intake_status"] = "invalid"

    artifact = intake.preferred_artifact
    if artifact is None:
        if intake.status == "invalid" or metadata["codebase_intake_status"] == "invalid":
            warnings.append(
                f"{source_id}: deposited codebase artifact failed bounded nonexecution/provenance validation"
            )
        else:
            warnings.append(
                f"{source_id}: no codebase adapter artifact found at {artifact_dir_label}. "
                "Have a separately authorized external worker deposit an inert artifact and manifest there."
            )
        return NormalizedSource(
            record=record,
            extraction_method="codebase_stub",
            title=title,
            authors=[],
            abstract=(
                "Codebase architecture stub. Adapter output has not been recorded as a validated "
                "external-worker artifact."
            ),
            outline=[(2, "Codebase Evidence"), (2, "Adapter Artifact")],
            extracted_text=f"No validated codebase artifact found under `{artifact_dir_label}`.",
            media=[],
            links=[link for link in links if link],
            bibliography_files=[],
            included_paths=[],
            warnings=unique_values(warnings),
        )

    extracted_text, abstract, outline, artifact_links, artifact_warnings = read_codebase_artifact(project_root, artifact)
    warnings.extend(artifact_warnings)
    return NormalizedSource(
        record=record,
        extraction_method="codebase_context",
        title=title,
        authors=[],
        abstract=abstract or "Codebase architecture context extracted from a local adapter artifact.",
        outline=outline or [(2, "Codebase Adapter Artifact")],
        extracted_text=extracted_text,
        media=[],
        links=unique_values([link for link in [*links, *artifact_links] if link]),
        bibliography_files=[],
        included_paths=intake.artifact_paths,
        warnings=unique_values(warnings),
    )


def raw_paths(record: dict[str, Any], included_paths: list[str]) -> list[str]:
    paths: list[str] = []
    value = record.get("raw_paths")
    if isinstance(value, list):
        paths.extend(path for path in value if isinstance(path, str))
    for key in ("latex_root", "raw_pdf"):
        path = record.get(key)
        if isinstance(path, str):
            paths.append(path)
    paths.extend(included_paths)
    return unique_values(paths)


def status_for(source: NormalizedSource) -> str:
    if source.extraction_method in {"link_stub", "web_stub", "codebase_stub"}:
        return "stubbed"
    if source.extraction_method == "codebase_context":
        return "content_extracted" if source.extracted_text and source.extracted_text != "None extracted." else "partial"
    if not source.extracted_text or source.extracted_text == "None extracted.":
        # A scanned/image-only PDF still produced a usable (degraded) record
        # that an orchestrator can route to OCR; reserve `failed` for broken
        # extraction paths.
        return "partial" if source.needs_ocr else "failed"
    if source.extraction_method in {"pdf_text", "html_text", "table_text"}:
        return "content_extracted"
    if not source.abstract or any("include file not found" in warning for warning in source.warnings):
        return "partial"
    return "content_extracted"


def confidence_for(source: NormalizedSource) -> str:
    status = status_for(source)
    if source.needs_ocr:
        return "low"
    if source.extraction_method == "codebase_context":
        return "medium"
    if source.extraction_method == "codebase_stub":
        return "low"
    if source.extraction_method == "link_stub":
        return "high"
    if source.extraction_method == "web_stub":
        return "medium"
    if source.extraction_method == "table_text" and status == "content_extracted":
        return "high"
    if source.extraction_method in {"pdf_text", "html_text"} and status == "content_extracted":
        return "medium"
    if status == "content_extracted" and source.title and source.abstract and source.outline:
        return "high"
    if status in {"content_extracted", "partial"}:
        return "medium"
    return "low"


def arxiv_id_from_record(record: dict[str, Any]) -> str | None:
    provenance = record.get("provenance")
    if isinstance(provenance, dict) and isinstance(provenance.get("arxiv_id"), str):
        return provenance["arxiv_id"]
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("arxiv_id"), str):
        return metadata["arxiv_id"]
    source_id = record.get("id")
    if isinstance(source_id, str) and source_id.startswith("paper:"):
        value = source_id.removeprefix("paper:")
        if re.match(r"^\d{4}\.\d{4,5}v\d+$", value):
            return value
    raw_pdf = raw_pdf_value(record)
    if raw_pdf:
        stem = PurePosixPath(raw_pdf).stem
        if re.match(r"^\d{4}\.\d{4,5}v\d+$", stem):
            return stem
    return None


def academic_metadata_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return None
    field_map = {
        "academic_provider": "provider",
        "academic_source_type": "source_type",
        "venue": "venue",
        "publication_year": "publication_year",
        "oa_status": "oa_status",
        "peer_review_status": "peer_review_status",
        "arxiv_id": "arxiv_id",
        "openalex_work_id": "openalex_work_id",
        "doi": "doi",
    }
    academic: dict[str, Any] = {}
    for source_key, target_key in field_map.items():
        value = provenance.get(source_key)
        if isinstance(value, str) and value.strip():
            academic[target_key] = value.strip()
        elif source_key == "publication_year" and isinstance(value, int) and not isinstance(value, bool):
            academic[target_key] = value
    return academic or None


def standards_metadata_from_record(record: dict[str, Any]) -> dict[str, Any] | None:
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return None
    standards = provenance.get("standards")
    if not isinstance(standards, dict):
        return None
    return dict(standards)


def content_hash(source: NormalizedSource) -> str:
    material = "\n\n".join(
        [
            source.title or "",
            "\n".join(source.authors),
            source.abstract or "",
            source.extracted_text,
        ]
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def frontmatter_for(
    source: NormalizedSource,
    manifest_path: str,
    output_path: Path,
    date_text: str,
    manifest_records: list[dict[str, Any]] | None = None,
    project_root: Path | None = None,
    normalized_at: str | None = None,
) -> dict[str, Any]:
    record = source.record
    metadata = record_metadata(record)
    academic = academic_metadata_from_record(record)
    standards = standards_metadata_from_record(record)
    created = existing_created_date(output_path) or date_text
    normalized_at = normalized_at or timestamp_utc()
    url = record_url(record)
    venue = academic.get("venue") if academic else None
    doi = academic.get("doi") if academic else None
    openalex_id = academic.get("openalex_work_id") if academic else None
    arxiv_id = arxiv_id_from_record(record)
    unusable_reasons = record_unusable_evidence_reasons(record)
    frontmatter: dict[str, Any] = {
        "type": "normalized_source",
        "source_id": record.get("id"),
        "source_kind": record.get("kind"),
        "status": status_for(source),
        "evidence_usable": not unusable_reasons,
        "unusable_evidence_reasons": unusable_reasons or None,
        "created": created,
        "updated": date_text,
        "normalized_at": normalized_at,
        "raw_paths": raw_paths(record, source.included_paths),
        "manifest_path": manifest_path,
        "normalizer": {
            "name": NORMALIZER_NAME,
            "version": NORMALIZER_VERSION,
        },
        "parse_warnings": source.warnings,
        "title": source.title,
        "title_source": source.title_source,
        "extracted_title": source.extracted_title,
        "authors": source.authors,
        "venue": venue or ("arXiv" if arxiv_id else None),
        "date": None,
        "doi": doi,
        "openalex_id": openalex_id,
        "arxiv_id": arxiv_id,
        "url": url,
        "raw_pdf": raw_pdf_value(record),
        "latex_root": record.get("latex_root") if isinstance(record.get("latex_root"), str) else None,
        "entrypoint": record.get("entrypoint") if isinstance(record.get("entrypoint"), str) else None,
        "repo_full_name": repo_full_name(record),
        "codebase_repo": codebase_repo(record) if is_codebase_record(record) else None,
        "codebase_revision": metadata.get("codebase_revision") if isinstance(metadata.get("codebase_revision"), str) else None,
        "codebase_tool": metadata.get("codebase_tool") if isinstance(metadata.get("codebase_tool"), str) else None,
        "codebase_artifact_paths": metadata.get("codebase_artifact_paths") if isinstance(metadata.get("codebase_artifact_paths"), list) else None,
        "codebase_intake_status": metadata.get("codebase_intake_status")
        if isinstance(metadata.get("codebase_intake_status"), str)
        else None,
        "codebase_execution_scope": metadata.get("codebase_execution_scope")
        if isinstance(metadata.get("codebase_execution_scope"), str)
        else None,
        "codebase_artifact_manifest": metadata.get("codebase_artifact_manifest")
        if isinstance(metadata.get("codebase_artifact_manifest"), str)
        else None,
        "codebase_artifact_checksums": metadata.get("codebase_artifact_checksums")
        if isinstance(metadata.get("codebase_artifact_checksums"), list)
        else None,
        "codebase_artifact_provenance": metadata.get("codebase_artifact_provenance")
        if isinstance(metadata.get("codebase_artifact_provenance"), dict)
        else None,
        "provider": codebase_provider(record) if is_codebase_record(record) else link_provider(record) if source.extraction_method in {"link_stub", "web_stub"} else None,
        "fetch_status": "artifact_recorded" if source.extraction_method == "codebase_context" else "not_fetched" if source.extraction_method in {"link_stub", "web_stub", "codebase_stub"} else None,
        "extraction_method": source.extraction_method,
        "content_hash": content_hash(source),
        "raw_fingerprint": record_raw_fingerprint(record),
        "references_source_ids": matched_reference_source_ids(source, manifest_records, project_root) or None,
        "academic": academic,
        "standards": standards,
        "provenance": record.get("provenance") if isinstance(record.get("provenance"), dict) else None,
        "needs_ocr": True if source.needs_ocr else None,
        "language": "en",
        "confidence": confidence_for(source),
        "title_confidence": source.title_confidence if source.extraction_method == "pdf_text" else None,
        "abstract_confidence": source.abstract_confidence if source.extraction_method == "pdf_text" else None,
    }
    return frontmatter


def render_yaml(frontmatter: dict[str, Any]) -> str:
    return yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()


def render_citation_metadata(source: NormalizedSource) -> list[str]:
    record = source.record
    metadata = record_metadata(record)
    lines: list[str] = []
    repo = repo_full_name(record)
    codebase = codebase_repo(record) if is_codebase_record(record) else None
    url = record_url(record)
    if codebase and not repo:
        lines.append(f"- Codebase: {codebase}")
    if repo:
        lines.append(f"- Repository: {repo}")
    if url:
        lines.append(f"- URL: {url}")
    tool = metadata.get("codebase_tool")
    if isinstance(tool, str) and tool.strip():
        lines.append(f"- Codebase tool: {tool}")
    artifact_paths = metadata.get("codebase_artifact_paths")
    if isinstance(artifact_paths, list) and artifact_paths:
        lines.append("- Codebase artifacts: " + ", ".join(f"`{path}`" for path in artifact_paths if isinstance(path, str)))
    if source.title and not url:
        lines.append(f"- Title: {source.title}")
    if source.authors:
        lines.append(f"- Authors: {', '.join(source.authors)}")
    arxiv_id = arxiv_id_from_record(record)
    if arxiv_id:
        lines.append("- Venue: arXiv")
        lines.append(f"- arXiv ID: {arxiv_id}")
    raw_pdf = raw_pdf_value(record)
    if raw_pdf:
        lines.append(f"- Raw PDF: `{raw_pdf}`")
    return lines or ["None recorded."]


def render_outline(outline: list[tuple[int, str]]) -> list[str]:
    if not outline:
        return ["None extracted."]
    lines: list[str] = []
    for level, title in outline:
        indent = "  " * max(0, level - 2)
        lines.append(f"{indent}- {title}")
    return lines


def render_media(media: list[MediaReference]) -> list[str]:
    if not media:
        return ["- None recorded."]
    lines: list[str] = []
    for item in media:
        details: list[str] = []
        if item.label:
            details.append(f"label `{item.label}`")
        if item.graphics:
            details.append("graphics " + ", ".join(f"`{graphic}`" for graphic in item.graphics))
        suffix = f" ({'; '.join(details)})" if details else ""
        caption = item.caption or "No caption extracted."
        lines.append(f"- {item.kind.title()}: {caption}{suffix}")
    return lines


def render_links(source: NormalizedSource) -> list[str]:
    lines: list[str] = []
    lines.extend(f"- {link}" for link in source.links)
    if source.bibliography_files:
        lines.append("- Bibliography files: " + ", ".join(f"`{value}`" for value in source.bibliography_files))
    return lines or ["- None recorded."]


def render_raw_paths(paths: list[str]) -> list[str]:
    return [f"- `{path}`" for path in paths] if paths else ["- None recorded."]


def render_warnings(warnings: list[str]) -> list[str]:
    return [f"- {warning}" for warning in warnings] if warnings else ["- None recorded."]


def render_markdown(source: NormalizedSource, frontmatter: dict[str, Any]) -> str:
    title = source.title or str(source.record.get("id", "Untitled source"))
    paths = frontmatter.get("raw_paths")
    raw_path_lines = render_raw_paths(paths if isinstance(paths, list) else [])
    sections = [
        "---",
        render_yaml(frontmatter),
        "---",
        "",
        f"# {title}",
        "",
        "## Citation Metadata",
        "",
        *render_citation_metadata(source),
        "",
        "## Abstract",
        "",
        source.abstract or "None extracted.",
        "",
        "## Outline",
        "",
        *render_outline(source.outline),
        "",
        "## Extracted Text",
        "",
        source.extracted_text or "None extracted.",
        "",
        "## Figures and Tables",
        "",
        *render_media(source.media),
        "",
        "## Links",
        "",
        *render_links(source),
        "",
        "## Raw Source Paths",
        "",
        *raw_path_lines,
        "",
        "## Parse Warnings",
        "",
        *render_warnings(source.warnings),
        "",
    ]
    return "\n".join(sections)


def normalized_output_path(source: NormalizedSource, normalized_root: Path) -> Path:
    return normalized_output_path_for_record(source.record, normalized_root)


def write_normalized_source(
    source: NormalizedSource,
    normalized_root: Path,
    manifest_path: str,
    date_text: str,
    manifest_records: list[dict[str, Any]] | None = None,
    project_root: Path | None = None,
    force: bool = False,
    normalized_at: str | None = None,
) -> tuple[Path, str]:
    output_path = normalized_output_path(source, normalized_root)
    existed = output_path.exists()
    if existed and not force:
        return output_path, "skipped_existing"
    frontmatter = frontmatter_for(
        source,
        manifest_path,
        output_path,
        date_text,
        normalized_at=normalized_at,
        manifest_records=manifest_records,
        project_root=project_root,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.tmp")
    temporary_path.write_text(render_markdown(source, frontmatter))
    temporary_path.replace(output_path)
    return output_path, "updated" if existed else "created"


def method_count_key(method: str) -> str:
    if method == "link":
        return "links"
    if method == "table":
        return "tables"
    return method


def empty_summary(
    selected_count: int,
    skipped_unsupported: int,
    selector: str,
) -> dict[str, int | str]:
    return {
        "selector": selector,
        "selected": selected_count,
        "planned": 0,
        "created": 0,
        "updated": 0,
        "dry_run": 0,
        "would_create": 0,
        "would_update": 0,
        "skipped_existing": 0,
        "skipped_unsupported": skipped_unsupported,
        "stale": 0,
        "partial": 0,
        "failed": 0,
        "latex": 0,
        "pdf": 0,
        "links": 0,
        "html": 0,
        "tables": 0,
        "codebase": 0,
    }


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


def append_normalization_log(project_root: Path, data: dict[str, Any]) -> None:
    append_log_entry(project_root / "log.md", render_normalization_log_entry(data))


def render_normalization_log_entry(data: dict[str, Any]) -> str:
    date_text = str(data["timestamp"]).split("T", 1)[0]
    summary = data["summary"]
    return (
        f"## [{date_text}] normalize | Source normalization run\n\n"
        f"- Manifest: `{data['manifest']}`\n"
        f"- Selector: `{summary['selector']}`\n"
        f"- Selected: {summary['selected']}\n"
        f"- Planned: {summary['planned']}\n"
        "- Results: "
        f"created={summary['created']} "
        f"updated={summary['updated']} "
        f"dry_run={summary['dry_run']} "
        f"skipped_existing={summary['skipped_existing']} "
        f"stale={summary['stale']} "
        f"failed={summary['failed']}\n"
        "- Methods: "
        f"latex={summary['latex']} "
        f"pdf={summary['pdf']} "
        f"links={summary['links']} "
        f"html={summary['html']} "
        f"tables={summary['tables']} "
        f"codebase={summary['codebase']}\n"
    )


def print_summary(summary: dict[str, int | str]) -> None:
    print(
        "summary "
        f"selector={summary['selector']} "
        f"selected={summary['selected']} "
        f"planned={summary['planned']} "
        f"created={summary['created']} "
        f"updated={summary['updated']} "
        f"dry_run={summary['dry_run']} "
        f"would_create={summary['would_create']} "
        f"would_update={summary['would_update']} "
        f"skipped_existing={summary['skipped_existing']} "
        f"skipped_unsupported={summary['skipped_unsupported']} "
        f"stale={summary['stale']} "
        f"latex={summary['latex']} "
        f"pdf={summary['pdf']} "
        f"links={summary['links']} "
        f"html={summary['html']} "
        f"tables={summary['tables']} "
        f"codebase={summary['codebase']} "
        f"partial={summary['partial']} "
        f"failed={summary['failed']}",
        file=sys.stderr,
    )


def normalization_report_summary(summary: dict[str, int | str]) -> dict[str, Any]:
    method_keys = ("latex", "pdf", "links", "html", "tables", "codebase")
    skipped_existing = int(summary["skipped_existing"])
    skipped_unsupported = int(summary["skipped_unsupported"])
    return {
        "selected": int(summary["selected"]),
        "planned": int(summary["planned"]),
        "created": int(summary["created"]),
        "updated": int(summary["updated"]),
        "skipped": skipped_existing + skipped_unsupported,
        "skipped_existing": skipped_existing,
        "skipped_unsupported": skipped_unsupported,
        "stale": int(summary["stale"]),
        "partial": int(summary["partial"]),
        "failed": int(summary["failed"]),
        "dry_run": int(summary["dry_run"]),
        "would_create": int(summary["would_create"]),
        "would_update": int(summary["would_update"]),
        "methods": {key: int(summary[key]) for key in method_keys},
    }


def build_normalization_report(
    *,
    timestamp: str,
    manifest_path: str,
    normalized_dir: str,
    dry_run: bool,
    force: bool,
    selector: str,
    summary: dict[str, int | str],
    warnings: list[str],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": NORMALIZATION_REPORT_SCHEMA_VERSION,
        "document_type": NORMALIZATION_REPORT_DOCUMENT_TYPE,
        "timestamp": timestamp,
        "manifest": manifest_path,
        "normalized_dir": normalized_dir,
        "dry_run": dry_run,
        "force": force,
        "selector": selector,
        "summary": normalization_report_summary(summary),
        "warnings": unique_values(warnings),
        "actions": actions,
    }


def relative_output_path(project_root: Path, output_path: Path) -> str:
    return output_path.relative_to(project_root).as_posix()


def run_normalization(args: argparse.Namespace) -> int:
    json_output = args.format == "json"
    project_root = Path(args.project_root).resolve()
    config = load_config(project_root)
    manifest_path_text, normalized_dir_text = source_paths(config)
    manifest_path = project_root / manifest_path_text
    normalized_root = project_root / normalized_dir_text
    records = load_manifest(manifest_path)
    date_text = today_utc()
    run_timestamp = timestamp_utc()
    normalized_at = run_timestamp
    eligible = eligible_records(project_root, records)
    selected, skipped_unsupported, selector = select_eligible_records(args, records, eligible, normalized_root)
    summary = empty_summary(len(selected), skipped_unsupported, selector)
    actions: list[dict[str, Any]] = []
    report_warnings: list[str] = []

    # Per-source observability for unsupported records: without this, a source the
    # normalizer cannot process (for example, a bare Markdown file) is dropped
    # silently and the manifest looks healthy while the evidence never reaches
    # retrieval. Name each skipped source and its kind so callers can re-deliver it
    # in a supported format.
    eligible_ids = set(eligible_by_source_id(eligible))
    for record in records:
        source_id = record_id(record)
        if source_id in eligible_ids:
            continue
        kind = record.get("kind") or "unknown"
        report_warnings.append(
            f"{source_id}: kind '{kind}' is not a supported normalization input; "
            "skipped (no content extracted)"
        )
        if not json_output:
            print(
                f"skipped unsupported source_id={source_id} kind={kind}",
                file=sys.stderr,
            )

    actionable: list[tuple[EligibleRecord, Path, bool, bool]] = []
    for item in selected:
        summary[method_count_key(item.method)] += 1
        output_path = normalized_output_path_for_record(item.record, normalized_root)
        existed = output_path.exists()
        stale = existed and is_stale(item.record, output_path)
        if existed and not args.force and not stale:
            summary["skipped_existing"] += 1
            output_text = relative_output_path(project_root, output_path)
            actions.append(
                {
                    "source_id": record_id(item.record),
                    "method": item.method,
                    "action": "skipped_existing",
                    "output": output_text,
                    "status": "skipped_existing",
                    "stale": False,
                    "warnings": [],
                }
            )
            if not json_output:
                print(
                    "skipped existing "
                    f"{output_text} "
                    f"source_id={record_id(item.record)} method={item.method}",
                    file=sys.stderr,
                )
            continue
        if stale:
            summary["stale"] += 1
        actionable.append((item, output_path, existed, stale))
        summary["planned"] += 1

    if args.dry_run:
        for item, output_path, existed, stale in actionable:
            action = "would_update" if existed else "would_create"
            text_action = action.replace("_", " ")
            reason = " (stale: raw changed)" if stale else ""
            summary["dry_run"] += 1
            if existed:
                summary["would_update"] += 1
            else:
                summary["would_create"] += 1
            output_text = relative_output_path(project_root, output_path)
            actions.append(
                {
                    "source_id": record_id(item.record),
                    "method": item.method,
                    "action": action,
                    "output": output_text,
                    "status": "planned",
                    "stale": stale,
                    "warnings": [],
                }
            )
            if not json_output:
                print(
                    f"{text_action}{reason} {output_text} "
                    f"source_id={record_id(item.record)} method={item.method}",
                    file=sys.stderr,
                )
        if args.append_log:
            warning = "--append-log skipped during --dry-run"
            if json_output:
                report_warnings.append(warning)
            else:
                print(f"warning: {warning}", file=sys.stderr)
        if json_output:
            print(
                json.dumps(
                    build_normalization_report(
                        timestamp=run_timestamp,
                        manifest_path=manifest_path_text,
                        normalized_dir=normalized_dir_text,
                        dry_run=True,
                        force=args.force,
                        selector=selector,
                        summary=summary,
                        warnings=report_warnings,
                        actions=actions,
                    ),
                    indent=2,
                    sort_keys=False,
                )
            )
        else:
            print_summary(summary)
        return 0

    pdftotext_path = shutil.which("pdftotext") if any(item.method == "pdf" for item, _, _, _ in actionable) else None
    if any(item.method == "pdf" for item, _, _, _ in actionable) and not pdftotext_path:
        raise SystemExit(
            "PDF text extraction requires `pdftotext` from Poppler. "
            "Install Poppler or poppler-utils, then rerun normalize_sources.py."
        )

    for item, output_path, _existed, stale in actionable:
        try:
            source = normalize_selected_record(project_root, config, item, pdftotext_path)
            # The selection loop already decided to (re)write this record, so force the write.
            output_path, write_action = write_normalized_source(
                source,
                normalized_root,
                manifest_path_text,
                date_text,
                normalized_at=normalized_at,
                manifest_records=records,
                project_root=project_root,
                force=True,
            )
        except Exception as exc:
            summary["failed"] += 1
            output_text = relative_output_path(project_root, output_path)
            actions.append(
                {
                    "source_id": record_id(item.record),
                    "method": item.method,
                    "action": "failed",
                    "output": output_text,
                    "status": "failed",
                    "stale": stale,
                    "warnings": [],
                    "error": str(exc),
                }
            )
            if not json_output:
                print(
                    "failed "
                    f"{output_text} "
                    f"source_id={record_id(item.record)} method={item.method} error={exc}",
                    file=sys.stderr,
                )
            continue

        summary[write_action] += 1
        status = status_for(source)
        if status == "partial":
            summary["partial"] += 1
        elif status == "failed":
            summary["failed"] += 1
        reason = " (stale: raw changed)" if stale else ""
        output_text = relative_output_path(project_root, output_path)
        report_warnings.extend(source.warnings)
        actions.append(
            {
                "source_id": record_id(item.record),
                "method": item.method,
                "action": write_action,
                "output": output_text,
                "status": status,
                "stale": stale,
                "warnings": source.warnings,
            }
        )
        if not json_output:
            print(
                f"{write_action}{reason} {output_text} "
                f"source_id={record_id(item.record)} method={item.method} status={status}",
                file=sys.stderr,
            )
            for warning in source.warnings:
                print(f"warning: {warning}", file=sys.stderr)

    if args.append_log:
        append_normalization_log(
            project_root,
            {
                "timestamp": run_timestamp,
                "manifest": manifest_path_text,
                "summary": summary,
            },
        )

    if json_output:
        print(
            json.dumps(
                build_normalization_report(
                    timestamp=run_timestamp,
                    manifest_path=manifest_path_text,
                    normalized_dir=normalized_dir_text,
                    dry_run=False,
                    force=args.force,
                    selector=selector,
                    summary=summary,
                    warnings=report_warnings,
                    actions=actions,
                ),
                indent=2,
                sort_keys=False,
            )
        )
    else:
        print_summary(summary)
    return 1 if summary["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = args.format == "json"
    try:
        return run_normalization(args)
    except LockUnavailableError as exc:
        emit_error(str(exc), json_mode=json_mode, error_code=exc.error_code, details=exc.details)
        return 2
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=2)


if __name__ == "__main__":
    raise SystemExit(main())
