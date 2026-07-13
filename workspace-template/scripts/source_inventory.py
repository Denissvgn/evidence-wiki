#!/usr/bin/env python3
"""Inventory raw source assets into a deterministic JSONL manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc


MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown"}
PDF_EXTENSIONS = {".pdf"}
LATEX_EXTENSIONS = {".tex", ".sty", ".cls"}
BIBTEX_EXTENSIONS = {".bib", ".bbl", ".bst"}
HTML_EXTENSIONS = {".html", ".htm", ".xhtml"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".tif", ".tiff", ".bmp", ".eps"}
TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".feather", ".jsonl"}
TABLE_TEXT_EXTENSIONS = {".csv", ".tsv"}
LINK_EXTENSIONS = {".url", ".webloc"}
ARCHIVE_SUFFIXES = {
    (".zip",),
    (".tar",),
    (".gz",),
    (".tgz",),
    (".bz2",),
    (".xz",),
    (".7z",),
    (".rar",),
    (".tar", ".gz"),
    (".tar", ".bz2"),
    (".tar", ".xz"),
}
URL_RE = re.compile(r"^https?://\S+$")
URL_EXTRACT_RE = re.compile(r"https?://[^\s<>'\"]+")
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}v\d+$", re.IGNORECASE)
ARXIV_BUNDLE_RE = re.compile(r"^arxiv-(?P<arxiv_id>\d{4}\.\d{4,5}v\d+)$", re.IGNORECASE)
DOCUMENTCLASS_RE = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{")
FALLBACK_ENTRYPOINTS = ("main.tex", "main_arxiv.tex", "arxiv.tex", "example_paper.tex")
URL_TRAILING_PUNCTUATION = ".,;:)]}"
PROVENANCE_SIDECAR_SUFFIX = ".provenance.yml"
PROVENANCE_FIELDS = (
    "url",
    "final_url",
    "origin_url",
    "downloaded_pdf_url",
    "downloaded_archive_url",
    "repository_owner",
    "repository_name",
    "repository_full_name",
    "repository_artifact_kind",
    "repository_ref",
    "commit_sha",
    "academic_provider",
    "academic_source_type",
    "venue",
    "publication_year",
    "oa_status",
    "peer_review_status",
    "title",
    "authors",
    "published",
    "arxiv_id",
    "openalex_work_id",
    "openalex_publication_year",
    "openalex_title_lag",
    "openalex_identity_conflict",
    "openalex_reported_title",
    "openalex_reported_authors",
    "openalex_reported_publication_year",
    "openalex_identity_evidence",
    "doi_resolution",
    "doi",
    "doi_source",
    "openalex_enrichment_status",
    "openalex_enrichment_error",
    "provider_license_slug",
    "license_source",
    "license",
    "retrieved_at",
    "retrieved_by",
    "source_type",
    "jurisdiction",
    "publisher",
    "date_metadata",
    "standards",
    "evidence_usability_override",
    "supported_evidence_areas",
    "byte_count",
    "content_type",
    "http_status",
    "redirect_chain",
    "tls_verified",
    "tls_verification_note",
    "curation_notes",
    "effective_date",
    "publication_date",
    "validity_period",
    "date_not_available",
    "source_status",
    "delivery_failure_code",
    "delivery_failure_detail",
    "delivery_failure_remediation",
    "sha256",
    "checksum",
    "request_id",
    "candidate_id",
    "acquisition_run_id",
    "terms_url",
    "terms_note",
    "notes",
)
PROVENANCE_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
INVENTORY_CHECKSUM_REQUIRED = "INVENTORY_CHECKSUM_REQUIRED"
INVENTORY_CHECKSUM_MISMATCH = "INVENTORY_CHECKSUM_MISMATCH"
EXIT_STRICT_REFUSAL = 1
SPDX_LICENSE_IDS = {
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC-BY-4.0",
    "CC-BY-NC-4.0",
    "CC-BY-NC-ND-4.0",
    "CC-BY-NC-SA-4.0",
    "CC-BY-ND-4.0",
    "CC-BY-SA-4.0",
    "CC0-1.0",
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-3.0-only",
    "GPL-3.0-or-later",
    "ISC",
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
    "MIT",
    "MPL-2.0",
    "Unlicense",
}
CODEBASE_ANALYSIS_DEFAULT_OUTPUT_DIR = "sources/code_wikis"
CODEBASE_LOCAL_REPO_MARKERS = (".git", ".agent-wiki", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")
CODEBASE_DEFAULT_SOURCE_ROOT_NAMES = {"code", "repos", "repositories"}
CODEBASE_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
CODEBASE_MAX_LOCAL_REPO_FILES = 10_000
INVENTORY_REPORT_SCHEMA_VERSION = "1.0"
INVENTORY_REPORT_DOCUMENT_TYPE = "source_inventory_report"
ACQUISITION_INCOMPLETE_SUFFIX = ".acquisition-incomplete.json"
ACQUISITION_LOCK_RELATIVE = ("raw", ".locks", "acquisition.lock")

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError, workspace_lock
from source_failure_taxonomy import (
    DELIVERY_FAILURE_CODES,
    SOURCE_STATUS_VALUES,
)
from source_failure_taxonomy import (
    unusable_evidence_reasons as delivery_unusable_evidence_reasons,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a source manifest from configured raw roots.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print manifest JSONL to stdout without writing sources/manifest.jsonl.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print an inventory report to stdout.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for --report. Defaults to text.",
    )
    parser.add_argument(
        "--append-log",
        action="store_true",
        help="Append an inventory report summary to log.md. Requires --report.",
    )
    parser.add_argument(
        "--require-checksum",
        action="store_true",
        help="Strict mode: refuse records that do not have provenance.checksum_verified=true.",
    )
    parser.add_argument(
        "--reject-mismatch",
        action="store_true",
        help="Strict mode: refuse records whose provenance checksum is present but not verified.",
    )
    return parser.parse_args(argv)


def text_format_explicitly_requested(argv: list[str] | None) -> bool:
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg == "--format" and index + 1 < len(args) and args[index + 1] == "text":
            return True
        if arg == "--format=text":
            return True
    return False


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


def existing_detected_at(manifest_path: Path) -> dict[str, str]:
    detected: dict[str, str] = {}
    if not manifest_path.exists():
        return detected
    for line_number, line in enumerate(manifest_path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL in {manifest_path}:{line_number}: {exc}") from exc
        source_id = record.get("id")
        detected_at = record.get("detected_at")
        if isinstance(source_id, str) and isinstance(detected_at, str):
            detected[source_id] = detected_at
    return detected


def should_skip(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


def is_archive(path: Path) -> bool:
    suffixes = tuple(s.lower() for s in path.suffixes)
    return any(suffixes[-len(pattern) :] == pattern for pattern in ARCHIVE_SUFFIXES if len(suffixes) >= len(pattern))


def looks_like_link_file(path: Path, raw_root: Path) -> bool:
    if path.suffix.lower() in LINK_EXTENSIONS:
        return True
    if raw_root.name == "links" and path.suffix.lower() == ".txt":
        return True
    if path.suffix.lower() != ".txt":
        return False
    try:
        sample = path.read_text(errors="ignore")[:8192].splitlines()
    except OSError:
        return False
    non_empty = [line.strip() for line in sample if line.strip()]
    return bool(non_empty) and all(URL_RE.match(line) for line in non_empty)


def classify(path: Path, raw_root: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MARKDOWN_EXTENSIONS:
        return "markdown"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in LATEX_EXTENSIONS:
        return "latex"
    if suffix in BIBTEX_EXTENSIONS:
        return "bibtex"
    if suffix in HTML_EXTENSIONS:
        return "html"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in TABLE_EXTENSIONS:
        return "table"
    if looks_like_link_file(path, raw_root):
        return "link"
    if is_archive(path):
        return "code_archive"
    return "unknown"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80].strip("-") or "source"


def stable_id(relative_path: str) -> str:
    base = slugify(str(Path(relative_path).with_suffix("")))
    digest = hashlib.sha1(relative_path.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"raw:{base}-{digest}"


def arxiv_id_from_bundle_name(name: str) -> str | None:
    match = ARXIV_BUNDLE_RE.match(name)
    if not match:
        return None
    return match.group("arxiv_id")


def stable_paper_id(relative_path: str) -> str:
    base = slugify(PurePosixPath(relative_path).name or relative_path)
    digest = hashlib.sha1(relative_path.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"paper:{base}-{digest}"


def stable_link_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    github = github_repo_metadata(url)
    if github:
        base = f"github-{slugify(github['owner'])}-{slugify(github['repo'])}"
    else:
        path_slug = slugify(parsed.path.strip("/") or "home")
        base = "-".join(part for part in [slugify(host), path_slug] if part)
    digest = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"link:{base}-{digest}"


def stable_codebase_id(seed: str, label: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"codebase:{slugify(label)}-{digest}"


def safe_source_id(source_id: str) -> str:
    value = source_id.lower().replace(":", "__colon__")
    value = re.sub(r"[/\s]+", "-", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    value = value.replace("__colon__", "--")
    value = value.replace("-.", ".").strip("-")
    return value or "source"


def codebase_analysis_config(config: dict[str, Any]) -> dict[str, Any]:
    integrations = config.get("integrations") or {}
    if not isinstance(integrations, dict):
        return {}
    codebase = integrations.get("codebase_analysis") or {}
    return codebase if isinstance(codebase, dict) else {}


def codebase_analysis_enabled(config: dict[str, Any]) -> bool:
    return codebase_analysis_config(config).get("enabled") is True


def codebase_output_dir(config: dict[str, Any]) -> str:
    value = codebase_analysis_config(config).get("output_dir")
    if isinstance(value, str) and value.strip():
        return validate_generated_sources_path(value, "integrations.codebase_analysis.output_dir")
    return CODEBASE_ANALYSIS_DEFAULT_OUTPUT_DIR


def codebase_artifact_dir(config: dict[str, Any], source_id: str) -> str:
    return f"{codebase_output_dir(config)}/{safe_source_id(source_id)}"


def unique_values(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def normalize_entrypoint_candidate(filename: Any) -> str | None:
    if not isinstance(filename, str):
        return None
    value = filename.strip().replace("\\", "/")
    if not value or not value.lower().endswith(".tex"):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def read_bundle_readme(project_root: Path, bundle_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    readme_path = bundle_dir / "00README.json"
    if not readme_path.exists():
        return None, []
    readme_rel = readme_path.relative_to(project_root).as_posix()
    try:
        readme = json.loads(readme_path.read_text())
    except json.JSONDecodeError as exc:
        return None, [f"{readme_rel}: invalid JSON: {exc}"]
    except OSError as exc:
        return None, [f"{readme_rel}: cannot read: {exc}"]
    if not isinstance(readme, dict):
        return None, [f"{readme_rel}: expected JSON object"]
    return readme, []


def readme_entrypoint_candidates(readme: dict[str, Any]) -> list[str]:
    sources = readme.get("sources")
    if not isinstance(sources, list):
        return []

    candidates: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        usage = source.get("usage")
        if not isinstance(usage, str) or usage.lower() != "toplevel":
            continue
        candidate = normalize_entrypoint_candidate(source.get("filename"))
        if candidate:
            candidates.append(candidate)
    return candidates


def fallback_entrypoint(bundle_dir: Path) -> tuple[str | None, str | None]:
    for filename in FALLBACK_ENTRYPOINTS:
        if (bundle_dir / filename).is_file():
            return filename, "fallback_name"

    documentclass_candidates: list[str] = []
    for path in sorted(bundle_dir.glob("*.tex"), key=lambda value: value.name):
        try:
            sample = path.read_text(errors="ignore")[:20000]
        except OSError:
            continue
        if DOCUMENTCLASS_RE.search(sample):
            documentclass_candidates.append(path.name)

    if documentclass_candidates:
        return documentclass_candidates[0], "fallback_documentclass"
    return None, None


def select_entrypoint(
    project_root: Path,
    bundle_dir: Path,
    readme: dict[str, Any] | None,
    readme_warnings: list[str],
) -> tuple[str | None, str | None, list[str], list[str]]:
    warnings = list(readme_warnings)
    bundle_rel = bundle_dir.relative_to(project_root).as_posix()
    candidates = readme_entrypoint_candidates(readme) if readme else []

    for candidate in candidates:
        if (bundle_dir / candidate).is_file():
            return candidate, "readme", candidates, warnings
        warnings.append(f"{bundle_rel}: README entrypoint does not exist: {candidate}")

    if readme is not None and not candidates:
        warnings.append(f"{bundle_rel}: README has no toplevel .tex source")

    entrypoint, source = fallback_entrypoint(bundle_dir)
    if entrypoint:
        return entrypoint, source, candidates, warnings
    return None, None, candidates, warnings


def bundle_file_count(project_root: Path, bundle_dir: Path) -> int:
    return sum(
        1
        for path in bundle_dir.rglob("*")
        if path.is_file() and not should_skip(path.relative_to(project_root))
    )


def readme_string(readme: dict[str, Any] | None, key: str) -> str | None:
    if not readme:
        return None
    value = readme.get(key)
    return value if isinstance(value, str) else None


def readme_process_string(readme: dict[str, Any] | None, key: str) -> str | None:
    if not readme:
        return None
    process = readme.get("process")
    if not isinstance(process, dict):
        return None
    value = process.get(key)
    return value if isinstance(value, str) else None


def build_bundle_record(
    project_root: Path,
    bundle_dir: Path,
    default_status: str,
    previous_detected_at: dict[str, str],
    detected_at: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    bundle_rel = bundle_dir.relative_to(project_root).as_posix()
    readme, readme_warnings = read_bundle_readme(project_root, bundle_dir)
    entrypoint, entrypoint_source, candidates, warnings = select_entrypoint(
        project_root,
        bundle_dir,
        readme,
        readme_warnings,
    )
    if not entrypoint:
        return None, warnings

    arxiv_id = arxiv_id_from_bundle_name(bundle_dir.name)
    source_id = f"paper:{arxiv_id}" if arxiv_id else stable_paper_id(bundle_rel)
    metadata: dict[str, Any] = {
        "bundle_type": "arxiv" if arxiv_id else "latex_bundle",
        "entrypoint_source": entrypoint_source,
        "file_count": bundle_file_count(project_root, bundle_dir),
    }
    readme_path = bundle_dir / "00README.json"
    if readme_path.exists():
        metadata["readme_path"] = readme_path.relative_to(project_root).as_posix()
    if candidates:
        metadata["entrypoint_candidates"] = candidates
    if arxiv_id:
        metadata["arxiv_id"] = arxiv_id
    texlive_version = readme_string(readme, "texlive_version")
    if texlive_version:
        metadata["texlive_version"] = texlive_version
    if warnings:
        metadata["warnings"] = warnings

    record: dict[str, Any] = {
        "id": source_id,
        "kind": "paper",
        "raw_paths": [bundle_rel],
        "status": default_status,
        "detected_at": previous_detected_at.get(source_id, detected_at),
        "latex_root": bundle_rel,
        "entrypoint": entrypoint,
        "metadata": metadata,
    }
    compiler = readme_process_string(readme, "compiler")
    if compiler:
        record["compiler"] = compiler
    return record, warnings


def is_bundle_candidate(path: Path) -> bool:
    return arxiv_id_from_bundle_name(path.name) is not None or (path / "00README.json").is_file()


def iter_bundle_candidates(
    project_root: Path,
    source_roots: list[str],
    warnings: list[str] | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    project_root_resolved = project_root.resolve()
    for raw_root_text in source_roots:
        raw_root = project_root / raw_root_text
        if not raw_root.is_dir():
            continue
        for path in (raw_root, *raw_root.rglob("*")):
            if path.is_dir() and not is_contained_nonsymlink(path, project_root_resolved):
                if warnings is not None:
                    relative = path.relative_to(project_root).as_posix()
                    warnings.append(f"refusing symlinked bundle candidate: {relative}")
                continue
            if (
                path.is_dir()
                and not should_skip(path.relative_to(project_root))
                and is_bundle_candidate(path)
            ):
                candidates.append(path)
    candidates.sort(key=lambda path: path.relative_to(project_root).as_posix())
    return candidates


def is_inside_any(path: Path, parents: list[Path]) -> bool:
    return any(parent == path or parent in path.parents for parent in parents)


def acquisition_workspace_lock_path(project_root: Path) -> Path:
    return project_root.joinpath(*ACQUISITION_LOCK_RELATIVE)


def incomplete_acquisition_targets(project_root: Path, source_roots: list[str]) -> tuple[list[Path], list[str]]:
    """Return marker-backed payload roots that must remain invisible to inventory."""
    targets: list[Path] = []
    warnings: list[str] = []
    project_root_resolved = project_root.resolve()
    for raw_root_text in source_roots:
        raw_root = project_root / raw_root_text
        if not raw_root.is_dir():
            continue
        for marker in sorted(raw_root.rglob(f".*{ACQUISITION_INCOMPLETE_SUFFIX}")):
            if not is_contained_nonsymlink(marker, project_root_resolved) or not marker.is_file():
                continue
            target_name = marker.name[1 : -len(ACQUISITION_INCOMPLETE_SUFFIX)]
            if not target_name:
                continue
            target = marker.with_name(target_name)
            if target not in targets:
                targets.append(target)
                warnings.append(
                    "refusing marker-backed incomplete acquisition payload: "
                    f"{target.relative_to(project_root).as_posix()}"
                )
    return targets, warnings


def is_contained_nonsymlink(path: Path, root_resolved: Path) -> bool:
    """Return True iff ``path`` is a safe in-workspace entry.

    "Safe" means the entry is *not* a symlink and its real path stays inside the
    workspace. This is the single definition of filesystem containment shared by
    the source readers (``iter_raw_files`` / ``iter_local_code_repos``, security
    review SEC-E1-T01/T02) and the init/upgrade copy paths (SEC-E1-T04), so the
    two can never drift. ``raw/`` is the untrusted-input boundary the research
    wiki rests on: a symlink (pointing anywhere, inside or outside the workspace)
    is refused outright so its target bytes are never read, and the ``resolve()``
    containment check is the belt-and-suspenders guard for older Pythons whose
    ``rglob`` descends through a symlinked ancestor directory.

    Pure: the only filesystem access is ``is_symlink()`` / ``resolve()``. Pass an
    already-resolved ``root_resolved`` (hoisted out of the loop) so resolution is
    not repeated per entry and a workspace under a symlinked prefix — e.g. macOS
    ``/tmp`` -> ``/private/tmp`` — does not false-refuse.
    """
    if path.is_symlink():
        return False
    return path.resolve().is_relative_to(root_resolved)


def raw_root_for_path(project_root: Path, path: Path, source_roots: list[str]) -> Path:
    for raw_root_text in source_roots:
        raw_root = project_root / raw_root_text
        if path == raw_root or raw_root in path.parents:
            return raw_root
    return path.parent


def clean_url(value: str) -> str | None:
    candidate = value.strip().rstrip(URL_TRAILING_PUNCTUATION).strip("<>").rstrip(URL_TRAILING_PUNCTUATION)
    if URL_RE.match(candidate):
        return candidate
    return None


def extracted_urls(value: str) -> list[str]:
    urls: list[str] = []
    for match in URL_EXTRACT_RE.findall(value):
        url = clean_url(match)
        if url:
            urls.append(url)
    return urls


def is_link_parse_candidate(path: Path, raw_root: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in LINK_EXTENSIONS or (suffix == ".txt" and raw_root.name == "links")


def parse_text_link_file(lines: list[str], relative_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    occurrences: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        url = clean_url(stripped)
        if url:
            occurrences.append({"url": url, "raw_line": line_number})
            continue
        urls = extracted_urls(stripped)
        if urls:
            occurrences.extend({"url": extracted_url, "raw_line": line_number} for extracted_url in urls)
            continue
        warnings.append(f"{relative_path}:{line_number}: expected HTTP(S) URL")
    return occurrences, warnings


def parse_url_file(lines: list[str], relative_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    occurrences: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.upper().startswith("URL="):
            url = clean_url(stripped[4:])
            if url:
                occurrences.append({"url": url, "raw_line": line_number})
            else:
                warnings.append(f"{relative_path}:{line_number}: invalid URL value")

    if occurrences:
        return occurrences, warnings

    for line_number, line in enumerate(lines, start=1):
        for url in extracted_urls(line):
            occurrences.append({"url": url, "raw_line": line_number})
    return occurrences, warnings


def parse_webloc_file(text: str) -> list[dict[str, Any]]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        urls = extracted_urls(line)
        if urls:
            return [{"url": urls[0], "raw_line": line_number}]
    return []


def parse_link_file(path: Path, relative_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        text = path.read_text(errors="ignore")
    except OSError as exc:
        return [], [f"{relative_path}: cannot read link file: {exc}"]

    suffix = path.suffix.lower()
    if suffix == ".url":
        occurrences, warnings = parse_url_file(text.splitlines(), relative_path)
    elif suffix == ".webloc":
        occurrences = parse_webloc_file(text)
        warnings = []
    else:
        occurrences, warnings = parse_text_link_file(text.splitlines(), relative_path)

    if not occurrences:
        warnings.append(f"{relative_path}: no valid HTTP(S) URLs found")
    return occurrences, warnings


def github_repo_metadata(url: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner = parts[0]
    repo = parts[1].removesuffix(".git")
    if not owner or not repo:
        return None
    return {
        "host": host,
        "owner": owner,
        "repo": repo,
        "repo_full_name": f"{owner}/{repo}",
    }


def codebase_provider(config: dict[str, Any]) -> str | None:
    provider = codebase_analysis_config(config).get("provider")
    return provider.strip() if isinstance(provider, str) and provider.strip() else None


def build_link_record(
    url: str,
    relative_path: str,
    raw_line: int | None,
    default_status: str,
    previous_detected_at: dict[str, str],
    detected_at: str,
    warnings: list[str],
    config: dict[str, Any],
) -> dict[str, Any]:
    github = github_repo_metadata(url)
    if github and codebase_analysis_enabled(config):
        source_id = stable_codebase_id(url, f"github-{github['owner']}-{github['repo']}")
        metadata: dict[str, Any] = {
            "host": urlparse(url).netloc.lower().removeprefix("www."),
            "link_file": relative_path,
            "codebase_source_type": "repo_link",
            "codebase_tool": codebase_provider(config),
            "codebase_intake": {
                "mode": "external_artifact_only",
                "bounded": True,
                "product_execution": "none",
                "requires_external_artifact": True,
            },
            **github,
        }
        if raw_line is not None:
            metadata["raw_line"] = raw_line
        metadata["codebase_output_dir"] = codebase_artifact_dir(config, source_id)
        if warnings:
            metadata["warnings"] = warnings
        return {
            "id": source_id,
            "kind": "codebase_architecture",
            "url": url,
            "raw_paths": [relative_path],
            "status": default_status,
            "detected_at": previous_detected_at.get(source_id, detected_at),
            "metadata": metadata,
        }

    source_id = stable_link_id(url)
    metadata: dict[str, Any] = {
        "host": urlparse(url).netloc.lower().removeprefix("www."),
        "link_file": relative_path,
    }
    if raw_line is not None:
        metadata["raw_line"] = raw_line
    if github:
        metadata.update(github)
    if warnings:
        metadata["warnings"] = warnings

    return {
        "id": source_id,
        "kind": "repo_link" if github else "web_link",
        "url": url,
        "raw_paths": [relative_path],
        "status": default_status,
        "detected_at": previous_detected_at.get(source_id, detected_at),
        "metadata": metadata,
    }


def merge_link_record(existing: dict[str, Any], new_record: dict[str, Any]) -> None:
    for raw_path in new_record.get("raw_paths", []):
        if isinstance(raw_path, str):
            add_raw_path(existing, raw_path)
    metadata = ensure_metadata(existing)
    new_metadata = new_record.get("metadata")
    if not isinstance(new_metadata, dict):
        return
    link_files = metadata.get("link_files")
    if not isinstance(link_files, list):
        link_files = unique_values(
            [value for value in [metadata.get("link_file")] if isinstance(value, str)]
        )
        metadata["link_files"] = link_files
    new_link_file = new_metadata.get("link_file")
    if isinstance(new_link_file, str) and new_link_file not in link_files:
        link_files.append(new_link_file)


def build_link_records(
    project_root: Path,
    raw_files: list[Path],
    source_roots: list[str],
    bundle_dirs: list[Path],
    default_status: str,
    previous_detected_at: dict[str, str],
    detected_at: str,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], set[Path], dict[str, list[str]], list[str]]:
    records_by_id: dict[str, dict[str, Any]] = {}
    parsed_paths: set[Path] = set()
    file_warnings: dict[str, list[str]] = {}
    warnings: list[str] = []

    for path in raw_files:
        if is_inside_any(path, bundle_dirs):
            continue
        raw_root = raw_root_for_path(project_root, path, source_roots)
        if not is_link_parse_candidate(path, raw_root):
            continue
        relative_path = path.relative_to(project_root).as_posix()
        occurrences, parse_warnings = parse_link_file(path, relative_path)
        if parse_warnings:
            file_warnings[relative_path] = parse_warnings
            warnings.extend(parse_warnings)
        if not occurrences:
            continue
        parsed_paths.add(path)
        for occurrence in occurrences:
            url = occurrence["url"]
            record = build_link_record(
                url,
                relative_path,
                occurrence.get("raw_line"),
                default_status,
                previous_detected_at,
                detected_at,
                parse_warnings,
                config,
            )
            existing = records_by_id.get(record["id"])
            if existing:
                merge_link_record(existing, record)
            else:
                records_by_id[record["id"]] = record

    return list(records_by_id.values()), parsed_paths, file_warnings, unique_values(warnings)


def local_repo_markers(path: Path) -> list[str]:
    return [marker for marker in CODEBASE_LOCAL_REPO_MARKERS if (path / marker).exists()]


def configured_codebase_source_roots(project_root: Path, source_roots: list[str], config: dict[str, Any]) -> list[Path]:
    codebase = codebase_analysis_config(config)
    configured = codebase.get("source_roots")
    roots: list[str]
    if isinstance(configured, list) and all(isinstance(item, str) and item.strip() for item in configured):
        roots = [item.strip() for item in configured]
    else:
        roots = [root for root in source_roots if PurePosixPath(root).name in CODEBASE_DEFAULT_SOURCE_ROOT_NAMES]
    return [project_root / root for root in roots]


def iter_local_code_repos(
    project_root: Path, source_roots: list[str], config: dict[str, Any]
) -> tuple[list[Path], list[str]]:
    repos: list[Path] = []
    warnings: list[str] = []
    project_root_resolved = project_root.resolve()
    for raw_root in configured_codebase_source_roots(project_root, source_roots, config):
        root_relative = raw_root.relative_to(project_root)
        # A configured codebase source root that is itself a symlink, or whose
        # real path escapes the workspace, is refused outright before rglob can
        # descend into it. raw/ is the untrusted-input boundary the research
        # wiki rests on; extracted code trees are exactly where malicious
        # symlinks land (review SEC-E1 / H1, cross-ref M-14). The symlink check
        # stays explicit and before is_dir (a symlink to a directory passes
        # is_dir) to emit the root-specific wording; is_contained_nonsymlink then
        # supplies the shared containment guard for the escape case.
        if raw_root.is_symlink():
            warnings.append(f"refusing symlinked codebase source root: {root_relative.as_posix()}")
            continue
        if not raw_root.is_dir():
            continue
        if not is_contained_nonsymlink(raw_root, project_root_resolved):
            warnings.append(
                f"refusing codebase source root that resolves outside workspace: {root_relative.as_posix()}"
            )
            continue
        for path in (raw_root, *raw_root.rglob("*")):
            relative = path.relative_to(project_root)
            # Mirror iter_raw_files via the shared containment guard: refuse any
            # symlinked entry and any entry whose real path escapes the workspace.
            # A symlinked repo dir would otherwise be enumerated as a local_repo_dir
            # and its target bytes read via local_repo_file_count / normalization.
            # The is_symlink() re-check only selects the diagnostic wording.
            if not is_contained_nonsymlink(path, project_root_resolved):
                if path.is_symlink():
                    warnings.append(
                        f"refusing symlinked directory in codebase source root: {relative.as_posix()}"
                    )
                else:
                    warnings.append(f"refusing path that resolves outside workspace: {relative.as_posix()}")
                continue
            if not path.is_dir():
                continue
            if is_inside_any(path, repos):
                continue
            if should_skip(relative):
                continue
            if local_repo_markers(path):
                repos.append(path)
    repos.sort(key=lambda path: path.relative_to(project_root).as_posix())
    return repos, warnings


def local_repo_file_count(project_root: Path, repo_dir: Path, *, limit: int | None = None) -> int:
    count = 0
    for path in repo_dir.rglob("*"):
        if not path.is_file() or should_skip(path.relative_to(project_root)):
            continue
        count += 1
        if limit is not None and count > limit:
            return count
    return count


def build_local_codebase_record(
    project_root: Path,
    repo_dir: Path,
    default_status: str,
    previous_detected_at: dict[str, str],
    detected_at: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    relative_path = repo_dir.relative_to(project_root).as_posix()
    source_id = stable_codebase_id(relative_path, PurePosixPath(relative_path).name)
    file_count = local_repo_file_count(project_root, repo_dir, limit=CODEBASE_MAX_LOCAL_REPO_FILES)
    accepted = file_count <= CODEBASE_MAX_LOCAL_REPO_FILES
    warnings = [] if accepted else [
        (
            f"{source_id}: local snapshot has {file_count} files, exceeding the "
            f"bounded intake limit {CODEBASE_MAX_LOCAL_REPO_FILES}"
        )
    ]
    return {
        "id": source_id,
        "kind": "codebase_architecture",
        "raw_paths": [relative_path],
        "status": default_status,
        "detected_at": previous_detected_at.get(source_id, detected_at),
        "metadata": {
            "codebase_source_type": "local_repo",
            "codebase_tool": codebase_provider(config),
            "codebase_output_dir": codebase_artifact_dir(config, source_id),
            "repo_name": repo_dir.name,
            "markers": local_repo_markers(repo_dir),
            "file_count": file_count,
            "codebase_intake": {
                "mode": "local_inert_snapshot",
                "bounded": accepted,
                "product_execution": "none",
                "file_limit": CODEBASE_MAX_LOCAL_REPO_FILES,
                "file_count": file_count,
            },
            **({"review_required": True, "warnings": warnings} if warnings else {}),
        },
    }


def build_code_archive_record(
    project_root: Path,
    relative_path: str,
    stat: Any,
    default_status: str,
    previous_detected_at: dict[str, str],
    detected_at: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    source_id = stable_codebase_id(relative_path, PurePosixPath(relative_path).stem)
    within_limit = stat.st_size <= CODEBASE_MAX_ARCHIVE_BYTES
    checksum = hash_file_contents(project_root / relative_path) if within_limit else None
    accepted = within_limit and checksum is not None
    warnings: list[str] = []
    if not within_limit:
        warnings.append(
            f"{source_id}: code archive is {stat.st_size} bytes, exceeding the "
            f"bounded intake limit {CODEBASE_MAX_ARCHIVE_BYTES}"
        )
    elif checksum is None:
        warnings.append(f"{source_id}: code archive could not be read for bounded checksum verification")
    return {
        "id": source_id,
        "kind": "codebase_architecture",
        "raw_paths": [relative_path],
        "status": default_status,
        "detected_at": previous_detected_at.get(source_id, detected_at),
        "metadata": {
            "codebase_source_type": "code_archive",
            "codebase_tool": codebase_provider(config),
            "codebase_output_dir": codebase_artifact_dir(config, source_id),
            "extension": "".join(PurePosixPath(relative_path).suffixes),
            "size_bytes": stat.st_size,
            "sha256": f"sha256:{checksum}" if checksum else None,
            "codebase_intake": {
                "mode": "inert_archive",
                "bounded": accepted,
                "product_execution": "none",
                "archive_limit_bytes": CODEBASE_MAX_ARCHIVE_BYTES,
                "size_bytes": stat.st_size,
            },
            **({"review_required": True, "warnings": warnings} if warnings else {}),
        },
    }


def ensure_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        record["metadata"] = metadata
    return metadata


def append_record_warning(record: dict[str, Any], warning: str) -> None:
    metadata = ensure_metadata(record)
    warnings = metadata.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
        metadata["warnings"] = warnings
    if warning not in warnings:
        warnings.append(warning)


def add_raw_path(record: dict[str, Any], raw_path: str) -> None:
    raw_paths = record.get("raw_paths")
    if not isinstance(raw_paths, list):
        raw_paths = []
        record["raw_paths"] = raw_paths
    if raw_path not in raw_paths:
        raw_paths.append(raw_path)


def arxiv_pairing_key(value: str) -> str | None:
    candidate = value.strip().lower()
    if ARXIV_ID_RE.match(candidate):
        return f"arxiv:{candidate}"
    return None


def pdf_pairing_keys(record: dict[str, Any]) -> list[str]:
    raw_paths = record.get("raw_paths")
    if not isinstance(raw_paths, list) or not raw_paths or not isinstance(raw_paths[0], str):
        return []
    stem = PurePosixPath(raw_paths[0]).stem
    keys = [f"slug:{slugify(stem)}"]
    arxiv_key = arxiv_pairing_key(stem)
    if arxiv_key:
        keys.insert(0, arxiv_key)
    return unique_values(keys)


def paper_pairing_keys(record: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        arxiv_id = metadata.get("arxiv_id")
        if isinstance(arxiv_id, str):
            arxiv_key = arxiv_pairing_key(arxiv_id)
            if arxiv_key:
                keys.append(arxiv_key)

    latex_root = record.get("latex_root")
    if isinstance(latex_root, str):
        bundle_name = PurePosixPath(latex_root).name
        keys.append(f"slug:{slugify(bundle_name)}")
        arxiv_id = arxiv_id_from_bundle_name(bundle_name)
        if arxiv_id:
            keys.append(f"arxiv:{arxiv_id.lower()}")

    return unique_values(keys)


def unique_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        record_id = record.get("id")
        key = record_id if isinstance(record_id, str) else str(id(record))
        if key not in seen:
            unique.append(record)
            seen.add(key)
    return unique


def index_records_by_pairing_key(
    records: list[dict[str, Any]],
    key_fn: Any,
) -> dict[str, list[dict[str, Any]]]:
    indexed: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for key in key_fn(record):
            indexed.setdefault(key, []).append(record)
    return indexed


def record_label(record: dict[str, Any]) -> str:
    record_id = record.get("id")
    return record_id if isinstance(record_id, str) else "<unknown>"


def raw_pdf_path(record: dict[str, Any]) -> str | None:
    raw_paths = record.get("raw_paths")
    if not isinstance(raw_paths, list) or not raw_paths or not isinstance(raw_paths[0], str):
        return None
    return raw_paths[0]


def apply_pdf_pairing(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    papers = [record for record in records if record.get("kind") == "paper"]
    pdfs = [record for record in records if record.get("kind") == "pdf"]
    paper_by_key = index_records_by_pairing_key(papers, paper_pairing_keys)
    pdf_by_key = index_records_by_pairing_key(pdfs, pdf_pairing_keys)
    pdf_candidates = {
        record_label(pdf): unique_records(
            [candidate for key in pdf_pairing_keys(pdf) for candidate in paper_by_key.get(key, [])]
        )
        for pdf in pdfs
    }
    paper_candidates = {
        record_label(paper): unique_records(
            [candidate for key in paper_pairing_keys(paper) for candidate in pdf_by_key.get(key, [])]
        )
        for paper in papers
    }

    paired_pdf_ids: set[str] = set()
    paired_paper_ids: set[str] = set()
    warnings: list[str] = []

    for pdf in sorted(pdfs, key=record_label):
        pdf_id = record_label(pdf)
        candidates = pdf_candidates.get(pdf_id, [])
        if len(candidates) != 1:
            continue
        paper = candidates[0]
        paper_id = record_label(paper)
        if len(paper_candidates.get(paper_id, [])) != 1:
            continue

        raw_pdf = raw_pdf_path(pdf)
        if not raw_pdf:
            continue
        paper["raw_pdf"] = raw_pdf
        paper["pairing_status"] = "paired"
        add_raw_path(paper, raw_pdf)
        shared_keys = sorted(set(pdf_pairing_keys(pdf)).intersection(paper_pairing_keys(paper)))
        metadata = ensure_metadata(paper)
        if shared_keys:
            metadata["pairing_keys"] = shared_keys
        paired_pdf_ids.add(pdf_id)
        paired_paper_ids.add(paper_id)

    for pdf in pdfs:
        pdf_id = record_label(pdf)
        if pdf_id in paired_pdf_ids:
            continue
        candidates = pdf_candidates.get(pdf_id, [])
        raw_pdf = raw_pdf_path(pdf)
        if raw_pdf:
            pdf["raw_pdf"] = raw_pdf
        if candidates:
            pdf["pairing_status"] = "ambiguous"
            metadata = ensure_metadata(pdf)
            metadata["candidate_latex_roots"] = sorted(
                root
                for root in (candidate.get("latex_root") for candidate in candidates)
                if isinstance(root, str)
            )
            metadata["review_required"] = True
            warning = f"{pdf_id}: ambiguous PDF/source pairing"
            append_record_warning(pdf, warning)
            warnings.append(warning)
        else:
            pdf["pairing_status"] = "pdf_only"
            metadata = ensure_metadata(pdf)
            metadata["review_required"] = True
            warning = f"{pdf_id}: no matching LaTeX source bundle found"
            append_record_warning(pdf, warning)
            warnings.append(warning)

    for paper in papers:
        paper_id = record_label(paper)
        if paper_id in paired_paper_ids:
            continue
        candidates = paper_candidates.get(paper_id, [])
        if candidates:
            paper["pairing_status"] = "ambiguous"
            metadata = ensure_metadata(paper)
            metadata["candidate_raw_pdfs"] = sorted(
                path for path in (raw_pdf_path(candidate) for candidate in candidates) if isinstance(path, str)
            )
            metadata["review_required"] = True
            warning = f"{paper_id}: ambiguous PDF/source pairing"
            append_record_warning(paper, warning)
            warnings.append(warning)
        else:
            paper["pairing_status"] = "latex_only"
            metadata = ensure_metadata(paper)
            metadata["review_required"] = True
            warning = f"{paper_id}: no matching PDF found"
            append_record_warning(paper, warning)
            warnings.append(warning)

    paired_records = [
        record
        for record in records
        if not (record.get("kind") == "pdf" and record_label(record) in paired_pdf_ids)
    ]
    paired_records.sort(key=lambda record: record["id"])
    summary = {
        "paired": len(paired_paper_ids),
        "pdf_only": sum(1 for record in paired_records if record.get("pairing_status") == "pdf_only"),
        "latex_only": sum(1 for record in paired_records if record.get("pairing_status") == "latex_only"),
        "ambiguous": sum(1 for record in paired_records if record.get("pairing_status") == "ambiguous"),
    }
    return paired_records, unique_values(warnings), summary


def hash_file_contents(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def is_provenance_sidecar(path: Path) -> bool:
    return path.name.endswith(PROVENANCE_SIDECAR_SUFFIX)


def provenance_timestamp_text(value: Any) -> str | None:
    """Normalize a sidecar retrieved_at value to ISO 8601 text, or None when invalid.

    YAML parses unquoted ISO timestamps into datetime/date objects, so both
    object and string forms must be accepted.
    """
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def normalize_sha256_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if PROVENANCE_CHECKSUM_RE.match(text):
        return text
    if SHA256_HEX_RE.match(text):
        return f"sha256:{text}"
    return None


def parse_date_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    parsed: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            return None
        key = raw_key.strip()
        if isinstance(raw_value, bool) or raw_value is None:
            return None
        if isinstance(raw_value, int):
            parsed[key] = raw_value
        elif isinstance(raw_value, (date, datetime)):
            parsed[key] = raw_value.isoformat()
        elif isinstance(raw_value, str) and raw_value.strip():
            parsed[key] = raw_value.strip()
        else:
            return None
    return parsed


def parse_evidence_usability_override(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(value, dict):
        return None, "provenance evidence_usability_override must be a mapping"
    missing: list[str] = []
    usable = value.get("usable")
    if usable is not True:
        missing.append("usable: true")
    parsed: dict[str, Any] = {"usable": True}
    for key in ("reviewed_by", "reviewed_at", "reason"):
        raw = value.get(key)
        if not isinstance(raw, str) or not raw.strip():
            missing.append(key)
            continue
        parsed[key] = raw.strip()
    if missing:
        return (
            None,
            "provenance evidence_usability_override requires "
            + ", ".join(missing),
        )
    return parsed, None


def parse_string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    parsed = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return parsed if len(parsed) == len(value) else None


def parse_provenance_sidecar(path: Path, relative_path: str) -> tuple[dict[str, Any], list[str]]:
    """Parse one provenance sidecar; malformed content degrades to warnings, never failure."""
    warnings: list[str] = []
    try:
        document = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        return {}, [f"{relative_path}: malformed provenance sidecar: {exc}"]
    if not isinstance(document, dict):
        return {}, [f"{relative_path}: provenance sidecar must be a YAML mapping"]

    data: dict[str, Any] = {}
    for key in sorted(str(key) for key in set(document) - set(PROVENANCE_FIELDS)):
        warnings.append(f"{relative_path}: unknown provenance field ignored: {key}")
    for field in PROVENANCE_FIELDS:
        if field not in document:
            continue
        value = document[field]
        if field == "license" and value is None:
            data[field] = None
            continue
        if field == "retrieved_at":
            timestamp = provenance_timestamp_text(value)
            if timestamp is None:
                warnings.append(f"{relative_path}: provenance retrieved_at must be an ISO 8601 timestamp")
                continue
            data[field] = timestamp
            continue
        if field in {"publication_year", "openalex_publication_year", "openalex_reported_publication_year"}:
            if isinstance(value, int) and not isinstance(value, bool):
                data[field] = value
                continue
            if isinstance(value, str) and re.fullmatch(r"\d{4}", value.strip()):
                data[field] = int(value.strip())
                continue
            warnings.append(f"{relative_path}: provenance {field} must be a four-digit year")
            continue
        if field == "date_metadata":
            parsed = parse_date_metadata(value)
            if parsed is None:
                warnings.append(f"{relative_path}: provenance date_metadata must be a mapping of scalar date metadata")
                continue
            data[field] = parsed
            continue
        if field == "standards":
            if isinstance(value, dict):
                data[field] = dict(value)
                continue
            data[field] = {"review_required": True}
            warnings.append(f"{relative_path}: provenance standards must be a mapping")
            continue
        if field == "evidence_usability_override":
            parsed_override, warning = parse_evidence_usability_override(value)
            if warning is not None:
                warnings.append(f"{relative_path}: {warning}")
                continue
            data[field] = parsed_override
            continue
        if field == "supported_evidence_areas":
            parsed = parse_string_list(value)
            if parsed is None:
                warnings.append(f"{relative_path}: provenance supported_evidence_areas must be a list of non-empty strings")
                continue
            data[field] = parsed
            continue
        if field == "authors":
            parsed = parse_string_list(value)
            if parsed is None:
                warnings.append(f"{relative_path}: provenance authors must be a list of non-empty strings")
                continue
            data[field] = parsed
            continue
        if field == "openalex_reported_authors":
            parsed = parse_string_list(value)
            if parsed is None:
                warnings.append(f"{relative_path}: provenance openalex_reported_authors must be a list of non-empty strings")
                continue
            data[field] = parsed
            continue
        if field in {"openalex_identity_evidence", "doi_resolution"}:
            if isinstance(value, dict):
                data[field] = dict(value)
                continue
            warnings.append(f"{relative_path}: provenance {field} must be a mapping")
            continue
        if field in {"openalex_title_lag", "openalex_identity_conflict"}:
            if isinstance(value, bool):
                data[field] = value
                continue
            warnings.append(f"{relative_path}: provenance {field} must be a boolean")
            continue
        if field == "published":
            if isinstance(value, datetime):
                data[field] = value.isoformat().replace("+00:00", "Z")
                continue
            if isinstance(value, date):
                data[field] = value.isoformat()
                continue
        if field in {"byte_count", "http_status"}:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                data[field] = value
                continue
            warnings.append(f"{relative_path}: provenance {field} must be a non-negative integer")
            continue
        if field == "redirect_chain":
            parsed = parse_string_list(value)
            if parsed is None:
                warnings.append(f"{relative_path}: provenance redirect_chain must be a list of non-empty strings")
                continue
            data[field] = parsed
            continue
        if field == "tls_verified":
            if isinstance(value, bool):
                data[field] = value
                continue
            warnings.append(f"{relative_path}: provenance tls_verified must be a boolean")
            continue
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"{relative_path}: provenance {field} must be a non-empty string")
            continue
        text = value.strip()
        if field == "source_status" and text not in SOURCE_STATUS_VALUES:
            allowed = ", ".join(SOURCE_STATUS_VALUES)
            warnings.append(f"{relative_path}: provenance source_status must be one of: {allowed}")
            continue
        if field == "delivery_failure_code" and text not in DELIVERY_FAILURE_CODES:
            allowed = ", ".join(DELIVERY_FAILURE_CODES)
            warnings.append(f"{relative_path}: provenance delivery_failure_code must be one of: {allowed}")
            continue
        if field in {"checksum", "sha256"}:
            normalized_sha = normalize_sha256_text(text)
            if normalized_sha is None:
                warnings.append(f"{relative_path}: provenance {field} must match sha256:<64 hex chars>")
                continue
            data[field] = normalized_sha
            continue
        data[field] = text
    if "url" in data and "origin_url" not in data:
        data["origin_url"] = data["url"]
    if "sha256" in data and "checksum" not in data:
        data["checksum"] = data["sha256"]
    if not data:
        warnings.append(f"{relative_path}: provenance sidecar has no usable fields")
    return data, warnings


def collect_provenance_sidecars(
    project_root: Path,
    raw_files: list[Path],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Map delivered-target relative paths to parsed sidecar provenance."""
    sidecars: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    raw_file_rels = {path.relative_to(project_root).as_posix() for path in raw_files}
    sidecar_rels = {
        path.relative_to(project_root).as_posix()
        for path in raw_files
        if is_provenance_sidecar(path)
    }
    for path in raw_files:
        if not is_provenance_sidecar(path):
            continue
        relative_path = path.relative_to(project_root).as_posix()
        target_rel = relative_path[: -len(PROVENANCE_SIDECAR_SUFFIX)]
        data, parse_warnings = parse_provenance_sidecar(path, relative_path)
        warnings.extend(parse_warnings)
        if not (project_root / target_rel).exists():
            target_path = PurePosixPath(target_rel)
            if (
                target_path.parent.as_posix() == "raw/web"
                and target_path.suffix == ""
                and f"{target_rel}.html" in raw_file_rels
            ):
                warnings.append(
                    f"{relative_path}: legacy web provenance sidecar missing .html segment; "
                    f"expected {target_rel}.html{PROVENANCE_SIDECAR_SUFFIX}"
                )
            warnings.append(f"{relative_path}: provenance sidecar target does not exist: {target_rel}")
            continue
        if not data:
            continue
        sidecars[target_rel] = {"sidecar_path": relative_path, "data": data}
    for raw_rel in sorted(raw_file_rels):
        raw_path = PurePosixPath(raw_rel)
        if raw_path.parent.as_posix() != "raw/web" or raw_path.suffix.lower() not in HTML_EXTENSIONS:
            continue
        expected_sidecar = f"{raw_rel}{PROVENANCE_SIDECAR_SUFFIX}"
        if expected_sidecar not in sidecar_rels:
            warnings.append(f"{raw_rel}: missing canonical provenance sidecar: {expected_sidecar}")
    return sidecars, warnings


def provenance_candidate_paths(record: dict[str, Any]) -> list[str]:
    """Delivered paths a record may carry a sidecar for, primary path first."""
    candidates: list[str] = []
    latex_root = record.get("latex_root")
    if isinstance(latex_root, str):
        candidates.append(latex_root)
    raw_paths = record.get("raw_paths")
    if isinstance(raw_paths, list):
        candidates.extend(path for path in raw_paths if isinstance(path, str))
    return unique_values(candidates)


def apply_provenance_sidecars(
    project_root: Path,
    records: list[dict[str, Any]],
    sidecars: dict[str, dict[str, Any]],
) -> list[str]:
    """Merge sidecar provenance into matching records and verify checksums."""
    warnings: list[str] = []
    matched: set[str] = set()
    for record in records:
        candidates = provenance_candidate_paths(record)
        primary = next((candidate for candidate in candidates if candidate in sidecars), None)
        if primary is None:
            continue
        entry = sidecars[primary]
        matched.add(primary)
        provenance: dict[str, Any] = dict(entry["data"])
        provenance["sidecar_path"] = entry["sidecar_path"]
        license_value = provenance.get("license")
        if isinstance(license_value, str) and license_value != "unresolved" and license_value not in SPDX_LICENSE_IDS:
            warning = (
                f"{entry['sidecar_path']}: provenance license is not in the SPDX allowlist: {license_value}"
            )
            provenance.pop("license", None)
            append_record_warning(record, warning)
            ensure_metadata(record)["review_required"] = True
            warnings.append(warning)
        checksum = provenance.get("checksum")
        if checksum:
            target = project_root / primary
            if target.is_file():
                actual = hash_file_contents(target)
                verified = actual is not None and f"sha256:{actual}" == checksum
                provenance["checksum_verified"] = verified
                if not verified:
                    warning = f"provenance checksum mismatch for {primary} (sidecar {entry['sidecar_path']})"
                    append_record_warning(record, warning)
                    ensure_metadata(record)["review_required"] = True
                    warnings.append(warning)
            else:
                provenance["checksum_verified"] = False
                warning = f"{entry['sidecar_path']}: checksum cannot be verified for a directory target"
                append_record_warning(record, warning)
                warnings.append(warning)
        record["provenance"] = provenance
        standards = provenance.get("standards")
        if isinstance(standards, dict) and standards.get("review_required") is True:
            warning = f"{entry['sidecar_path']}: standards metadata requires review"
            append_record_warning(record, warning)
            ensure_metadata(record)["review_required"] = True
            warnings.append(warning)
        for extra in candidates:
            if extra == primary or extra not in sidecars:
                continue
            matched.add(extra)
            warning = (
                f"{sidecars[extra]['sidecar_path']}: additional provenance sidecar not merged "
                f"(record {record_label(record)} already carries provenance from {entry['sidecar_path']})"
            )
            append_record_warning(record, warning)
            warnings.append(warning)
    for target_rel in sorted(set(sidecars) - matched):
        warnings.append(f"{sidecars[target_rel]['sidecar_path']}: provenance sidecar matches no source record")
    return warnings


def apply_unusable_evidence_flags(records: list[dict[str, Any]]) -> None:
    for record in records:
        provenance = record.get("provenance")
        reasons = unique_values(delivery_unusable_evidence_reasons(provenance if isinstance(provenance, dict) else {}))
        if reasons:
            record["evidence_usable"] = False
            record["unusable_evidence_reasons"] = reasons
        else:
            record.pop("evidence_usable", None)
            record.pop("unusable_evidence_reasons", None)


def raw_fingerprint_paths(project_root: Path, record: dict[str, Any]) -> list[Path]:
    """Raw files whose bytes determine a record's normalized output.

    Paper bundles, PDFs, HTML pages, and CSV/TSV tables are covered: those are
    re-derived from raw bytes by normalize_sources.py, so a content change
    should trigger re-normalization. Links and codebase records derive their
    output from manifest metadata or generated artifacts, not raw bytes, so
    they carry no fingerprint. Provenance sidecars count toward the
    fingerprint so provenance corrections also re-trigger normalization.
    """
    kind = record.get("kind")
    paths: list[Path] = []
    if kind == "paper":
        latex_root = record.get("latex_root")
        if isinstance(latex_root, str):
            base = project_root / latex_root
            if base.is_dir():
                paths.extend(
                    path
                    for path in base.rglob("*")
                    if path.is_file() and not should_skip(path.relative_to(project_root))
                )
            sidecar = project_root / f"{latex_root}{PROVENANCE_SIDECAR_SUFFIX}"
            if sidecar.is_file():
                paths.append(sidecar)
        raw_pdf = record.get("raw_pdf")
        if isinstance(raw_pdf, str):
            pdf = project_root / raw_pdf
            if pdf.is_file():
                paths.append(pdf)
            sidecar = project_root / f"{raw_pdf}{PROVENANCE_SIDECAR_SUFFIX}"
            if sidecar.is_file():
                paths.append(sidecar)
    elif kind == "pdf":
        raw_paths = record.get("raw_paths")
        if isinstance(raw_paths, list):
            for raw_path in raw_paths:
                if isinstance(raw_path, str) and raw_path.lower().endswith(".pdf"):
                    pdf = project_root / raw_path
                    if pdf.is_file():
                        paths.append(pdf)
                    sidecar = project_root / f"{raw_path}{PROVENANCE_SIDECAR_SUFFIX}"
                    if sidecar.is_file():
                        paths.append(sidecar)
    elif kind in {"html", "table"}:
        eligible_suffixes = HTML_EXTENSIONS if kind == "html" else TABLE_TEXT_EXTENSIONS
        raw_paths = record.get("raw_paths")
        if isinstance(raw_paths, list):
            for raw_path in raw_paths:
                if not (isinstance(raw_path, str) and PurePosixPath(raw_path).suffix.lower() in eligible_suffixes):
                    continue
                source_file = project_root / raw_path
                if source_file.is_file():
                    paths.append(source_file)
                sidecar = project_root / f"{raw_path}{PROVENANCE_SIDECAR_SUFFIX}"
                if sidecar.is_file():
                    paths.append(sidecar)
    return paths


def compute_raw_fingerprint(project_root: Path, record: dict[str, Any]) -> str | None:
    paths = raw_fingerprint_paths(project_root, record)
    if not paths:
        return None
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda item: item.relative_to(project_root).as_posix()):
        file_hash = hash_file_contents(path)
        if file_hash is None:
            continue
        digest.update(path.relative_to(project_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def iter_raw_files(project_root: Path, source_roots: list[str]) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    warnings: list[str] = []
    project_root_resolved = project_root.resolve()
    for raw_root_text in source_roots:
        raw_root = project_root / raw_root_text
        if not raw_root.exists():
            warnings.append(f"missing raw root: {raw_root_text}")
            continue
        if not raw_root.is_dir():
            warnings.append(f"raw root is not a directory: {raw_root_text}")
            continue
        for path in raw_root.rglob("*"):
            relative = path.relative_to(project_root)
            # raw/ is the untrusted-input boundary and is declared immutable, so an
            # entry that is a symlink (pointing anywhere, inside or outside the
            # workspace) or whose real path escapes the workspace is refused rather
            # than read. is_contained_nonsymlink holds the shared containment
            # definition; the is_symlink() re-check below only selects the wording.
            if not is_contained_nonsymlink(path, project_root_resolved):
                if path.is_symlink():
                    warnings.append(f"refusing symlink in raw root: {relative.as_posix()}")
                else:
                    warnings.append(f"refusing path that resolves outside workspace: {relative.as_posix()}")
                continue
            if path.is_file() and not should_skip(relative):
                files.append(path)
    files.sort(key=lambda path: path.relative_to(project_root).as_posix())
    return files, warnings


def revalidate_enumerated_raw_files(project_root: Path, paths: list[Path]) -> tuple[list[Path], list[str]]:
    """Refuse entries replaced between enumeration and record construction.

    Raw roots are an untrusted-input boundary.  Re-checking the returned paths
    closes the deterministic enumerate-then-replace window before classifiers,
    sidecar parsers, or fingerprint readers inspect the entry.  This is not a
    substitute for the documented single-writer workflow contract: native
    junction and hostile concurrent-writer proof remains a platform lane.
    """
    project_root_resolved = project_root.resolve()
    safe: list[Path] = []
    warnings: list[str] = []
    for path in paths:
        try:
            relative = path.relative_to(project_root).as_posix()
        except ValueError:
            warnings.append(f"refusing raw path changed after enumeration: {path}")
            continue
        if not is_contained_nonsymlink(path, project_root_resolved) or not path.is_file():
            warnings.append(f"refusing raw path changed after enumeration: {relative}")
            continue
        safe.append(path)
    return safe, warnings


def _build_records_unlocked(
    project_root: Path,
    config: dict[str, Any],
    previous_detected_at: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    raw_config = config.get("raw") or {}
    sources_config = config.get("sources") or {}
    source_roots = raw_config.get("source_roots") or []
    if not isinstance(source_roots, list):
        raise SystemExit("research.yml raw.source_roots must be a list")
    source_roots = [
        validate_workspace_relative_path(root, "raw.source_roots")
        for root in source_roots
    ]

    default_status = sources_config.get("default_status", "discovered")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    incomplete_targets, incomplete_warnings = incomplete_acquisition_targets(project_root, source_roots)
    raw_files, warnings = iter_raw_files(project_root, source_roots)
    warnings.extend(incomplete_warnings)
    raw_files = [path for path in raw_files if not is_inside_any(path, incomplete_targets)]
    raw_files, replacement_warnings = revalidate_enumerated_raw_files(project_root, raw_files)
    warnings.extend(replacement_warnings)
    records: list[dict[str, Any]] = []
    bundle_dirs: list[Path] = []
    local_repo_dirs: list[Path] = []

    if codebase_analysis_enabled(config):
        local_repo_dirs, repo_warnings = iter_local_code_repos(project_root, source_roots, config)
        local_repo_dirs = [path for path in local_repo_dirs if not is_inside_any(path, incomplete_targets)]
        warnings.extend(repo_warnings)
        for repo_dir in local_repo_dirs:
            records.append(
                build_local_codebase_record(
                    project_root,
                    repo_dir,
                    default_status,
                    previous_detected_at,
                    now,
                    config,
                )
            )

    for bundle_dir in iter_bundle_candidates(project_root, source_roots, warnings):
        if is_inside_any(bundle_dir, incomplete_targets):
            continue
        if is_inside_any(bundle_dir, bundle_dirs):
            continue
        record, bundle_warnings = build_bundle_record(
            project_root,
            bundle_dir,
            default_status,
            previous_detected_at,
            now,
        )
        warnings.extend(bundle_warnings)
        if record:
            records.append(record)
            bundle_dirs.append(bundle_dir)

    link_records, parsed_link_paths, link_file_warnings, link_warnings = build_link_records(
        project_root,
        raw_files,
        source_roots,
        bundle_dirs,
        default_status,
        previous_detected_at,
        now,
        config,
    )
    records.extend(link_records)
    warnings.extend(link_warnings)

    for path in raw_files:
        if is_inside_any(path, bundle_dirs) or is_inside_any(path, local_repo_dirs) or path in parsed_link_paths:
            continue
        if is_provenance_sidecar(path):
            continue
        relative_path = path.relative_to(project_root).as_posix()
        raw_root = raw_root_for_path(project_root, path, source_roots)
        kind = classify(path, raw_root)
        source_id = stable_id(relative_path)
        try:
            stat = path.stat()
        except OSError as exc:
            warnings.append(f"cannot stat {relative_path}: {exc}")
            continue
        if codebase_analysis_enabled(config) and kind == "code_archive":
            records.append(
                build_code_archive_record(
                    project_root,
                    relative_path,
                    stat,
                    default_status,
                    previous_detected_at,
                    now,
                    config,
                )
            )
            continue
        records.append(
            {
                "id": source_id,
                "kind": kind,
                "raw_paths": [relative_path],
                "status": default_status,
                "detected_at": previous_detected_at.get(source_id, now),
                "metadata": {
                    "extension": path.suffix.lower(),
                    "size_bytes": stat.st_size,
                },
            }
        )
        path_warnings = link_file_warnings.get(relative_path)
        if path_warnings:
            metadata = ensure_metadata(records[-1])
            metadata["review_required"] = True
            metadata["warnings"] = path_warnings

    records, pairing_warnings, summary = apply_pdf_pairing(records)
    warnings.extend(pairing_warnings)
    sidecars, sidecar_warnings = collect_provenance_sidecars(project_root, raw_files)
    warnings.extend(sidecar_warnings)
    warnings.extend(apply_provenance_sidecars(project_root, records, sidecars))
    apply_unusable_evidence_flags(records)
    for record in records:
        fingerprint = compute_raw_fingerprint(project_root, record)
        if fingerprint:
            record["raw_fingerprint"] = fingerprint
    records.sort(key=lambda record: record["id"])
    return records, warnings, summary


def build_records(
    project_root: Path,
    config: dict[str, Any],
    previous_detected_at: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    """Build one inventory snapshot while acquisition commits are invisible."""
    with workspace_lock(acquisition_workspace_lock_path(project_root), purpose="source inventory acquisition barrier"):
        return _build_records_unlocked(project_root, config, previous_detected_at)


def write_manifest(manifest_path: Path, records: list[dict[str, Any]]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records)
    # Unique temp name so concurrent runs cannot steal each other's temp file;
    # the final rename stays atomic on POSIX (same filesystem).
    tmp_path = manifest_path.with_name(f"{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(content)
    tmp_path.replace(manifest_path)


def count_by_field(records: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = record.get(field)
        if isinstance(value, str):
            counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def review_required_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    review_records: list[dict[str, Any]] = []
    for record in records:
        metadata = record.get("metadata")
        if isinstance(metadata, dict) and metadata.get("review_required") is True:
            review_records.append(record)
    return sorted(review_records, key=record_label)


def records_by_kind(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return sorted([record for record in records if record.get("kind") == kind], key=record_label)


def format_record_reference(record: dict[str, Any]) -> str:
    raw_paths = record.get("raw_paths")
    path_text = ", ".join(path for path in raw_paths if isinstance(path, str)) if isinstance(raw_paths, list) else ""
    record_id = record_label(record)
    kind = record.get("kind") if isinstance(record.get("kind"), str) else "unknown"
    if path_text:
        return f"`{record_id}` ({kind}) - {path_text}"
    return f"`{record_id}` ({kind})"


def unusable_evidence_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [record for record in records if record.get("evidence_usable") is False],
        key=record_label,
    )


def evidence_usable_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    unusable = len(unusable_evidence_records(records))
    return {"usable": len(records) - unusable, "unusable": unusable}


def report_readiness(
    summary: dict[str, int],
    review_records: list[dict[str, Any]],
    unknown_records: list[dict[str, Any]],
    unusable_records: list[dict[str, Any]],
) -> str:
    if summary.get("ambiguous", 0) or review_records or unknown_records or unusable_records:
        return "needs_review"
    return "ready_for_normalization"


def report_next_actions(
    readiness: str,
    summary: dict[str, int],
    review_records: list[dict[str, Any]],
    unknown_records: list[dict[str, Any]],
    raw_link_records: list[dict[str, Any]],
    unusable_records: list[dict[str, Any]],
) -> list[str]:
    actions: list[str] = []
    if summary.get("ambiguous", 0):
        actions.append("Resolve ambiguous PDF/source pairings before normalization.")
    if summary.get("pdf_only", 0):
        actions.append("Review PDF-only records; accept PDF extraction or add source bundles.")
    if summary.get("latex_only", 0):
        actions.append("Review LaTeX-only records; add matching PDFs or continue with LaTeX source.")
    if unknown_records:
        actions.append("Classify, move, or ignore unknown raw files.")
    if raw_link_records:
        actions.append("Fix malformed raw link files or remove them from raw link roots.")
    if unusable_records:
        actions.append("Redeliver or replace unusable source captures before using them in required coverage facets.")
    if review_records and not actions:
        actions.append("Inspect review-required records before normalization.")
    if readiness == "ready_for_normalization":
        actions.append("Proceed to source normalization.")
    return actions


def recompute_pairing_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "paired": sum(1 for record in records if record.get("pairing_status") == "paired"),
        "pdf_only": sum(1 for record in records if record.get("pairing_status") == "pdf_only"),
        "latex_only": sum(1 for record in records if record.get("pairing_status") == "latex_only"),
        "ambiguous": sum(1 for record in records if record.get("pairing_status") == "ambiguous"),
    }


def provenance_for_record(record: dict[str, Any]) -> dict[str, Any]:
    provenance = record.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def strict_checksum_refusals(
    records: list[dict[str, Any]],
    *,
    require_checksum: bool,
    reject_mismatch: bool,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    if not require_checksum and not reject_mismatch:
        return records, [], []

    kept: list[dict[str, Any]] = []
    warnings: list[str] = []
    refusals: list[dict[str, str]] = []
    for record in records:
        provenance = provenance_for_record(record)
        checksum_present = isinstance(provenance.get("checksum"), str)
        checksum_verified = provenance.get("checksum_verified") is True
        record_id = record_label(record)

        reason: str | None = None
        if reject_mismatch and checksum_present and not checksum_verified:
            reason = "checksum_mismatch"
            message = f"strict checksum refusal: {record_id} checksum is present but not verified"
        elif require_checksum and not checksum_verified:
            reason = "checksum_required"
            message = f"strict checksum refusal: {record_id} missing verified checksum"
        else:
            kept.append(record)
            continue

        warnings.append(message)
        refusals.append({"source_id": record_id, "reason": reason, "message": message})
    return kept, warnings, refusals


def strict_refusal_error_code(refusals: list[dict[str, str]]) -> str:
    if any(refusal.get("reason") == "checksum_mismatch" for refusal in refusals):
        return INVENTORY_CHECKSUM_MISMATCH
    return INVENTORY_CHECKSUM_REQUIRED


def strict_refusal_message(refusals: list[dict[str, str]]) -> str:
    count = len(refusals)
    noun = "source" if count == 1 else "sources"
    reasons = {refusal.get("reason") for refusal in refusals}
    if "checksum_mismatch" in reasons:
        return f"strict checksum mode refused {count} {noun} with unverified provenance checksums."
    return f"strict checksum mode refused {count} {noun} missing verified checksums."


def strict_refusal_details(refusals: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "source_ids": [refusal["source_id"] for refusal in refusals],
        "refusals": refusals,
    }


def build_report_data(
    records: list[dict[str, Any]],
    warnings: list[str],
    summary: dict[str, int],
    manifest_path: Path,
    project_root: Path,
    timestamp: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    review_records = review_required_records(records)
    unknown_records = records_by_kind(records, "unknown")
    raw_link_records = records_by_kind(records, "link")
    unusable_records = unusable_evidence_records(records)
    readiness = report_readiness(summary, review_records, unknown_records, unusable_records)
    return {
        "schema_version": INVENTORY_REPORT_SCHEMA_VERSION,
        "document_type": INVENTORY_REPORT_DOCUMENT_TYPE,
        "dry_run": dry_run,
        "timestamp": timestamp,
        "manifest": manifest_path.relative_to(project_root).as_posix(),
        "total": len(records),
        "kind_counts": count_by_field(records, "kind"),
        "pairing_counts": {
            "paired": summary.get("paired", 0),
            "pdf_only": summary.get("pdf_only", 0),
            "latex_only": summary.get("latex_only", 0),
            "ambiguous": summary.get("ambiguous", 0),
        },
        "evidence_usable_counts": evidence_usable_counts(records),
        "unusable_records": unusable_records,
        "review_records": review_records,
        "unknown_records": unknown_records,
        "raw_link_records": raw_link_records,
        "warnings": unique_values(warnings),
        "readiness": readiness,
        "next_actions": report_next_actions(
            readiness,
            summary,
            review_records,
            unknown_records,
            raw_link_records,
            unusable_records,
        ),
    }


def append_section(lines: list[str], title: str, values: list[str]) -> None:
    lines.append(f"## {title}")
    if values:
        lines.extend(f"- {value}" for value in values)
    else:
        lines.append("- none")
    lines.append("")


def render_inventory_report(data: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Source Inventory Report",
        "",
        f"- Generated: `{data['timestamp']}`",
        f"- Manifest: `{data['manifest']}`",
        f"- Readiness: `{data['readiness']}`",
        f"- Total records: {data['total']}",
        "",
    ]

    kind_counts = data["kind_counts"]
    append_section(lines, "Counts by Kind", [f"`{kind}`: {count}" for kind, count in kind_counts.items()])

    pairing_counts = data["pairing_counts"]
    append_section(
        lines,
        "PDF/LaTeX Pairing",
        [f"`{name}`: {pairing_counts[name]}" for name in ("paired", "pdf_only", "latex_only", "ambiguous")],
    )

    append_section(
        lines,
        "Review Required",
        [format_record_reference(record) for record in data["review_records"]],
    )
    unusable_counts = data["evidence_usable_counts"]
    append_section(
        lines,
        "Evidence Usability",
        [
            f"`usable`: {unusable_counts['usable']}",
            f"`unusable`: {unusable_counts['unusable']}",
        ],
    )
    append_section(
        lines,
        "Unusable Evidence",
        [format_record_reference(record) for record in data["unusable_records"]],
    )
    append_section(
        lines,
        "Unknown Files",
        [format_record_reference(record) for record in data["unknown_records"]],
    )
    append_section(
        lines,
        "Raw Link Files Requiring Review",
        [format_record_reference(record) for record in data["raw_link_records"]],
    )
    append_section(lines, "Anomalies", [f"`{warning}`" for warning in data["warnings"]])
    append_section(lines, "Next Actions", data["next_actions"])
    return "\n".join(lines).rstrip() + "\n"


def render_log_entry(data: dict[str, Any]) -> str:
    date_text = str(data["timestamp"]).split("T", 1)[0]
    pairing = data["pairing_counts"]
    return (
        f"## [{date_text}] inventory | Source inventory report\n\n"
        f"- Manifest: `{data['manifest']}`\n"
        f"- Records: {data['total']}\n"
        f"- Readiness: `{data['readiness']}`\n"
        f"- Anomalies: {len(data['warnings'])}\n"
        "- Pairing: "
        f"paired={pairing['paired']} "
        f"pdf_only={pairing['pdf_only']} "
        f"latex_only={pairing['latex_only']} "
        f"ambiguous={pairing['ambiguous']}\n"
    )


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


def append_log(project_root: Path, data: dict[str, Any]) -> None:
    append_log_entry(project_root / "log.md", render_log_entry(data))


def run_inventory(args: argparse.Namespace) -> int:
    if args.append_log and not args.report:
        raise SystemExit("--append-log requires --report")

    project_root = Path(args.project_root).resolve()
    config = load_config(project_root)
    sources_config = config.get("sources") or {}
    if not isinstance(sources_config, dict):
        raise SystemExit("research.yml sources must be a mapping")
    manifest_path_text = validate_workspace_relative_path(
        sources_config.get("manifest_path", "sources/manifest.jsonl"),
        "sources.manifest_path",
    )
    manifest_path = project_root / manifest_path_text
    previous = existing_detected_at(manifest_path)
    records, warnings, summary = build_records(project_root, config, previous)
    records, strict_warnings, strict_refusals = strict_checksum_refusals(
        records,
        require_checksum=args.require_checksum,
        reject_mismatch=args.reject_mismatch,
    )
    if strict_warnings:
        warnings.extend(strict_warnings)
        summary = recompute_pairing_summary(records)
    report_timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report_data = build_report_data(
        records,
        warnings,
        summary,
        manifest_path,
        project_root,
        report_timestamp,
        dry_run=args.dry_run,
    )

    if args.report:
        if args.format == "json":
            print(json.dumps(report_data, indent=2, sort_keys=False))
        else:
            print(render_inventory_report(report_data), end="")
    elif args.dry_run:
        for record in records:
            print(json.dumps(record, sort_keys=True, separators=(",", ":")))

    if not args.dry_run:
        write_manifest(manifest_path, records)
        if args.append_log:
            append_log(project_root, report_data)
    elif args.append_log:
        print("warning: --append-log skipped during --dry-run", file=sys.stderr)

    if strict_refusals and args.format == "json":
        emit_error(
            strict_refusal_message(strict_refusals),
            json_mode=True,
            error_code=strict_refusal_error_code(strict_refusals),
            details=strict_refusal_details(strict_refusals),
        )
        return EXIT_STRICT_REFUSAL

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    action = "would write" if args.dry_run else "wrote"
    print(
        "summary "
        f"paired={summary['paired']} "
        f"pdf_only={summary['pdf_only']} "
        f"latex_only={summary['latex_only']} "
        f"ambiguous={summary['ambiguous']}",
        file=sys.stderr,
    )
    print(f"{action} {len(records)} records to {manifest_path.relative_to(project_root)}", file=sys.stderr)
    return EXIT_STRICT_REFUSAL if strict_refusals else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    if text_format_explicitly_requested(argv):
        json_mode = False
    try:
        return run_inventory(args)
    except LockUnavailableError as exc:
        emit_error(str(exc), json_mode=json_mode, error_code=exc.error_code, details=exc.details)
        return 2
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=2)


if __name__ == "__main__":
    raise SystemExit(main())
