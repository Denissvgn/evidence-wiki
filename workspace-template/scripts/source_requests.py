#!/usr/bin/env python3
"""Manage the structured source-request artifact for fetch agents.

When research stalls on missing evidence, scout output and blocked questions
become structured requests in ``sources/source-requests.jsonl`` (configurable
via ``sources.source_requests_path``). Fetch agents consume open requests,
deliver files into ``raw/`` per ``docs/source-delivery.md``, and mark requests
fulfilled with the manifest record that satisfied them. This closes the
acquisition loop without the workspace doing any fetching itself.

Each JSONL line is one request record (schema version 1.0)::

    {
      "schema_version": "1.0",
      "request_id": "req-1a2b3c4d5e",
      "kind": "paper",                    # paper|dataset|web|code|other
      "query_or_identifier": "arXiv:2601.00001",
      "rationale": "Blocks the benchmark question.",
      "priority": "high",                 # high|medium|low
      "question_slugs": ["which-benchmarks"],
      "status": "open",                   # open|fulfilled
      "created_at": "2026-06-10T12:00:00Z",
      "updated_at": "2026-06-10T12:00:00Z",
      "source_id": null                    # manifest id set on fulfill
    }

Subcommands:

- ``add``: append one validated request. Referenced ``--question-slug`` values
  must exist as question pages. Re-adding the same kind plus query while an
  open request exists is an idempotent no-op reported as a duplicate.
- ``list``: read-only listing with optional repeatable ``--status`` filters.
- ``fulfill``: link a delivered manifest ``--source-id`` to a request and set
  ``status: fulfilled``. Fulfilling an already-fulfilled request with the same
  source id is a no-op; with a different source id it is an error.

The file is single-writer by design: ``add`` and ``fulfill`` serialize through
the shared workspace lock helper while preserving atomic append/replace writes.
Concurrent readers always see complete lines.

Exit codes:

- ``0``: success (including duplicate-add and same-source refulfill no-ops).
- ``2``: validation error, unknown request/source id, malformed artifact, or
  unreadable workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc


SCHEMA_VERSION = "1.0"
DEFAULT_REQUESTS_PATH = "sources/source-requests.jsonl"
REQUEST_KINDS = ("paper", "dataset", "web", "code", "other")
REQUEST_PRIORITIES = ("high", "medium", "low")
REQUEST_STATUSES = ("open", "fulfilled")
EXIT_OK = 0
EXIT_INVALID = 2
LOG_HEADER = "# Research Wiki Activity Log\n\n"
ARXIV_ID_RE = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", re.IGNORECASE)
ARXIV_VERSIONED_ID_RE = re.compile(r"^\d{4}\.\d{4,5}v\d+$", re.IGNORECASE)
DOI_RE = re.compile(r"^10\.\S+/.+", re.IGNORECASE)
OPENALEX_WORK_ID_RE = re.compile(r"\b(W\d+)\b", re.IGNORECASE)
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError, workspace_lock


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage structured source requests for fetch agents.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Record one open source request.")
    add_parser.add_argument("--kind", required=True, choices=REQUEST_KINDS, help="Requested evidence kind.")
    add_parser.add_argument(
        "--query-or-identifier",
        "--query",
        dest="query_or_identifier",
        required=True,
        help="What to fetch: arXiv ID, DOI, URL, or a search query.",
    )
    add_parser.add_argument(
        "--rationale",
        required=True,
        help="Why the evidence is needed, tied to workspace gaps.",
    )
    add_parser.add_argument(
        "--priority",
        choices=REQUEST_PRIORITIES,
        default="medium",
        help="Request priority. Defaults to medium.",
    )
    add_parser.add_argument(
        "--question-slug",
        action="append",
        dest="question_slugs",
        default=None,
        help="Slug of a question page this request unblocks. Repeatable.",
    )
    add_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )

    list_parser = subparsers.add_parser("list", help="List recorded source requests.")
    list_parser.add_argument(
        "--status",
        action="append",
        choices=REQUEST_STATUSES,
        default=None,
        help="Only include requests with the given status. Repeatable.",
    )
    list_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. Defaults to text.",
    )

    fulfill_parser = subparsers.add_parser("fulfill", help="Mark a request fulfilled by a delivered source.")
    fulfill_parser.add_argument("--request-id", required=True, help="Request to fulfill.")
    fulfill_parser.add_argument(
        "--source-id",
        required=True,
        help="Manifest source id of the delivered evidence.",
    )
    fulfill_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )

    plan_parser = subparsers.add_parser("plan-fetch", help="Plan provider commands for one source request.")
    plan_parser.add_argument("--request-id", required=True, help="Open source request to plan.")
    plan_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )
    return parser.parse_args(argv)


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


def validate_generated_sources_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"research.yml {label} must be a non-empty workspace-relative path")
    normalized = value.strip().replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not a URL: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path without '..': {value}")
    relative = path.as_posix()
    if relative != "sources" and not relative.startswith("sources/"):
        raise SystemExit(f"research.yml {label} must be under the generated sources/ directory: {value}")
    return relative


def requests_path(project_root: Path, config: dict[str, Any]) -> Path:
    sources_config = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    value = sources_config.get("source_requests_path", DEFAULT_REQUESTS_PATH)
    return project_root / validate_generated_sources_path(value, "sources.source_requests_path")


def integrations_config(config: dict[str, Any]) -> dict[str, Any]:
    integrations = config.get("integrations")
    return integrations if isinstance(integrations, dict) else {}


def load_requests(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL in {path}:{line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise SystemExit(f"Invalid request record in {path}:{line_number}: expected JSON object")
        for field in ("created_at", "updated_at"):
            normalized = normalize_timestamp_utc(record.get(field))
            if normalized is not None:
                record[field] = normalized
        records.append(record)
    return records


def normalize_query(value: str) -> str:
    return " ".join(value.split()).casefold()


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_timestamp_utc(value: Any) -> str | None:
    parsed = parse_timestamp_utc(value)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed is not None else None


def request_time_sort_key(record: dict[str, Any]) -> tuple[datetime, str]:
    parsed = parse_timestamp_utc(record.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc)
    return parsed, str(record.get("request_id", ""))


def generate_request_id(kind: str, query: str, created_at: str, existing_count: int) -> str:
    material = f"{kind}\n{normalize_query(query)}\n{created_at}\n{existing_count}"
    return "req-" + hashlib.sha1(material.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


def questions_directory(project_root: Path, config: dict[str, Any]) -> Path:
    wiki_config = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    wiki_root = wiki_config.get("root") if isinstance(wiki_config.get("root"), str) else "wiki"
    return project_root / wiki_root / "questions"


def validate_question_slugs(project_root: Path, config: dict[str, Any], slugs: list[str]) -> list[str]:
    questions_dir = questions_directory(project_root, config)
    cleaned: list[str] = []
    for slug in slugs:
        value = slug.strip()
        if not value:
            raise SystemExit("--question-slug must be a non-empty slug")
        if not (questions_dir / f"{value}.md").is_file():
            try:
                label = questions_dir.relative_to(project_root).as_posix()
            except ValueError:
                label = questions_dir.as_posix()
            raise SystemExit(f"Unknown question slug: {value} (no page under {label}/)")
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def manifest_source_ids(project_root: Path, config: dict[str, Any]) -> set[str]:
    sources_config = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    manifest_value = sources_config.get("manifest_path", "sources/manifest.jsonl")
    manifest = project_root / validate_generated_sources_path(manifest_value, "sources.manifest_path")
    ids: set[str] = set()
    if not manifest.is_file():
        return ids
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            ids.add(record["id"])
    return ids


def render_request_line(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def source_requests_lock_path(path: Path) -> Path:
    return path.parent / ".locks" / "source-requests.lock"


def _append_request_unlocked(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(render_request_line(record) + "\n")


def append_request(path: Path, record: dict[str, Any]) -> None:
    """Append one request line under the workspace mutation lock."""
    with workspace_lock(source_requests_lock_path(path), purpose="source request mutation"):
        _append_request_unlocked(path, record)


def _write_requests_unlocked(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(render_request_line(record) + "\n" for record in records)
    # Unique temp name so concurrent writers cannot steal each other's temp file;
    # the final rename stays atomic on POSIX (same filesystem).
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def write_requests(path: Path, records: list[dict[str, Any]]) -> None:
    with workspace_lock(source_requests_lock_path(path), purpose="source request mutation"):
        _write_requests_unlocked(path, records)


def append_log_entry(log_path: Path, entry: str) -> None:
    """Append a rendered log entry under the shared workspace lock."""
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


def request_summary(record: dict[str, Any]) -> str:
    question_slugs = record.get("question_slugs")
    slugs = ", ".join(question_slugs) if isinstance(question_slugs, list) and question_slugs else "none"
    return (
        f"[{record.get('priority', '?')}] {record.get('request_id', '?')} ({record.get('kind', '?')}, "
        f"{record.get('status', '?')}): {record.get('query_or_identifier', '?')} | questions: {slugs}"
    )


def find_open_duplicate(records: list[dict[str, Any]], kind: str, query: str) -> dict[str, Any] | None:
    normalized = normalize_query(query)
    for record in records:
        if (
            record.get("status") == "open"
            and record.get("kind") == kind
            and isinstance(record.get("query_or_identifier"), str)
            and normalize_query(record["query_or_identifier"]) == normalized
        ):
            return record
    return None


def acquisition_plan_context(config: dict[str, Any]) -> dict[str, Any]:
    acquisition = integrations_config(config).get("acquisition")
    if not isinstance(acquisition, dict):
        return {"enabled": False, "providers": [], "target_root": None}
    providers = acquisition.get("providers")
    if isinstance(providers, list):
        provider_ids = [provider for provider in providers if isinstance(provider, str)]
    else:
        provider_ids = []
    return {
        "enabled": acquisition.get("enabled") is True,
        "providers": provider_ids,
        "target_root": acquisition.get("target_root") if isinstance(acquisition.get("target_root"), str) else None,
    }


def provider_allowed(acquisition: dict[str, Any], provider: str) -> bool:
    return bool(acquisition.get("enabled")) and provider in (acquisition.get("providers") or [])


def command_route(
    *,
    provider: str,
    route: str,
    confidence: str,
    reason: str,
    allowed_by_config: bool,
    command_argv: list[str],
) -> dict[str, Any]:
    return {
        "provider": provider,
        "route": route,
        "confidence": confidence,
        "reason": reason,
        "allowed_by_config": allowed_by_config,
        "command": shlex.join(command_argv),
        "command_argv": command_argv,
    }


def arxiv_id_from_query(value: str) -> tuple[str, bool] | None:
    candidate = value.strip()
    if candidate.lower().startswith("arxiv:"):
        candidate = candidate.split(":", 1)[1].strip()
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower().endswith("arxiv.org"):
        candidate = unquote(parsed.path.rsplit("/", 1)[-1])
        if candidate.lower().endswith(".pdf"):
            candidate = candidate[:-4]
    match = ARXIV_ID_RE.search(candidate)
    if match is None:
        return None
    arxiv_id = match.group(0).lower()
    return arxiv_id, ARXIV_VERSIONED_ID_RE.match(arxiv_id) is not None


def doi_from_query(value: str) -> str | None:
    candidate = value.strip()
    if candidate.lower().startswith("doi:"):
        candidate = candidate.split(":", 1)[1].strip()
    else:
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {"doi.org", "dx.doi.org"}:
            candidate = unquote(parsed.path.lstrip("/"))
    return candidate if DOI_RE.match(candidate) else None


def openalex_work_id_from_value(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    match = OPENALEX_WORK_ID_RE.search(value.strip())
    return match.group(1).upper() if match else None


def paper_metadata(candidate: dict[str, Any]) -> dict[str, Any] | None:
    value = candidate.get("paper")
    return value if isinstance(value, dict) else None


def paper_provider_id(paper: dict[str, Any], provider: str) -> str | None:
    provider_ids = paper.get("provider_ids")
    if isinstance(provider_ids, dict):
        value = provider_ids.get(provider)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def paper_arxiv_match(paper: dict[str, Any]) -> tuple[str, bool] | None:
    for value in (paper.get("arxiv_id"), paper_provider_id(paper, "arxiv")):
        if isinstance(value, str) and value.strip():
            match = arxiv_id_from_query(value)
            if match is not None:
                return match
    return None


def paper_openalex_work_id(paper: dict[str, Any]) -> str | None:
    for value in (paper_provider_id(paper, "openalex"), paper.get("landing_page_url")):
        work_id = openalex_work_id_from_value(value)
        if work_id is not None:
            return work_id
    return None


def paper_doi(paper: dict[str, Any]) -> str | None:
    for value in (paper.get("doi"), paper_provider_id(paper, "doi")):
        if isinstance(value, str) and value.strip():
            doi = doi_from_query(value)
            if doi is not None:
                return doi
    return None


ACQUISITION_DISABLED_WARNING = (
    "Acquisition is disabled; do not run provider fetch commands until explicitly enabled."
)
UNSUPPORTED_KIND_WARNING = "No provider-backed plan is available for this kind; use manual delivery."
PROVIDER_NOT_ALLOWLISTED_PREFIX = (
    "Provider route is not allow-listed by integrations.acquisition.providers: "
)


def plan_warnings(acquisition: dict[str, Any], routes: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    if not acquisition.get("enabled"):
        warnings.append(ACQUISITION_DISABLED_WARNING)
    disabled = sorted({route["provider"] for route in routes if not route["allowed_by_config"]})
    if acquisition.get("enabled") and disabled:
        warnings.append(PROVIDER_NOT_ALLOWLISTED_PREFIX + ", ".join(disabled))
    return warnings


# --- Selected-candidate routes (E36-T02) -------------------------------------
# `plan-fetch` includes acquisition guidance for discovery candidates that were
# explicitly selected for the request (discover_sources.py candidates select,
# E36-T01). A selected candidate carries status "selected" and
# selected_for_request_id == the request id (legacy selected_request_id is still
# accepted). Planning stays read-only: it suggests
# the exact provider command (or manual-delivery target) for each candidate type
# and never fetches.
CANDIDATE_STORE_RELATIVE = ("sources", "discovery", "candidates.jsonl")
DEFAULT_COVERAGE_DIR = "sources/coverage"
# Trust tiers ordered best (rank 0) to worst; mirrors docs/source-discovery.md.
TRUST_TIERS = (
    "official_primary",
    "primary_non_official",
    "secondary_reputable",
    "secondary_unknown",
    "unsafe_or_unusable",
)
TRUST_TIER_RANK = {tier: index for index, tier in enumerate(TRUST_TIERS)}
# Current discovery records may use the finer OEH vocabulary documented in
# source-discovery.md. Map it onto the same policy ordering so plan-fetch does
# not misclassify an academic-primary record as an unknown/worst tier.
TRUST_TIER_RANK.update(
    {
        "official_secondary": 1,
        "academic_primary": 1,
        "vendor_primary": 1,
        "implementation_primary": 1,
        "aggregator": 2,
        "unknown": 3,
        "rejected": 4,
    }
)
# A selected candidate at a worse tier than this triggers a review warning. A
# request may override it with an optional `min_trust_tier` field.
DEFAULT_MIN_TRUST_TIER = "secondary_reputable"
SOURCE_POLICY_MIN_TRUST_TIER = {
    "official_primary": "official_primary",
    "official_vendor": "official_primary",
    "official_standards_registry": "official_primary",
    "standards_body_primary": "official_primary",
    "canonical_repository": "official_primary",
    "primary_or_official": "primary_non_official",
    "academic_indexed": "secondary_reputable",
    "openalex_or_arxiv": "secondary_reputable",
    "domain_pack_allowed": "secondary_reputable",
    "manual_review_required": "secondary_unknown",
}
CANDIDATE_POLICY_DEFAULTS = {
    "paper": {
        "evidence_path": "academic_method_existence",
        "source_policy": "academic_indexed",
        "freshness_policy": "publication_identity",
        "identity_policy": "citation_id_resolves",
    },
    "code_repository": {
        "evidence_path": "github_implementation",
        "source_policy": "canonical_repository",
        "freshness_policy": "release_snapshot",
        "identity_policy": "repo_ref_resolves",
    },
    "official_legal": {
        "evidence_path": "legal_current_figure",
        "source_policy": "official_primary",
        "freshness_policy": "current_legal_figure",
        "identity_policy": "official_domain_match",
    },
    "standards_registry_entry": {
        "evidence_path": "standards_registry_reference",
        "source_policy": "official_standards_registry",
        "freshness_policy": "current_standard_reference",
        "identity_policy": "standard_designation_matches_registry",
    },
    "harmonised_standard_reference": {
        "evidence_path": "standards_registry_reference",
        "source_policy": "official_standards_registry",
        "freshness_policy": "current_product_requirement",
        "identity_policy": "registry_entry_matches_product_requirement",
    },
    "product_requirement_guidance": {
        "evidence_path": "product_requirement_profile",
        "source_policy": "official_primary",
        "freshness_policy": "current_product_requirement",
        "identity_policy": "official_domain_match",
    },
    "geospatial_standard_register_entry": {
        "evidence_path": "standards_registry_reference",
        "source_policy": "official_standards_registry",
        "freshness_policy": "current_standard_reference",
        "identity_policy": "standard_designation_matches_registry",
    },
    "official_web": {
        "evidence_path": "vendor_product_spec",
        "source_policy": "official_primary",
        "freshness_policy": "current_product_spec",
        "identity_policy": "official_domain_match",
    },
    "dataset": {
        "evidence_path": "academic_method_existence",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "publisher_page": {
        "evidence_path": "academic_method_existence",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "supplemental_material": {
        "evidence_path": "academic_method_existence",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "project_page": {
        "evidence_path": "vendor_product_spec",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
    "web_page": {
        "evidence_path": "vendor_product_spec",
        "source_policy": "manual_review_required",
        "freshness_policy": "manual_review",
        "identity_policy": "origin_url_matches_candidate",
    },
}
DEFAULT_CANDIDATE_POLICY = CANDIDATE_POLICY_DEFAULTS["web_page"]
# Manual-delivery target root by candidate source_type (docs/source-delivery.md).
MANUAL_DELIVERY_ROOT = {
    "paper": "raw/papers/",
    "publisher_page": "raw/web/",
    "official_web": "raw/web/",
    "official_legal": "raw/links/",
    "standards_registry_entry": "raw/web/",
    "harmonised_standard_reference": "raw/web/",
    "product_requirement_guidance": "raw/web/",
    "geospatial_standard_register_entry": "raw/web/",
    "dataset": "raw/data/",
    "project_page": "raw/web/",
    "supplemental_material": "raw/other/",
    "web_page": "raw/links/",
}
WEB_FETCH_SOURCE_TYPES = {
    "official_legal",
    "official_web",
    "publisher_page",
    "project_page",
    "web_page",
    "standards_registry_entry",
    "harmonised_standard_reference",
    "product_requirement_guidance",
    "geospatial_standard_register_entry",
}


def candidate_store_path(project_root: Path, config: dict[str, Any] | None = None) -> Path:
    default = "/".join(CANDIDATE_STORE_RELATIVE)
    integrations = config.get("integrations") if isinstance(config, dict) else {}
    integrations = integrations if isinstance(integrations, dict) else {}
    discovery = integrations.get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    value = discovery.get("candidate_store_path", default)
    relative = validate_generated_sources_path(value, "integrations.discovery.candidate_store_path")
    if not relative.lower().endswith(".jsonl"):
        raise SystemExit("integrations.discovery.candidate_store_path must use the .jsonl extension")
    return project_root / relative


def coverage_dir_path(project_root: Path, config: dict[str, Any]) -> Path:
    sources_config = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    value = sources_config.get("coverage_dir", DEFAULT_COVERAGE_DIR)
    return project_root / validate_generated_sources_path(value, "sources.coverage_dir")


def selected_candidate_request_id(candidate: dict[str, Any]) -> str | None:
    for key in ("selected_for_request_id", "selected_request_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_policy_defaults(source_type: Any) -> dict[str, str]:
    if isinstance(source_type, str):
        return CANDIDATE_POLICY_DEFAULTS.get(source_type, DEFAULT_CANDIDATE_POLICY)
    return DEFAULT_CANDIDATE_POLICY


def candidate_policy_fields(candidate: dict[str, Any]) -> dict[str, str]:
    defaults = candidate_policy_defaults(candidate.get("source_type"))
    fields: dict[str, str] = {}
    for key, default in defaults.items():
        value = candidate.get(key)
        fields[key] = value.strip() if isinstance(value, str) and value.strip() else default
    return fields


def load_selected_candidates(
    project_root: Path,
    config: dict[str, Any],
    request_id: str,
) -> list[dict[str, Any]]:
    """Return discovery candidates selected for this request (status "selected"
    and selected_for_request_id == request_id, accepting legacy selected_request_id).
    Read-only and lenient: plan-fetch never rewrites the store, so a malformed
    line is skipped rather than fatal."""
    path = candidate_store_path(project_root, config)
    if not path.exists():
        return []
    selected: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("status") == "selected" and selected_candidate_request_id(record) == request_id:
            selected.append(record)
    return selected


def candidate_string_list(candidate: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        value = candidate.get(key)
        if isinstance(value, list):
            return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def selected_candidate_is_official_web(candidate: dict[str, Any], source_type: Any, url: str) -> bool:
    if not url or source_type not in WEB_FETCH_SOURCE_TYPES:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    return (
        candidate.get("official_source") is True
        or candidate.get("trust_tier") == "official_primary"
        or source_type in {"official_legal", "official_web"}
    )


def append_optional_arg(argv: list[str], flag: str, value: Any) -> None:
    if isinstance(value, str) and value.strip():
        argv.extend([flag, value.strip()])


def web_candidate_command(candidate: dict[str, Any], request_id: str, url: str, source_type: Any) -> list[str]:
    argv = [
        "python3",
        "scripts/fetch_sources.py",
        "--format",
        "json",
        "web",
        "get",
        "--url",
        url,
        "--request-id",
        request_id,
    ]
    append_optional_arg(argv, "--candidate-id", candidate.get("candidate_id"))
    append_optional_arg(argv, "--source-type", source_type)
    append_optional_arg(argv, "--publisher", candidate.get("publisher"))
    append_optional_arg(argv, "--jurisdiction", candidate.get("jurisdiction"))
    append_optional_arg(argv, "--terms-url", candidate.get("terms_url"))
    for area in candidate_string_list(candidate, "evidence_areas", "supported_evidence_areas"):
        argv.extend(["--evidence-area", area])
    return argv


def is_github_url(url: str) -> bool:
    host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    return host == "github.com" or host.endswith(".github.com")


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def policy_min_trust_tier(source_policy: Any) -> str:
    if isinstance(source_policy, str):
        return SOURCE_POLICY_MIN_TRUST_TIER.get(source_policy, DEFAULT_MIN_TRUST_TIER)
    return DEFAULT_MIN_TRUST_TIER


def request_min_trust_tier(request: dict[str, Any]) -> str:
    threshold = request.get("min_trust_tier")
    if threshold not in TRUST_TIER_RANK:
        threshold = DEFAULT_MIN_TRUST_TIER
    return threshold


def trust_tier_below(tier: Any, threshold: str) -> bool:
    return TRUST_TIER_RANK.get(tier, len(TRUST_TIERS)) > TRUST_TIER_RANK.get(threshold, len(TRUST_TIERS))


def strictest_min_trust_tier(facets: list[dict[str, Any]], fallback: str | None = None) -> str | None:
    tiers = [
        facet["policy_min_trust_tier"]
        for facet in facets
        if facet.get("policy_min_trust_tier") in TRUST_TIER_RANK
    ]
    if not tiers:
        return fallback
    return min(tiers, key=lambda tier: TRUST_TIER_RANK[tier])


def coverage_manifest_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    paths = [*directory.rglob("*.yml"), *directory.rglob("*.yaml")]
    return sorted({path for path in paths if path.is_file()}, key=lambda path: path.as_posix())


def coverage_policy_facet_summary(path: Path, project_root: Path, document: dict[str, Any], facet: dict[str, Any]) -> dict[str, Any] | None:
    request_ids = string_list(facet.get("blocking_request_ids"))
    evidence_path = facet.get("evidence_path")
    source_policy = facet.get("source_policy")
    facet_id = facet.get("facet_id")
    if not isinstance(evidence_path, str) or not evidence_path.strip():
        return None
    if not isinstance(source_policy, str) or not source_policy.strip():
        return None
    if not isinstance(facet_id, str) or not facet_id.strip():
        return None
    min_sources = facet.get("min_sources")
    if not isinstance(min_sources, int):
        min_sources = 0
    return {
        "coverage_manifest": relative_label(project_root, path),
        "question_slug": document.get("question_slug"),
        "facet_id": facet_id.strip(),
        "description": facet.get("description"),
        "required": facet.get("required") is True,
        "evidence_path": evidence_path.strip(),
        "source_policy": source_policy.strip(),
        "freshness_policy": facet.get("freshness_policy"),
        "identity_policy": facet.get("identity_policy"),
        "min_sources": min_sources,
        "accepted_source_ids": string_list(facet.get("accepted_source_ids")),
        "blocking_request_ids": request_ids,
        "facet_verdict": facet.get("facet_verdict"),
        "policy_min_trust_tier": policy_min_trust_tier(source_policy),
    }


def linked_policy_facets(project_root: Path, config: dict[str, Any], request_id: str) -> list[dict[str, Any]]:
    facets: list[dict[str, Any]] = []
    for path in coverage_manifest_files(coverage_dir_path(project_root, config)):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        for section in ("required_facets", "optional_facets"):
            section_facets = document.get(section)
            if not isinstance(section_facets, list):
                continue
            for facet in section_facets:
                if not isinstance(facet, dict) or request_id not in string_list(facet.get("blocking_request_ids")):
                    continue
                summary = coverage_policy_facet_summary(path, project_root, document, facet)
                if summary is not None:
                    facets.append(summary)
    return facets


def candidate_acquisition_route(
    candidate: dict[str, Any], acquisition: dict[str, Any], request_id: str
) -> dict[str, Any]:
    """Map one selected candidate to an explicit acquisition route: a provider
    fetch command for arXiv/OpenAlex/GitHub candidates, or a manual-delivery
    target for URL/official-legal/dataset candidates. Never invents provider
    syntax — manual candidates get a delivery target, not a fake command."""
    url = candidate.get("url") if isinstance(candidate.get("url"), str) else ""
    source_type = candidate.get("source_type")
    paper = paper_metadata(candidate)
    provider_budget = candidate.get("provider_budget") if isinstance(candidate.get("provider_budget"), dict) else None
    candidate_network_io = candidate.get("network_io_executed") is True or (
        provider_budget is not None and provider_budget.get("network_io_executed") is True
    )
    policy_fields = candidate_policy_fields(candidate)
    route: dict[str, Any] = {
        "candidate_id": candidate.get("candidate_id"),
        "title": candidate.get("title"),
        "url": url,
        "source_type": source_type,
        "discovery_provider": candidate.get("provider"),
        "trust_tier": candidate.get("trust_tier"),
        "official_source": candidate.get("official_source"),
        "recommended_action": candidate.get("recommended_action"),
        "license": candidate.get("license"),
        "terms_url": candidate.get("terms_url"),
        "evidence_path": policy_fields["evidence_path"],
        "source_policy": policy_fields["source_policy"],
        "freshness_policy": policy_fields["freshness_policy"],
        "identity_policy": policy_fields["identity_policy"],
        "paper": paper,
        "candidate_network_io_executed": candidate_network_io,
        "provider_budget": provider_budget,
    }

    if source_type == "paper" and paper is not None:
        paper_route = paper_acquisition_route(route, paper, acquisition, request_id)
        if paper_route is not None:
            return paper_route

    arxiv_match = arxiv_id_from_query(url) if url else None
    doi = doi_from_query(url) if url else None

    if arxiv_match is not None:
        arxiv_id, is_versioned = arxiv_match
        if is_versioned:
            candidate_id = route.get("candidate_id")
            argv = arxiv_download_argv(
                arxiv_id,
                file_format="source",
                request_id=request_id,
                candidate_id=candidate_id.strip() if isinstance(candidate_id, str) and candidate_id.strip() else None,
            )
            route.update(provider="arxiv", provider_backed=True, route="download-source", confidence="high",
                         reason="Selected paper candidate is a versioned arXiv id; download its source bundle.")
            add_arxiv_pdf_companion(route, arxiv_id, request_id)
        else:
            argv = [
                "python3", "scripts/fetch_sources.py", "--format", "json", "arxiv",
                "search", "--id-list", arxiv_id, "--max-results", "5",
            ]
            route.update(provider="arxiv", provider_backed=True, route="search-by-id", confidence="high",
                         reason="Selected paper candidate is an unversioned arXiv id; search by id and pick the version.")
        return _finish_provider_route(route, acquisition, argv)

    if doi is not None:
        argv = [
            "python3", "scripts/fetch_sources.py", "--format", "json", "openalex",
            "get", "--id-or-doi", doi,
        ]
        route.update(provider="openalex", provider_backed=True, route="get-by-doi", confidence="high",
                     reason="Selected candidate carries a DOI; inspect the OpenAlex work, then download-pdf if open access.")
        return _finish_provider_route(route, acquisition, argv)

    if source_type == "code_repository" or (url and is_github_url(url)):
        argv = [
            "python3", "scripts/fetch_sources.py", "--format", "json", "github",
            "repo-metadata", "--url", url, "--request-id", request_id,
        ]
        route.update(provider="github", provider_backed=True, route="repo-metadata", confidence="high",
                     reason="Selected repository candidate; snapshot repo metadata (add release-metadata or "
                            "download-archive --ref for release/source evidence). No clone, no code execution.")
        return _finish_provider_route(route, acquisition, argv)

    if selected_candidate_is_official_web(candidate, source_type, url):
        argv = web_candidate_command(candidate, request_id, url, source_type)
        route.update(
            provider="web",
            provider_backed=True,
            route="get",
            confidence="high",
            reason=(
                "Selected official HTTPS web candidate; capture it through the allow-listed web provider with "
                "bounded transport and provenance."
            ),
        )
        return _finish_provider_route(route, acquisition, argv)

    # Everything else is manual delivery: datasets, supplemental material, and
    # non-official URL candidates. Emit a delivery target, not a fake provider
    # command.
    target_root = MANUAL_DELIVERY_ROOT.get(source_type, "raw/other/")
    if source_type == "official_legal":
        reason = (
            "Selected official legal source is not eligible for contracted web acquisition, usually because the "
            "URL is missing or not HTTPS. Manually deliver the official URL into "
            f"{target_root} (or an HTML snapshot into raw/web/) with a provenance sidecar."
        )
    else:
        reason = f"Selected {source_type or 'web'} candidate; manually deliver the URL/file into {target_root}."
    route.update(
        provider="manual",
        provider_backed=False,
        route="manual-delivery",
        confidence="medium",
        reason=reason,
        allowed_by_config=True,
        command="",
        command_argv=[],
        manual_delivery={
            "target_root": target_root,
            "url": url,
            "note": (
                "Deliver with a .provenance.yml sidecar (origin_url, retrieved_at, license), then run "
                f"source_inventory.py and source_requests.py fulfill --request-id {request_id} "
                "--source-id <manifest-id> (see docs/source-delivery.md)."
            ),
        },
    )
    return route


def paper_acquisition_route(
    route: dict[str, Any],
    paper: dict[str, Any],
    acquisition: dict[str, Any],
    request_id: str,
) -> dict[str, Any] | None:
    arxiv_match = paper_arxiv_match(paper)
    if arxiv_match is not None:
        arxiv_id, is_versioned = arxiv_match
        if is_versioned:
            candidate_id = route.get("candidate_id")
            argv = arxiv_download_argv(
                arxiv_id,
                file_format="source",
                request_id=request_id,
                candidate_id=candidate_id.strip() if isinstance(candidate_id, str) and candidate_id.strip() else None,
            )
            route.update(provider="arxiv", provider_backed=True, route="download-source", confidence="high",
                         reason="Selected paper metadata carries a versioned arXiv id; download its source bundle.")
            add_arxiv_pdf_companion(route, arxiv_id, request_id)
        else:
            argv = [
                "python3", "scripts/fetch_sources.py", "--format", "json", "arxiv",
                "search", "--id-list", arxiv_id, "--max-results", "5",
            ]
            route.update(provider="arxiv", provider_backed=True, route="search-by-id", confidence="high",
                         reason="Selected paper metadata carries an unversioned arXiv id; search by id and pick the version.")
        return _finish_provider_route(route, acquisition, argv)

    work_id = paper_openalex_work_id(paper)
    if work_id is not None:
        if paper.get("open_access") is True and isinstance(paper.get("pdf_url"), str) and paper["pdf_url"].strip():
            argv = [
                "python3", "scripts/fetch_sources.py", "--format", "json", "openalex",
                "download-pdf", "--work-id", work_id, "--request-id", request_id,
            ]
            append_candidate_id_arg(argv, route)
            route.update(provider="openalex", provider_backed=True, route="download-pdf", confidence="high",
                         reason="Selected OpenAlex paper metadata exposes an open-access PDF; download it by work id.")
        else:
            argv = [
                "python3", "scripts/fetch_sources.py", "--format", "json", "openalex",
                "get", "--id-or-doi", work_id,
                "--output", f"raw/papers/openalex-{work_id}-metadata.json",
                "--request-id", request_id,
            ]
            route.update(provider="openalex", provider_backed=True, route="get", confidence="high",
                         reason="Selected OpenAlex paper metadata is metadata-only or not open access; snapshot the work metadata before manual delivery or alternate acquisition.")
        return _finish_provider_route(route, acquisition, argv)

    doi = paper_doi(paper)
    if doi is not None:
        argv = [
            "python3", "scripts/fetch_sources.py", "--format", "json", "openalex",
            "get", "--id-or-doi", doi,
        ]
        route.update(provider="openalex", provider_backed=True, route="get-by-doi", confidence="high",
                     reason="Selected paper metadata carries a DOI; inspect the OpenAlex work, then download-pdf if open access.")
        return _finish_provider_route(route, acquisition, argv)

    title = paper.get("title")
    if isinstance(title, str) and title.strip():
        argv = [
            "python3", "scripts/fetch_sources.py", "--format", "json", "openalex",
            "resolve", "--entity", "works", "--query", title.strip(), "--max-results", "5",
        ]
        route.update(provider="openalex", provider_backed=True, route="resolve", confidence="medium",
                     reason="Selected paper metadata has no provider id; resolve the title in OpenAlex before fetching.")
        return _finish_provider_route(route, acquisition, argv)

    return None


def _finish_provider_route(
    route: dict[str, Any], acquisition: dict[str, Any], argv: list[str]
) -> dict[str, Any]:
    route["allowed_by_config"] = provider_allowed(acquisition, route["provider"])
    route["command"] = shlex.join(argv)
    route["command_argv"] = argv
    route["manual_delivery"] = None
    return route


def append_candidate_id_arg(argv: list[str], route: dict[str, Any]) -> None:
    candidate_id = route.get("candidate_id")
    if isinstance(candidate_id, str) and candidate_id.strip():
        argv.extend(["--candidate-id", candidate_id.strip()])


def arxiv_download_argv(
    arxiv_id: str,
    *,
    file_format: str,
    request_id: str,
    candidate_id: str | None = None,
) -> list[str]:
    argv = [
        "python3",
        "scripts/fetch_sources.py",
        "--format",
        "json",
        "arxiv",
        "download",
        "--id",
        arxiv_id,
        "--format",
        file_format,
        "--request-id",
        request_id,
    ]
    if candidate_id:
        argv.extend(["--candidate-id", candidate_id])
    return argv


def add_arxiv_pdf_companion(route: dict[str, Any], arxiv_id: str, request_id: str) -> None:
    candidate_id = route.get("candidate_id")
    argv = arxiv_download_argv(
        arxiv_id,
        file_format="pdf",
        request_id=request_id,
        candidate_id=candidate_id.strip() if isinstance(candidate_id, str) and candidate_id.strip() else None,
    )
    route["companion_commands"] = [
        {
            "description": "paired arXiv PDF archival artifact",
            "command": shlex.join(argv),
            "command_argv": argv,
        }
    ]


def candidate_trust_warnings(
    request: dict[str, Any], candidates: list[dict[str, Any]]
) -> tuple[list[str], str]:
    """Warn for selected candidates whose trust tier is below the request's
    required threshold, and for any selected candidate discovery recommended
    rejecting. Returns (warnings, the threshold tier applied)."""
    threshold = request_min_trust_tier(request)
    threshold_rank = TRUST_TIER_RANK[threshold]
    warnings: list[str] = []
    for candidate in candidates:
        tier = candidate.get("trust_tier")
        rank = TRUST_TIER_RANK.get(tier, len(TRUST_TIERS))  # unknown tier sorts worst
        if rank > threshold_rank:
            warnings.append(
                f"Selected candidate {candidate.get('candidate_id')} has trust tier {tier!r}, below the "
                f"required {threshold!r}; review before acquiring."
            )
    warnings.extend(candidate_recommendation_warnings(candidates))
    return warnings, threshold


def candidate_recommendation_warnings(candidates: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for candidate in candidates:
        if candidate.get("recommended_action") == "reject":
            warnings.append(
                f"Selected candidate {candidate.get('candidate_id')} was discovery-ranked recommended_action "
                "'reject'; confirm it is appropriate before acquiring."
            )
    return warnings


def academic_candidate_warnings(candidates: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for candidate in candidates:
        if candidate.get("source_type") != "paper":
            continue
        candidate_id = candidate.get("candidate_id")
        paper = paper_metadata(candidate) or {}
        candidate_license_known = candidate.get("license") is not None
        paper_license_known = paper.get("license") is not None
        if not candidate_license_known or not paper_license_known:
            warnings.append(
                f"Selected academic candidate {candidate_id} has unknown license metadata; review terms before reusable output."
            )
        if paper.get("resolution_status") == "uncertain":
            warnings.append(
                f"Selected academic candidate {candidate_id} has uncertain resolution; resolve provider identity before fetching."
            )
        if paper and paper.get("open_access") is False:
            warnings.append(
                f"Selected academic candidate {candidate_id} is not open access according to provider metadata; inspect metadata and use manual delivery if needed."
            )
        elif paper and paper.get("resolution_status") == "metadata_only":
            warnings.append(
                f"Selected academic candidate {candidate_id} has metadata-only provider data; inspect the record before attempting PDF acquisition."
            )
    return warnings


def attach_policy_context_to_candidate_routes(
    routes: list[dict[str, Any]],
    policy_facets: list[dict[str, Any]],
    fallback_min_trust_tier: str,
    request_id: str,
) -> list[str]:
    warnings: list[str] = []
    for route in routes:
        if not policy_facets:
            route["policy_facets"] = []
            route["policy_alignment"] = "request_min_trust_tier"
            route["policy_min_trust_tier"] = fallback_min_trust_tier
            continue

        evidence_path = route.get("evidence_path")
        matching_facets = [facet for facet in policy_facets if facet.get("evidence_path") == evidence_path]
        route["policy_facets"] = matching_facets
        if not matching_facets:
            route["policy_alignment"] = "no_matching_evidence_path"
            route["policy_min_trust_tier"] = None
            warnings.append(
                f"Selected candidate {route.get('candidate_id')} evidence_path {evidence_path!r} "
                f"does not match any linked coverage facet for request {request_id}; review before acquiring."
            )
            continue

        threshold = strictest_min_trust_tier(matching_facets, fallback_min_trust_tier)
        route["policy_min_trust_tier"] = threshold
        if threshold is not None and trust_tier_below(route.get("trust_tier"), threshold):
            route["policy_alignment"] = "below_min_trust"
            policies = ", ".join(sorted({str(facet.get("source_policy")) for facet in matching_facets}))
            warnings.append(
                f"Selected candidate {route.get('candidate_id')} has trust tier {route.get('trust_tier')!r}, "
                f"below linked facet policy {policies} requiring at least {threshold!r}; review before acquiring."
            )
        else:
            route["policy_alignment"] = "matched"
    return warnings


def plan_routes_for_request(record: dict[str, Any], acquisition: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    query = record.get("query_or_identifier") if isinstance(record.get("query_or_identifier"), str) else ""
    request_id = record.get("request_id") if isinstance(record.get("request_id"), str) else ""
    if record.get("status") == "fulfilled":
        source_id = record.get("source_id")
        return "already_fulfilled", [], [f"Request {request_id} is already fulfilled by source {source_id}."]
    if record.get("kind") != "paper":
        return "unsupported", [], [UNSUPPORTED_KIND_WARNING]

    arxiv_match = arxiv_id_from_query(query)
    if arxiv_match is not None:
        arxiv_id, is_versioned = arxiv_match
        if not is_versioned:
            route = command_route(
                provider="arxiv",
                route="search-by-id",
                confidence="high",
                reason=(
                    "Request contains an unversioned arXiv identifier; search by id-list and choose the exact "
                    "version before downloading."
                ),
                allowed_by_config=provider_allowed(acquisition, "arxiv"),
                command_argv=[
                    "python3",
                    "scripts/fetch_sources.py",
                    "--format",
                    "json",
                    "arxiv",
                    "search",
                    "--id-list",
                    arxiv_id,
                    "--max-results",
                    "5",
                ],
            )
            return "ready", [route], plan_warnings(acquisition, [route])

        route = command_route(
            provider="arxiv",
            route="download-source",
            confidence="high",
            reason="Request contains a versioned arXiv identifier; download the source bundle with the request id.",
            allowed_by_config=provider_allowed(acquisition, "arxiv"),
            command_argv=arxiv_download_argv(arxiv_id, file_format="source", request_id=request_id),
        )
        add_arxiv_pdf_companion(route, arxiv_id, request_id)
        return "ready", [route], plan_warnings(acquisition, [route])

    doi = doi_from_query(query)
    if doi is not None:
        route = command_route(
            provider="openalex",
            route="get-by-doi",
            confidence="high",
            reason=(
                "Request contains a DOI; inspect the OpenAlex work, then use openalex download-pdf with the "
                "returned work id when an open-access PDF is available."
            ),
            allowed_by_config=provider_allowed(acquisition, "openalex"),
            command_argv=[
                "python3",
                "scripts/fetch_sources.py",
                "--format",
                "json",
                "openalex",
                "get",
                "--id-or-doi",
                doi,
            ],
        )
        return "ready", [route], plan_warnings(acquisition, [route])

    routes = [
        command_route(
            provider="arxiv",
            route="search",
            confidence="medium",
            reason="Request looks like a paper search query; inspect arXiv candidates before downloading.",
            allowed_by_config=provider_allowed(acquisition, "arxiv"),
            command_argv=[
                "python3",
                "scripts/fetch_sources.py",
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                query,
                "--max-results",
                "5",
            ],
        ),
        command_route(
            provider="openalex",
            route="resolve",
            confidence="medium",
            reason="Request looks like a paper search query; inspect OpenAlex candidates before downloading.",
            allowed_by_config=provider_allowed(acquisition, "openalex"),
            command_argv=[
                "python3",
                "scripts/fetch_sources.py",
                "--format",
                "json",
                "openalex",
                "resolve",
                "--entity",
                "works",
                "--query",
                query,
                "--max-results",
                "5",
            ],
        ),
    ]
    warnings = plan_warnings(acquisition, routes)
    warnings.append("Ambiguous query; inspect provider results before choosing a download command.")
    return "ambiguous", routes, warnings


def relative_label(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def run_add(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(args.project_root).expanduser().resolve()
    config = load_config(project_root)
    path = requests_path(project_root, config)

    query = args.query_or_identifier.strip()
    if not query:
        raise SystemExit("--query-or-identifier must be a non-empty string")
    rationale = args.rationale.strip()
    if not rationale:
        raise SystemExit("--rationale must be a non-empty string")
    question_slugs = validate_question_slugs(project_root, config, args.question_slugs or [])

    with workspace_lock(source_requests_lock_path(path), purpose="source request mutation"):
        records = load_requests(path)
        duplicate = find_open_duplicate(records, args.kind, query)
        if duplicate is not None:
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "add",
                "created": False,
                "duplicate_of": duplicate.get("request_id"),
                "request": duplicate,
                "requests_path": relative_label(project_root, path),
            }

        now = timestamp_utc()
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request_id": generate_request_id(args.kind, query, now, len(records)),
            "kind": args.kind,
            "query_or_identifier": query,
            "rationale": rationale,
            "priority": args.priority,
            "question_slugs": question_slugs,
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "source_id": None,
        }
        _append_request_unlocked(path, record)
    append_log_entry(
        project_root / "log.md",
        (
            f"## [{now.split('T', 1)[0]}] source-request | Recorded source request\n\n"
            f"- Request: `{record['request_id']}` ({record['kind']}, {record['priority']}).\n"
            f"- Needs: {query}\n"
            f"- Questions: {', '.join(question_slugs) if question_slugs else 'none'}.\n"
        ),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "add",
        "created": True,
        "request": record,
        "requests_path": relative_label(project_root, path),
    }


def run_list(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(args.project_root).expanduser().resolve()
    config = load_config(project_root)
    path = requests_path(project_root, config)
    records = load_requests(path)
    statuses = list(dict.fromkeys(args.status)) if args.status else None
    selected = [record for record in records if statuses is None or record.get("status") in statuses]
    selected.sort(key=request_time_sort_key)
    counts = {status: 0 for status in REQUEST_STATUSES}
    for record in records:
        status = record.get("status")
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_utc(),
        "requests_path": relative_label(project_root, path),
        "filter_statuses": statuses,
        "counts": {"total": len(records), **counts},
        "requests": selected,
    }


def run_fulfill(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(args.project_root).expanduser().resolve()
    config = load_config(project_root)
    path = requests_path(project_root, config)

    request_id = args.request_id.strip()
    source_id = args.source_id.strip()
    if not request_id or not source_id:
        raise SystemExit("--request-id and --source-id must be non-empty strings")

    with workspace_lock(source_requests_lock_path(path), purpose="source request mutation"):
        records = load_requests(path)
        target = next((record for record in records if record.get("request_id") == request_id), None)
        if target is None:
            raise SystemExit(f"Unknown request id: {request_id} (no record in {relative_label(project_root, path)})")
        if target.get("status") == "fulfilled":
            if target.get("source_id") == source_id:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "action": "fulfill",
                    "updated": False,
                    "request": target,
                    "requests_path": relative_label(project_root, path),
                }
            raise SystemExit(
                f"Request {request_id} is already fulfilled by source {target.get('source_id')}; "
                "record a new request instead of relinking it."
            )
        if source_id not in manifest_source_ids(project_root, config):
            raise SystemExit(
                f"Unknown source id: {source_id} (not in the manifest). "
                "Run source_inventory.py after delivering the files, then fulfill."
            )

        now = timestamp_utc()
        target["status"] = "fulfilled"
        target["source_id"] = source_id
        target["updated_at"] = now
        _write_requests_unlocked(path, records)
    append_log_entry(
        project_root / "log.md",
        (
            f"## [{now.split('T', 1)[0]}] source-request | Fulfilled source request\n\n"
            f"- Request: `{request_id}` fulfilled by `{source_id}`.\n"
            f"- Questions: {', '.join(target.get('question_slugs') or []) or 'none'}.\n"
        ),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "fulfill",
        "updated": True,
        "request": target,
        "requests_path": relative_label(project_root, path),
    }


def run_plan_fetch(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(args.project_root).expanduser().resolve()
    config = load_config(project_root)
    path = requests_path(project_root, config)
    records = load_requests(path)

    request_id = args.request_id.strip()
    if not request_id:
        raise SystemExit("--request-id must be a non-empty string")
    target = next((record for record in records if record.get("request_id") == request_id), None)
    if target is None:
        raise SystemExit(f"Unknown request id: {request_id} (no record in {relative_label(project_root, path)})")

    acquisition = acquisition_plan_context(config)
    plan_status, routes, warnings = plan_routes_for_request(target, acquisition)

    # Fold in explicitly selected discovery candidates (E36-T02). These are
    # authoritative — a reviewer picked them — so when present they supersede the
    # heuristic query routing for an unsupported/ambiguous request.
    selected = load_selected_candidates(project_root, config, request_id)
    candidate_routes = [
        candidate_acquisition_route(candidate, acquisition, request_id) for candidate in selected
    ]
    policy_facets = linked_policy_facets(project_root, config, request_id)
    policy_source = "coverage_manifest" if policy_facets else "request_min_trust_tier"
    fallback_min_trust_tier = request_min_trust_tier(target)
    policy_warnings = attach_policy_context_to_candidate_routes(
        candidate_routes,
        policy_facets,
        fallback_min_trust_tier,
        request_id,
    )
    warnings = list(warnings)
    if candidate_routes:
        if plan_status in ("unsupported", "ambiguous"):
            plan_status = "ready"
        # The "no provider-backed plan / use manual delivery" note is stale once
        # explicit candidate routes exist; drop it to avoid contradicting "ready".
        warnings = [warning for warning in warnings if warning != UNSUPPORTED_KIND_WARNING]
        if not acquisition.get("enabled"):
            warnings.append(ACQUISITION_DISABLED_WARNING)
        disabled = sorted(
            {route["provider"] for route in candidate_routes if route.get("provider_backed") and not route["allowed_by_config"]}
        )
        if acquisition.get("enabled") and disabled:
            warnings.append(PROVIDER_NOT_ALLOWLISTED_PREFIX + ", ".join(disabled))
    if policy_facets:
        min_trust_tier = strictest_min_trust_tier(policy_facets, fallback_min_trust_tier) or fallback_min_trust_tier
        warnings.extend(policy_warnings)
        warnings.extend(candidate_recommendation_warnings(selected))
    else:
        trust_warnings, min_trust_tier = candidate_trust_warnings(target, selected)
        warnings.extend(trust_warnings)
    warnings.extend(academic_candidate_warnings(selected))
    warnings = list(dict.fromkeys(warnings))  # de-duplicate, preserving order

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "plan-fetch",
        "generated_at": timestamp_utc(),
        "request": target,
        "acquisition": acquisition,
        "plan_status": plan_status,
        "network_io_executed": False,
        "min_trust_tier": min_trust_tier,
        "policy_source": policy_source,
        "policy_facets": policy_facets,
        "selected_candidate_count": len(selected),
        "routes": routes,
        "candidate_routes": candidate_routes,
        "warnings": warnings,
    }


def render_text_report(report: dict[str, Any]) -> str:
    if report.get("action") == "add":
        if report["created"]:
            lines = ["Recorded source request:", f"  {request_summary(report['request'])}"]
        else:
            lines = [
                f"Duplicate of open request {report['duplicate_of']}; nothing recorded:",
                f"  {request_summary(report['request'])}",
            ]
        return "\n".join(lines) + "\n"
    if report.get("action") == "fulfill":
        verb = "Fulfilled" if report["updated"] else "Already fulfilled (no-op)"
        return f"{verb}:\n  {request_summary(report['request'])}\n"
    if report.get("action") == "plan-fetch":
        lines = [
            f"Fetch plan for {report['request'].get('request_id', '?')}: {report['plan_status']}",
            f"Network I/O executed: {str(report['network_io_executed']).lower()}",
        ]
        if report["routes"]:
            lines.append("")
            lines.extend(f"- {route['provider']} {route['route']}: {route['command']}" for route in report["routes"])
        candidate_routes = report.get("candidate_routes") or []
        if candidate_routes:
            lines.append("")
            lines.append(f"Selected candidates ({report.get('selected_candidate_count', len(candidate_routes))}):")
            for route in candidate_routes:
                if route["provider"] == "manual":
                    target = route.get("manual_delivery", {}).get("target_root", "raw/")
                    detail = f"manual delivery to {target} <- {route['url']}"
                else:
                    detail = route["command"]
                lines.append(f"- [{route['trust_tier']}] {route['candidate_id']} ({route['provider']} {route['route']}): {detail}")
        if report["warnings"]:
            lines.append("")
            lines.extend(f"Warning: {warning}" for warning in report["warnings"])
        return "\n".join(lines).rstrip() + "\n"
    counts = report["counts"]
    lines = [
        "Source Requests",
        "===============",
        f"File: {report['requests_path']}",
        f"Total: {counts['total']} (open: {counts.get('open', 0)}, fulfilled: {counts.get('fulfilled', 0)})",
    ]
    if report["requests"]:
        lines.append("")
        lines.extend(f"- {request_summary(record)}" for record in report["requests"])
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    try:
        if args.command == "add":
            report = run_add(args)
        elif args.command == "list":
            report = run_list(args)
        elif args.command == "fulfill":
            report = run_fulfill(args)
        else:
            report = run_plan_fetch(args)
    except LockUnavailableError as exc:
        emit_error(str(exc), json_mode=json_mode, error_code=exc.error_code, details=exc.details)
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text_report(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
