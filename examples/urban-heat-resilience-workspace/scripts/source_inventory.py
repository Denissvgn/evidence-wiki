#!/usr/bin/env python3
"""Inventory raw source assets into a deterministic JSONL manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
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
TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".feather"}
# Structured payloads: evidence whose shape is fields rather than prose or rows. This
# package does not extract them, so they stay classified-only unless a workspace
# configures a normalization adapter for the kind (see docs/research-yml.md).
# `.jsonl` classified as `table` before this kind existed, but nothing ever normalized
# it as one — only `.csv`/`.tsv` are tabular to the normalizer — so it belongs here.
STRUCTURED_DATA_EXTENSIONS = {".json", ".jsonl"}
TABLE_TEXT_EXTENSIONS = {".csv", ".tsv"}
LINK_EXTENSIONS = {".url", ".webloc"}
# Why a link-shaped file's URLs were not expanded into source records. `outside_link_root`
# means the file was never a parse candidate (a URL list under, say, `raw/data/`);
# `no_urls_parsed` means it was a candidate but yielded nothing usable.
LINK_PARSE_OUTSIDE_ROOT = "outside_link_root"
LINK_PARSE_NO_URLS = "no_urls_parsed"
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
# Fields the package writes into a sidecar but deliberately does not copy into the
# manifest. They are listed so a registered provider's own artifacts do not draw an
# "unknown provenance field" warning for fields this package authored:
#
#   provider_capabilities  the declaration is per-provider, not per-artifact; the sidecar
#                          is its system of record and `doctor` renders it. The manifest
#                          carries provider_registration, which joins to both, instead of
#                          duplicating the whole declaration on every row.
#   provider_metadata      plugin-supplied and untrusted. It is nested in the sidecar
#                          precisely so it cannot forge a policy field; promoting it into
#                          the manifest would hand it the authority that nesting withheld.
PROVENANCE_RECOGNIZED_ONLY_FIELDS = frozenset({"provider_capabilities", "provider_metadata"})
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
    "license_check_required",
    "provider_registration",
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
    "companions",
    "request_id",
    "candidate_id",
    "scope",
    "acquisition_run_id",
    "terms_url",
    "terms_note",
    "notes",
)
# Fields that bind a capture to the request and candidate it was authorised by. They
# are carried only on the record's primary `provenance`, because every consumer that
# reads them — delegated fulfilment correlation above all — asks a question with one
# answer: which request authorised this delivery. A record whose paired capture also
# supplied a request_id would offer two answers to that scalar question, so
# `additional_provenance` entries are stripped of them and correlation keeps reading
# the primary alone.
PROVENANCE_CORRELATION_FIELDS = frozenset({"request_id", "candidate_id"})
# Recognised fields that say nothing about where this capture came from. A sidecar
# carrying only these has no provenance in it, whatever else it does, and is told so --
# which is the whole of what "no usable fields" ever meant. Named explicitly because
# `companions` reaches `data` unread, so a malformed one would otherwise count as
# provenance simply by being present.
PROVENANCE_NON_PROVENANCE_FIELDS = frozenset({"companions"})
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
#: Most companion files one sidecar may declare beside the capture it describes.
#: A bound rather than a budget: a companion is a named part of one capture, and a
#: declaration long enough to need counting is describing a directory instead. No
#: global bound is added with it, because every declared companion is an ordinary
#: file under a raw root and so already counts toward the snapshot's existing
#: scope-guard entry and byte caps.
PROVENANCE_MAX_COMPANIONS = 8
#: Characters no bare file name may carry, matched on a declared companion before the
#: name is ever joined to a path. Unicode's two control ranges, C0 and C1: the NUL among
#: them cannot even be handed to the operating system -- ``lstat`` raises before it
#: reaches a syscall -- and the rest are characters no delivery writes into a file name
#: on purpose. Matched against the name exactly as declared, never a stripped copy of it.
PROVENANCE_COMPANION_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: The capture suffixes whose bytes each fingerprinting record kind re-reads. Held once
#: rather than twice, because the two questions that read it have to agree: which captures
#: `raw_fingerprint` covers, and which captures a companion declaration is worth accepting
#: on. A declaration the fingerprint could not carry would be a promise -- edit this and
#: normalization re-runs -- that the record has no way to keep.
RAW_FINGERPRINT_CAPTURE_SUFFIXES = {
    "pdf": PDF_EXTENSIONS,
    "html": HTML_EXTENSIONS,
    "table": TABLE_TEXT_EXTENSIONS,
    "structured_data": STRUCTURED_DATA_EXTENSIONS,
}
INVENTORY_REPORT_SCHEMA_VERSION = "1.0"
INVENTORY_REPORT_DOCUMENT_TYPE = "source_inventory_report"
ACQUISITION_INCOMPLETE_SUFFIX = ".acquisition-incomplete.json"
ACQUISITION_LOCK_RELATIVE = ("raw", ".locks", "acquisition.lock")

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _request_scope import normalize_scope
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
        help=(
            "Strict mode: refuse records that do not have provenance.checksum_verified=true. "
            "Asks this of the record's primary capture only."
        ),
    )
    parser.add_argument(
        "--reject-mismatch",
        action="store_true",
        help=(
            "Strict mode: refuse records whose provenance checksum is present but not verified, "
            "including the checksum of any secondary capture the record delivered."
        ),
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
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
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
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
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
        sample = path.read_text(encoding="utf-8", errors="ignore")[:8192].splitlines()
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
    if suffix in STRUCTURED_DATA_EXTENSIONS:
        return "structured_data"
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
        readme = json.loads(readme_path.read_text(encoding="utf-8"))
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
            sample = path.read_text(encoding="utf-8", errors="ignore")[:20000]
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


def bundle_file_count(bundle_dir: Path) -> int:
    """Count every regular file inside a LaTeX or arXiv bundle, dot-prefixed entries included.

    A bundle record declares exactly one ``raw_paths`` entry -- the bundle directory -- and
    no member list, so the whole subtree beneath it is the record's unit of admission. The
    controller expands that directory-shaped entry into every regular file beneath it with
    no skip predicate when it decides which delivered raw files a record accounts for, and
    the raw tree snapshot fingerprints one entry per regular file the same way. Filtering
    members through ``should_skip`` here measured a strict subset of that: a dot-prefixed
    file planted inside a delivered bundle was admitted under the record while
    ``metadata.file_count`` did not move, so the record's own account of how much evidence
    it holds disagreed with the tree it admits, and the disagreement was silent.

    "Regular file" here means what the snapshot means by it, checked the way the snapshot
    checks it: ``lstat`` rather than ``is_file``, a real regular file rather than a symlink
    to one, and a link count of exactly one. The snapshot refuses a symlink or a
    multiply-linked file rather than enumerating it, so an entry of either kind is not
    evidence this record admits and must not be measured as though it were. Counting them
    would put the same subset mismatch back that this function exists to remove, only with
    the excluded set on the other side. The exclusion tracks a real refusal rather than
    inventing a subset of its own: a bundle holding either entry sits under a raw source
    root, so the snapshot refuses the whole workspace rather than returning a smaller
    enumeration, and no count this function could state would make that tree deliverable.

    ``should_skip`` is deliberately not consulted, and equally deliberately not changed. It
    governs which paths become *records* -- dotfiles are not inventoried as separate sources
    -- and that is a different question from how much evidence a record admits. A bundle
    record declares the whole directory in ``raw_paths``, so every regular file beneath it
    is attributed to the record and has to be measured by it.

    What this closes is the count, not every way a bundle can hold more than it says.
    ``raw_fingerprint`` covers the paths ``raw_fingerprint_paths`` selects, which do filter
    through ``should_skip``, so a dot-prefixed member still reproduces a byte-identical
    fingerprint and triggers no re-normalization; and no member list exists anywhere in the
    record to hold a delivered bundle to its own contents. Both remain open on purpose:
    closing either needs an inventory-level member list, not a different predicate here.
    """
    count = 0
    for path in bundle_dir.rglob("*"):
        try:
            metadata = path.lstat()
        except OSError:
            # An entry that cannot be inspected is one the snapshot will refuse on its
            # own terms; it is not this count's business to decide that.
            continue
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if int(getattr(metadata, "st_nlink", 1) or 1) != 1:
            continue
        count += 1
    return count


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
        "file_count": bundle_file_count(bundle_dir),
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


def link_parse_status(record: dict[str, Any]) -> str:
    """Why a link-shaped file's URLs were not inventoried.

    Records written before this field existed default to the parse failure, which keeps
    their remediation exactly as it was.
    """
    metadata = record.get("metadata")
    value = metadata.get("link_parse_status") if isinstance(metadata, dict) else None
    return value if value in {LINK_PARSE_OUTSIDE_ROOT, LINK_PARSE_NO_URLS} else LINK_PARSE_NO_URLS


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
        text = path.read_text(encoding="utf-8", errors="ignore")
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


def local_repo_file_count(repo_dir: Path, *, limit: int | None = None) -> int:
    """Count every regular file under a local repository, dot-prefixed entries included.

    This count decides ``codebase_intake.bounded``, and the promise that flag makes is
    consumed by the controller's raw tree snapshot, which fingerprints one entry per regular
    file beneath the raw roots with no skip predicate and refuses the workspace once that
    enumeration passes its own 10,000-entry cap. Measuring the bound over a *subset* of what
    the snapshot walks makes the two caps incomparable: filtering through ``should_skip``
    here withheld every dot-prefixed path, so a checkout whose ``.git`` carried the
    difference was stamped bounded and then refused as unbounded, the refusal naming a tree
    the record had already declared admissible. ``.git`` is one of
    ``CODEBASE_LOCAL_REPO_MARKERS``, so the excluded subset was not exotic: it is what makes
    the tree a repository at all.

    "Regular file" here means what the snapshot means by it, checked the way the snapshot
    checks it: ``lstat`` rather than ``is_file``, a real regular file rather than a symlink
    to one, and a link count of exactly one. The snapshot refuses a symlink or a
    multiply-linked file rather than enumerating it, so an entry of either kind is not
    evidence this record admits and must not be measured as though it were.

    ``should_skip`` is deliberately not consulted, and equally deliberately not changed. It
    governs which paths become *records* -- dotfiles are not inventoried as separate sources
    -- and that is a different question from how much evidence a record admits. A local
    repository record declares the whole directory in ``raw_paths``, so every regular file
    beneath it is attributed to the record and has to be measured by it.

    What this closes is the subset mismatch, not every way the two limits can disagree. The
    snapshot's cap is over all configured raw roots combined while this one is per
    repository, so ``bounded`` remains a statement about one repository rather than a
    guarantee about the workspace: several repositories, or one repository beside enough
    other raw evidence, can still total past the snapshot's limit with every record
    correctly bounded. Reconciling that needs a workspace-wide accounting, not a different
    predicate here.
    """
    count = 0
    for path in repo_dir.rglob("*"):
        try:
            metadata = path.lstat()
        except OSError:
            # An entry that cannot be inspected is one the snapshot will refuse on its
            # own terms; it is not this count's business to decide that.
            continue
        # ``Path.is_file()`` resolves symlinks and accepts hardlinks, so it would count
        # entries the snapshot never enumerates: it refuses a symlink outright, and
        # refuses any regular file whose link count is not one. Counting them would put
        # the same subset mismatch back that this function exists to remove, only with
        # the excluded set on the other side.
        if not stat.S_ISREG(metadata.st_mode):
            continue
        if int(getattr(metadata, "st_nlink", 1) or 1) != 1:
            continue
        count += 1
        if limit is not None and count > limit:
            # Over the limit is all the caller can act on, so stop walking rather than
            # enumerate a tree that has already been refused.
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
    file_count = local_repo_file_count(repo_dir, limit=CODEBASE_MAX_LOCAL_REPO_FILES)
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
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return {}, [f"{relative_path}: malformed provenance sidecar: {exc}"]
    if not isinstance(document, dict):
        return {}, [f"{relative_path}: provenance sidecar must be a YAML mapping"]

    data: dict[str, Any] = {}
    unknown = set(document) - set(PROVENANCE_FIELDS) - PROVENANCE_RECOGNIZED_ONLY_FIELDS
    for key in sorted(str(key) for key in unknown):
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
        if field == "scope":
            # What this delivery claims to answer, matched against the fulfilled request's
            # own scope. Non-conforming entries are dropped rather than failing the
            # sidecar: an unusable scope key must not cost the workspace the whole record.
            parsed_scope = normalize_scope(value)
            if not parsed_scope:
                warnings.append(
                    f"{relative_path}: provenance scope must be a mapping of non-empty scalar values"
                )
                continue
            data[field] = parsed_scope
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
        if field == "companions":
            # Copied through unread, shape included. This function is handed the sidecar's
            # own path and nothing else, so it cannot stat a declared name, decide whether
            # it sits beside the capture, or know how many the record may carry -- and it
            # cannot report a refusal anywhere a consumer will see it, because the record
            # this sidecar belongs to has not been matched yet. Refusing a shape here
            # would drop the key against a warning that attaches to nothing, which reads
            # as a record that declared nothing at all. `resolve_declared_companions` has
            # the record in hand and owns every companion rule, this one included.
            data[field] = value
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
        if field == "provider_registration":
            # Which installed distribution supplied this source. Recorded so a manifest
            # reader can attribute evidence to a provider without opening every sidecar,
            # and so an uninstalled provider's past deliveries stay attributable.
            if isinstance(value, dict):
                data[field] = dict(value)
                continue
            warnings.append(f"{relative_path}: provenance provider_registration must be a mapping")
            continue
        if field == "license_check_required":
            if isinstance(value, bool):
                data[field] = value
                continue
            warnings.append(f"{relative_path}: provenance license_check_required must be a boolean")
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
    if not set(data) - PROVENANCE_NON_PROVENANCE_FIELDS:
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


def companion_admission_failure(path: Path) -> str | None:
    """Why the raw tree would refuse this companion, or None when it admits it.

    "Regular file" here means what the raw-tree snapshot means by it, checked the way the
    snapshot checks it: ``lstat`` rather than ``is_file``, a real regular file rather than
    a symlink to one, and a link count of exactly one. ``Path.is_file()`` resolves symlinks
    and accepts hardlinks, so it would admit two kinds of entry the snapshot refuses to
    enumerate at all -- and a record must not fingerprint bytes the tree declines to show.

    Refusing is the only answer this returns, including for a path the operating system
    will not be asked about at all: a name carrying a NUL raises ``ValueError`` out of
    ``lstat`` before any syscall, and letting that escape would end the whole inventory
    run over one line of acquirer-authored sidecar. Every caller here is deciding whether
    to admit one companion, and "this one cannot be inspected" is an answer all of them
    already handle.
    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "does not exist"
    except OSError as exc:
        return f"could not be inspected: {exc.strerror or exc}"
    except ValueError as exc:
        return f"could not be inspected: {exc}"
    if not stat.S_ISREG(metadata.st_mode):
        return "is not a regular file"
    if int(getattr(metadata, "st_nlink", 1) or 1) != 1:
        return "is a multiply linked file"
    return None


def resolve_declared_companions(
    project_root: Path,
    record: dict[str, Any],
    target_rel: str,
    sidecar_path: str,
    declared: Any,
    warnings: list[str],
) -> list[str] | None:
    """Resolve one sidecar's declared companions against the tree, dropping what fails.

    A companion is a second file a delivery names as part of the same capture -- the
    schema a payload is keyed on, say. Declaring it is the only way it can reach a record:
    ``should_skip`` refuses every dot-prefixed path component before the raw walk sees it,
    so a companion is never inventoried as a source of its own. What a surviving
    declaration buys is a place in the capture's ``raw_fingerprint``, so editing the
    companion re-triggers normalization of the record whose output depends on it.

    That is also the whole test of where a declaration may sit: on a file-shaped capture,
    on a record kind whose fingerprint covers that capture's bytes. A bundle record already
    admits its entire subtree, and a record the fingerprint does not reach has nothing a
    companion could move, so both are refused rather than recorded as an accepted list that
    quietly does nothing.

    Every rejection is a warning plus ``review_required``, and the entry is dropped rather
    than the sidecar failed: inventory does not hard-fail on sidecar content, and a dropped
    entry is one the record neither admits nor fingerprints. A declared-but-absent
    companion is the same -- the record simply carries fewer entries than the delivery
    claimed, which is a thing to review rather than a reason to lose the capture.

    None of this widens ``raw_paths``. A companion is part of one capture's evidence, not a
    source of its own, and adding it there would move both source identity and the
    raw-attribution equality the guards are written against.

    Every companion rule is here, the declaration's own shape included, because a rule
    applied where the record is out of reach can only warn at run level -- and a run-level
    warning attaches to nothing a consumer reads. A record whose declaration was refused
    for its shape and a record that declared nothing would then be byte-identical, so the
    acquirer would read a clean record and believe a companion the fingerprint never
    reached was being watched. ``None`` comes back for a value that is not a list of file
    names at all, which is the one answer the caller acts on rather than records.
    """

    def refuse(message: str) -> None:
        warning = f"{sidecar_path}: {message}"
        append_record_warning(record, warning)
        ensure_metadata(record)["review_required"] = True
        warnings.append(warning)

    # Not ``parse_string_list``, which strips each entry: that is right for a display
    # string and wrong for a file name, because ``str.strip()`` removes nine of the very
    # control characters refused below and would silently resolve a declaration to a
    # neighbouring file the delivery did not name. A name is read exactly as written, and
    # surrounding whitespace makes it a name that is simply not there.
    if not isinstance(declared, list) or not all(
        isinstance(name, str) and name.strip() for name in declared
    ):
        # Failed closed on the whole list rather than per entry: a declaration carrying
        # something that is not a file name is not a list this function can rule on, and
        # admitting the readable half would put a record's fingerprint somewhere between
        # what the delivery asked for and what it wrote.
        refuse("provenance companions must be a list of file names")
        return None
    if not declared:
        return []
    try:
        target_is_file = stat.S_ISREG((project_root / target_rel).lstat().st_mode)
    except OSError:
        target_is_file = False
    if not target_is_file:
        # A directory target is a bundle, and a bundle record already declares its whole
        # subtree in ``raw_paths``; every regular file beneath it is attributed to the
        # record already. A companion list there would name evidence the record admits
        # anyway, under wording that reads as though it had added some.
        refuse(f"provenance companions require a file-shaped target: {target_rel}")
        return []
    kind = record.get("kind")
    eligible_suffixes = RAW_FINGERPRINT_CAPTURE_SUFFIXES.get(kind if isinstance(kind, str) else "")
    if eligible_suffixes is None or PurePosixPath(target_rel).suffix.lower() not in eligible_suffixes:
        # What a companion buys is a place in `raw_fingerprint`, so a capture with no
        # fingerprint of its own has nothing to offer it. Accepting the declaration anyway
        # would write `companion_paths` onto a record whose fingerprint could never carry
        # it, and silence is the worst way to break that promise: the acquirer would read
        # a clean record and believe editing the companion re-triggers normalization.
        refuse(f"provenance companions are not carried by this record's fingerprint: {target_rel}")
        return []

    target_path = PurePosixPath(target_rel)
    directory = target_path.parent
    required_prefix = f".{target_path.name}."
    if len(declared) > PROVENANCE_MAX_COMPANIONS:
        refuse(
            f"provenance companions declares {len(declared)} entries, above the "
            f"{PROVENANCE_MAX_COMPANIONS}-entry limit; the entries past it are ignored"
        )
    accepted: list[str] = []
    for name in declared[:PROVENANCE_MAX_COMPANIONS]:
        # A control character is not part of any name a delivery means to write, and the
        # refusal is here rather than left to the admission check so that the name is
        # never joined to a path at all: `lstat` raises `ValueError` on an embedded NUL
        # instead of reporting a missing file, and a rule that depends on a downstream
        # `except` to hold is a rule stated in the wrong place. Reported as a repr because
        # the characters it names are the ones a warning cannot show.
        if PROVENANCE_COMPANION_CONTROL_RE.search(name):
            refuse(f"provenance companion must not contain control characters: {name!r}")
            continue
        # A bare file name, refused as such rather than normalized and then inspected for
        # traversal. A name carrying no separator at all cannot address anything outside
        # the directory it is joined to, so "resolves into the artifact's own directory"
        # holds by construction instead of by a check that has to be right.
        if "/" in name or "\\" in name or name in {".", ".."}:
            refuse(f"provenance companion must be a bare file name beside {target_rel}: {name}")
            continue
        if not name.startswith(required_prefix):
            # Dot-prefixed, and named after the capture it belongs to. The leading dot is
            # what keeps ``should_skip`` refusing it as a source in its own right; the
            # capture's own file name is what stops one capture's companion from being
            # read as a neighbouring capture's. Together they also put every companion
            # outside the set of paths any record can hold, since no record's path has a
            # dot-prefixed component.
            refuse(
                f"provenance companion must be named {required_prefix}<suffix> "
                f"beside {target_rel}: {name}"
            )
            continue
        relative = (directory / name).as_posix()
        if relative in {target_rel, sidecar_path}:
            refuse(f"provenance companion may not name the capture or its own sidecar: {name}")
            continue
        failure = companion_admission_failure(project_root / relative)
        if failure is not None:
            refuse(f"provenance companion {failure}: {relative}")
            continue
        if relative not in accepted:
            accepted.append(relative)
    return accepted


def merge_sidecar_provenance(
    project_root: Path,
    record: dict[str, Any],
    target_rel: str,
    entry: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Validate one sidecar's fields for the delivered path it sits beside.

    The checksum is verified against `target_rel` and nowhere else, which is why a
    second capture's provenance is never folded into the first one's mapping: a
    checksum only means anything next to the path whose bytes it was computed from.
    """
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
        target = project_root / target_rel
        if target.is_file():
            actual = hash_file_contents(target)
            verified = actual is not None and f"sha256:{actual}" == checksum
            provenance["checksum_verified"] = verified
            if not verified:
                warning = f"provenance checksum mismatch for {target_rel} (sidecar {entry['sidecar_path']})"
                append_record_warning(record, warning)
                ensure_metadata(record)["review_required"] = True
                warnings.append(warning)
        else:
            provenance["checksum_verified"] = False
            warning = f"{entry['sidecar_path']}: checksum cannot be verified for a directory target"
            append_record_warning(record, warning)
            warnings.append(warning)
    standards = provenance.get("standards")
    if isinstance(standards, dict) and standards.get("review_required") is True:
        warning = f"{entry['sidecar_path']}: standards metadata requires review"
        append_record_warning(record, warning)
        ensure_metadata(record)["review_required"] = True
        warnings.append(warning)
    if "companions" in provenance:
        # Two keys, and only one of them is load-bearing. `provenance` opens as a copy of
        # the whole parsed sidecar, so the *declared* list stays visible at `companions`
        # exactly as the delivery wrote it, and stays untrusted. `companion_paths` is the
        # package's own answer to that declaration and the only key a consumer reads --
        # the same division `sidecar_path` sits on, one line above.
        #
        # Entered on the key's presence rather than on its type, because a declaration of
        # the wrong shape is the one case that most needs an answer written onto the
        # record, and `resolve_declared_companions` is where every companion rule lives.
        admitted = resolve_declared_companions(
            project_root, record, target_rel, entry["sidecar_path"], provenance["companions"], warnings
        )
        if admitted is None:
            # Not a list of file names, so not a declared list either. Dropped rather than
            # shown: the key's whole contract is that a reader can see what the delivery
            # named, and arbitrary delivered YAML left under it would sit on every manifest
            # row claiming to be that. The refusal above is what the record shows instead.
            provenance.pop("companions")
            admitted = []
        provenance["companion_paths"] = admitted
    return provenance


def additional_provenance_entry(target_rel: str, provenance: dict[str, Any]) -> dict[str, Any]:
    """One secondary capture's provenance, named by the path it describes."""
    entry: dict[str, Any] = {"path": target_rel}
    for field, value in provenance.items():
        if field in PROVENANCE_CORRELATION_FIELDS:
            continue
        entry[field] = value
    return entry


def apply_provenance_sidecars(
    project_root: Path,
    records: list[dict[str, Any]],
    sidecars: dict[str, dict[str, Any]],
) -> list[str]:
    """Merge sidecar provenance into matching records and verify checksums.

    A record can own more than one delivered path — inventory folds a paired PDF into
    the LaTeX bundle record that describes the same paper, so one manifest row carries
    two captures that were retrieved separately and hash differently. The first
    matching sidecar becomes the record's `provenance`; every other matching sidecar
    becomes an `additional_provenance` entry rather than being discarded, because the
    capture it describes was delivered and its origin, retrieval time, and checksum are
    the only record of where those bytes came from.
    """
    warnings: list[str] = []
    matched: set[str] = set()
    for record in records:
        candidates = provenance_candidate_paths(record)
        primary = next((candidate for candidate in candidates if candidate in sidecars), None)
        if primary is None:
            continue
        matched.add(primary)
        merged = merge_sidecar_provenance(
            project_root, record, primary, sidecars[primary], warnings
        )
        # Named by the path it describes, exactly as every secondary capture is. A record
        # can own several delivered paths and the primary is whichever of them a sidecar
        # matched first, which is not derivable from the record afterwards: `latex_root`
        # is not in `raw_paths`, so a consumer reading `checksum_verified` off a bundle
        # record could not say which capture had been verified.
        merged["path"] = primary
        record["provenance"] = merged
        additional: list[dict[str, Any]] = []
        for extra in candidates:
            if extra == primary or extra not in sidecars:
                continue
            matched.add(extra)
            additional.append(
                additional_provenance_entry(
                    extra,
                    merge_sidecar_provenance(project_root, record, extra, sidecars[extra], warnings),
                )
            )
        if additional:
            record["additional_provenance"] = additional
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


def record_companion_paths(record: dict[str, Any]) -> list[str]:
    """The companions a record's primary provenance actually admits.

    Only ``companion_paths`` is read, never the declared ``companions`` beside it: the
    first is what inventory resolved against the tree, the second is what the delivery
    asked for and is never acted on.

    The primary provenance is the whole answer for every kind this is asked about. A
    record owns more than one delivered path in exactly two shapes -- a paper bundle
    paired with its PDF, and a link record merging several link files -- and neither is a
    kind whose fingerprint companions reach.
    """
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        return []
    companions = provenance.get("companion_paths")
    if not isinstance(companions, list):
        return []
    return [path for path in companions if isinstance(path, str)]


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
    elif kind in RAW_FINGERPRINT_CAPTURE_SUFFIXES:
        eligible_suffixes = RAW_FINGERPRINT_CAPTURE_SUFFIXES[kind]
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
        # Appended outside the suffix filter above, which selects which *captures*
        # a record re-reads and would drop every companion on sight: a companion is named
        # by its capture's sidecar rather than found by its own extension, so a `.json`
        # schema beside a `.csv` matches none of them. Every other kind is absent because
        # no declaration survives onto one: `resolve_declared_companions` reads this same
        # map and refuses a companion the fingerprint here could not carry.
        #
        # This is what `raw_fingerprint_paths` is for rather than a widening of it: a
        # declared companion the normalizer keys its structured view on is a raw file whose
        # bytes determine the record's normalized output, which is the sentence at the top
        # of this function. The undeclared dot-prefixed member of a bundle stays outside,
        # and stays outside for the same reason -- nothing declared it.
        for relative in record_companion_paths(record):
            companion = project_root / relative
            if companion_admission_failure(companion) is None:
                paths.append(companion)
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
        if kind == "link":
            # A link-shaped file reaches this loop only when its URLs were *not* expanded
            # into source records, and the two ways that happens need opposite fixes: a
            # file inside a link root yielded no usable URLs, while one outside a link
            # root was never a parse candidate and is simply in the wrong place. Record
            # which it was, so the report can say so instead of guessing.
            ensure_metadata(records[-1])["link_parse_status"] = (
                LINK_PARSE_NO_URLS if is_link_parse_candidate(path, raw_root) else LINK_PARSE_OUTSIDE_ROOT
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
    tmp_path.write_text(content, encoding="utf-8", newline="\n")
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
    # These need opposite fixes, and naming the wrong one sends the operator away from
    # the remedy: a misplaced URL list is not malformed, and moving it *out* of a link
    # root is the reverse of what it needs.
    if any(link_parse_status(record) == LINK_PARSE_OUTSIDE_ROOT for record in raw_link_records):
        actions.append(
            "Move link files under a link root (for example raw/links/) or rename them to "
            ".url/.webloc; their URLs are not inventoried as sources where they are."
        )
    if any(link_parse_status(record) == LINK_PARSE_NO_URLS for record in raw_link_records):
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


def unverified_additional_capture_paths(record: dict[str, Any]) -> list[str]:
    """Paths of the record's secondary captures whose checksum is present but unverified.

    A record can deliver more than one capture — a paired paper's LaTeX bundle and its
    PDF, a link URL harvested from two link files — and each `additional_provenance`
    entry carries the checksum of its own path. A checksum that is present and did not
    verify is a mismatch wherever it sits, so a mismatch-rejecting run has to read these
    entries too. An *absent* checksum is a different question and is deliberately not
    reported here; `strict_checksum_refusals` says why it stays on the primary.
    """
    additional = record.get("additional_provenance")
    if not isinstance(additional, list):
        return []
    paths: list[str] = []
    for entry in additional:
        if not isinstance(entry, dict):
            continue
        if not isinstance(entry.get("checksum"), str) or entry.get("checksum_verified") is True:
            continue
        # Every entry is built by additional_provenance_entry, which always names a path.
        paths.append(str(entry.get("path")))
    return paths


def strict_checksum_refusals(
    records: list[dict[str, Any]],
    *,
    require_checksum: bool,
    reject_mismatch: bool,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    """Split records into those the strict modes admit and the refusals they raise.

    The two modes ask different questions, and only one of them reaches past the
    record's primary capture. `reject_mismatch` asks whether any checksum the record
    carries failed against the bytes beside it, which is positive evidence about a
    specific capture and so is asked of every capture the record delivered.
    `require_checksum` asks whether a verified checksum is present at all, which is
    evidence about nothing in particular, and is asked of the primary capture alone:
    a secondary capture may legitimately arrive without a checksum, and a primary that
    is a bundle root can never be verified, so demanding one everywhere would refuse
    correct deliveries for an absence.
    """
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

        record_refusals: list[dict[str, str]] = []
        # The primary capture names its path too now. It could not before: a record may own
        # several delivered paths and nothing recorded which one the primary provenance
        # described, so these refusals named only the record -- ambiguous for exactly the
        # multi-capture records the mismatch arm exists to catch.
        primary_path = provenance.get("path") if isinstance(provenance.get("path"), str) else None
        primary_named = f" capture {primary_path}" if primary_path else ""
        if reject_mismatch and checksum_present and not checksum_verified:
            refusal = {
                "source_id": record_id,
                "reason": "checksum_mismatch",
                "message": (
                    f"strict checksum refusal: {record_id}{primary_named} "
                    "checksum is present but not verified"
                ),
            }
            if primary_path:
                refusal["path"] = primary_path
            record_refusals.append(refusal)
        elif require_checksum and not checksum_verified:
            refusal = {
                "source_id": record_id,
                "reason": "checksum_required",
                "message": f"strict checksum refusal: {record_id}{primary_named} missing verified checksum",
            }
            if primary_path:
                refusal["path"] = primary_path
            record_refusals.append(refusal)
        if reject_mismatch:
            # Whatever the primary did, a secondary capture may still have mismatched,
            # and the operator needs the path of each one that did.
            record_refusals.extend(
                {
                    "source_id": record_id,
                    "reason": "checksum_mismatch",
                    "path": path,
                    "message": (
                        f"strict checksum refusal: {record_id} capture {path} "
                        "checksum is present but not verified"
                    ),
                }
                for path in unverified_additional_capture_paths(record)
            )

        if not record_refusals:
            kept.append(record)
            continue

        warnings.extend(refusal["message"] for refusal in record_refusals)
        refusals.extend(record_refusals)
    return kept, warnings, refusals


def strict_refusal_error_code(refusals: list[dict[str, str]]) -> str:
    if any(refusal.get("reason") == "checksum_mismatch" for refusal in refusals):
        return INVENTORY_CHECKSUM_MISMATCH
    return INVENTORY_CHECKSUM_REQUIRED


def strict_refusal_message(refusals: list[dict[str, str]]) -> str:
    # One record can raise a refusal per offending capture; the count is of sources.
    count = len(unique_values([refusal["source_id"] for refusal in refusals]))
    noun = "source" if count == 1 else "sources"
    reasons = {refusal.get("reason") for refusal in refusals}
    if "checksum_mismatch" in reasons:
        return f"strict checksum mode refused {count} {noun} with unverified provenance checksums."
    return f"strict checksum mode refused {count} {noun} missing verified checksums."


def strict_refusal_details(refusals: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "source_ids": unique_values([refusal["source_id"] for refusal in refusals]),
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
