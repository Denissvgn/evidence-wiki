#!/usr/bin/env python3
"""Optional acquisition command surface for fetch agents.

This script defines the acquisition gate, provider registry, CLI shape,
provider transports, and provenance helpers for explicitly enabled fetch
workflows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _workspace_module_loader import load_workspace_module

_academic_identity = load_workspace_module(_SCRIPT_DIR, "_academic_identity")
author_sets_match = _academic_identity.author_sets_match

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_INVALID = 2
ACQUISITION_DEFAULT_TARGET_ROOT = "raw/papers"
PROVENANCE_SIDECAR_SUFFIX = ".provenance.yml"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_ABS_URL = "https://arxiv.org/abs/{id}"
ARXIV_PDF_URL = "https://arxiv.org/pdf/{id}"
ARXIV_SOURCE_URL = "https://arxiv.org/e-print/{id}"
ARXIV_TIMEOUT_SECONDS = 30.0
ARXIV_MAX_ATTEMPTS = 3
ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_MAX_MEMBER_NAME_BYTES = 512
ARXIV_MAX_MEMBER_PART_BYTES = 255
ARXIV_MAX_MEMBERS = 4096
ARXIV_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}v\d+$", re.IGNORECASE)
ARXIV_ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
ARXIV_TRANSPORT = None
ARXIV_SLEEP = time.sleep
ARXIV_CLOCK = time.monotonic
ARXIV_LAST_REQUEST_AT: float | None = None
OPENALEX_API_URL = "https://api.openalex.org"
OPENALEX_WORKS_URL = f"{OPENALEX_API_URL}/works"
OPENALEX_SELECT_FIELDS = (
    "id,doi,display_name,publication_year,type,authorships,primary_location,best_oa_location,open_access,locations"
)
NEGATIVE_CLAIM_PROBE_LIMITATION = "not found in configured providers for this bounded run; not a global nonexistence claim"
OPENALEX_TIMEOUT_SECONDS = 30.0
OPENALEX_MAX_ATTEMPTS = 3
OPENALEX_REQUEST_INTERVAL_SECONDS = 1.0
OPENALEX_ID_RE = re.compile(r"^W\d+$", re.IGNORECASE)
OPENALEX_DOI_RE = re.compile(r"^10\.\S+/.+", re.IGNORECASE)
OPENALEX_TRANSPORT = None
OPENALEX_SLEEP = time.sleep
OPENALEX_CLOCK = time.monotonic
OPENALEX_LAST_REQUEST_AT: float | None = None
DOI_TIMEOUT_SECONDS = 10.0
DOI_TRANSPORT = None
# --- GitHub bounded acquisition (E32-T03) ------------------------------------
# Acquisition captures explicitly selected repositories as evidence: a repository
# metadata snapshot, release asset metadata, or a source archive for a chosen ref.
# It never clones, never executes repository code, and never auto-selects a repo
# from discovery search results. GITHUB_TOKEN is read from the environment only
# and is never written to output, provenance sidecars, or logs.
GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_TIMEOUT_SECONDS = 30.0
GITHUB_MAX_ATTEMPTS = 3
GITHUB_REQUEST_INTERVAL_SECONDS = 2.0
GITHUB_DEFAULT_TARGET_ROOT = "raw/code"
# Pre- and post-download size ceiling so a selected archive stays bounded.
GITHUB_DEFAULT_MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
GITHUB_RETRIEVED_BY = "fetch_sources.py/github"
GITHUB_USER_AGENT = (
    "evidence-wiki fetch_sources.py/1.0; "
    "GitHub acquisition; set GITHUB_TOKEN for higher rate limits"
)
# owner/repo and ref syntax guards. Refs allow branch/tag/sha characters but
# never spaces, control characters, leading '-', or '..' path traversal.
GITHUB_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
GITHUB_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
GITHUB_TRANSPORT = None
GITHUB_SLEEP = time.sleep
GITHUB_CLOCK = time.monotonic
GITHUB_LAST_REQUEST_AT: float | None = None
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_BACKOFF_MAX_SECONDS = 8.0
ARXIV_DEFAULT_MAX_RESPONSE_BYTES = 100 * 1024 * 1024
OPENALEX_DEFAULT_MAX_RESPONSE_BYTES = 100 * 1024 * 1024
GITHUB_DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
WEB_EXPECTED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/xml",
    "text/xml",
)
ARXIV_METADATA_CONTENT_TYPES = ("application/atom+xml", "application/xml", "text/xml")
ARXIV_PDF_CONTENT_TYPES = ("application/pdf",)
ARXIV_SOURCE_CONTENT_TYPES = (
    "application/x-eprint-tar",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/octet-stream",
)
OPENALEX_JSON_CONTENT_TYPES = ("application/json",)
OPENALEX_PDF_CONTENT_TYPES = ("application/pdf",)
GITHUB_JSON_CONTENT_TYPES = ("application/json", "application/vnd.github+json")
GITHUB_ARCHIVE_CONTENT_TYPES = (
    "application/gzip",
    "application/x-gzip",
    "application/octet-stream",
)
WEB_DEFAULT_TARGET_ROOT = "raw/web"
WEB_DEFAULT_MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
WEB_TIMEOUT_SECONDS = 30.0
WEB_RETRIEVED_BY = "fetch_sources.py/web"
WEB_TRANSPORT = None
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
OPENALEX_LICENSE_TO_SPDX = {
    "public-domain": "CC0-1.0",
    "cc0": "CC0-1.0",
    "cc0-1.0": "CC0-1.0",
    "cc-by-4.0": "CC-BY-4.0",
    "cc-by-sa-4.0": "CC-BY-SA-4.0",
    "cc-by-nc-4.0": "CC-BY-NC-4.0",
    "cc-by-nc-sa-4.0": "CC-BY-NC-SA-4.0",
    "cc-by-nd-4.0": "CC-BY-ND-4.0",
    "cc-by-nc-nd-4.0": "CC-BY-NC-ND-4.0",
    "mit": "MIT",
}
PROVIDER_REGISTRY: dict[str, dict[str, Any]] = {
    "arxiv": {
        "provider_id": "arxiv",
        "terms_urls": [
            "https://info.arxiv.org/help/api/tou.html",
            "https://info.arxiv.org/help/api/user-manual.html",
            "https://info.arxiv.org/help/license/index.html",
        ],
        "supported_commands": ["search", "download"],
        "license_inference": "partial",
    },
    "openalex": {
        "provider_id": "openalex",
        "terms_urls": [
            "https://developers.openalex.org/api-reference/authentication",
            "https://developers.openalex.org/api-reference/works",
            "https://developers.openalex.org/api-reference/licenses",
        ],
        "supported_commands": ["resolve", "get", "download-pdf", "enrich"],
        "license_inference": "yes",
    },
    "github": {
        "provider_id": "github",
        "terms_urls": [
            "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
            "https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api",
            "https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api",
            "https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository",
        ],
        # Bounded acquisition (E32-T03): capture an explicitly selected repository
        # as evidence without cloning or executing code. Repository discovery lives
        # in discover_sources.py github (E32-T02).
        "supported_commands": ["repo-metadata", "release-metadata", "download-archive"],
        "license_inference": "partial",
    },
    "web": {
        "provider_id": "web",
        "terms_urls": ["per-origin"],
        "supported_commands": ["get"],
        "license_inference": "none",
    },
}

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _provider_registry import ProviderListError, validate_provider_ids

_acquisition_transport = load_workspace_module(_SCRIPT_DIR, "_acquisition_transport")
AcquisitionTransportError = _acquisition_transport.AcquisitionTransportError
DownloadResult = _acquisition_transport.DownloadResult
bounded_download = _acquisition_transport.bounded_download
build_default_opener = _acquisition_transport.build_default_opener
header_value = _acquisition_transport.header_value
redact_diagnostic = _acquisition_transport.redact_diagnostic
redact_url = _acquisition_transport.redact_url
response_status = _acquisition_transport.response_status
response_url = _acquisition_transport.response_url
result_from_bytes = _acquisition_transport.result_from_bytes
validate_download_result = _acquisition_transport.validate_download_result
validate_https_url = _acquisition_transport.validate_https_url
_script_errors = load_workspace_module(_SCRIPT_DIR, "_script_errors")
emit_error = _script_errors.emit_error
handle_system_exit = _script_errors.handle_system_exit
json_mode_requested = _script_errors.json_mode_requested
_workspace_locks = load_workspace_module(_SCRIPT_DIR, "_workspace_locks")
LockUnavailableError = _workspace_locks.LockUnavailableError
workspace_lock = _workspace_locks.workspace_lock

ACQUISITION_INCOMPLETE_SUFFIX = ".acquisition-incomplete.json"
ACQUISITION_LOCK_RELATIVE = ("raw", ".locks", "acquisition.lock")
WINDOWS_RESERVED_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
RUN_TERMINAL_STATES = {"complete", "blocked_on_sources", "no_ship", "failed"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class FetchSourcesError(Exception):
    """Structured acquisition failure with a stable machine error code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        recoverable: bool = True,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.recoverable = recoverable
        self.remediation = remediation


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch sources through explicitly enabled acquisition providers.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("text", "json"),
        default="text",
        help="Report format for command output and fatal errors. Defaults to text.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Active run-controller id used for restart-stable cumulative acquisition budgets. "
            "When omitted, the sole active run is selected automatically."
        ),
    )
    providers = parser.add_subparsers(dest="provider", required=True)

    arxiv = providers.add_parser("arxiv", help="arXiv acquisition commands.")
    arxiv_commands = arxiv.add_subparsers(dest="command", required=True)
    arxiv_search = arxiv_commands.add_parser(
        "search",
        help="Search arXiv metadata. Redirect large JSON results to --output.",
        description=(
            "Search arXiv metadata through the Atom API. Results are compact JSON; "
            "agents should use --output for anything larger than a tiny inspection query."
        ),
    )
    arxiv_search.add_argument("--query", help="Search query text.")
    arxiv_search.add_argument("--id-list", help="Comma-separated arXiv identifiers to fetch or filter.")
    arxiv_search.add_argument("--max-results", type=positive_int, required=True, help="Maximum results to return.")
    arxiv_search.add_argument("--output", help="Optional workspace-relative output path for search metadata.")
    arxiv_download = arxiv_commands.add_parser("download", help="Download an arXiv artifact.")
    arxiv_download.add_argument("--id", required=True, help="arXiv identifier to download.")
    arxiv_download.add_argument("--format", dest="artifact_format", choices=("pdf", "source"), required=True)
    arxiv_download.add_argument("--request-id", help="Optional source request id satisfied by this download.")
    arxiv_download.add_argument("--candidate-id", help="Optional discovery candidate id satisfied by this download.")

    openalex = providers.add_parser("openalex", help="OpenAlex acquisition commands.")
    openalex_commands = openalex.add_subparsers(dest="command", required=True)
    openalex_resolve = openalex_commands.add_parser("resolve", help="Resolve OpenAlex entities by query.")
    openalex_resolve.add_argument("--entity", choices=("works",), required=True, help="OpenAlex entity type.")
    openalex_resolve.add_argument("--query", required=True, help="OpenAlex query text.")
    openalex_resolve.add_argument("--max-results", type=positive_int, default=10, help="Maximum results to return.")
    openalex_resolve.add_argument(
        "--allow-unconfirmed",
        action="store_true",
        help=(
            "Return a structured unconfirmed bounded-search report instead of failing when no exact work resolves."
        ),
    )
    openalex_get = openalex_commands.add_parser("get", help="Get one OpenAlex work by ID or DOI.")
    openalex_get.add_argument("--id-or-doi", required=True, help="OpenAlex work ID or DOI.")
    openalex_get.add_argument(
        "--output",
        help="Optional workspace-relative metadata snapshot path under integrations.acquisition.target_root.",
    )
    openalex_get.add_argument("--request-id", help="Optional source request id satisfied by this snapshot.")
    openalex_get.add_argument("--candidate-id", help="Optional discovery candidate id satisfied by this snapshot.")
    openalex_download = openalex_commands.add_parser("download-pdf", help="Download an OpenAlex work PDF.")
    openalex_download.add_argument("--work-id", required=True, help="OpenAlex work ID.")
    openalex_download.add_argument("--output", help="Optional workspace-relative output path for the PDF.")
    openalex_download.add_argument("--request-id", help="Optional source request id satisfied by this download.")
    openalex_download.add_argument("--candidate-id", help="Optional discovery candidate id satisfied by this download.")
    openalex_enrich = openalex_commands.add_parser("enrich", help="Enrich existing arXiv sidecars with OpenAlex identity.")
    enrich_target = openalex_enrich.add_mutually_exclusive_group(required=True)
    enrich_target.add_argument("--source-id", action="append", dest="source_ids", help="Manifest source id to enrich.")
    enrich_target.add_argument("--all-arxiv", action="store_true", help="Enrich every arXiv-acquired manifest record.")
    openalex_enrich.add_argument("--request-id", help="Optional request id filter when using --all-arxiv.")

    github = providers.add_parser(
        "github",
        help="GitHub bounded acquisition: capture an explicitly selected repository as evidence.",
    )
    github_commands = github.add_subparsers(dest="command", required=True)
    github_repo = github_commands.add_parser(
        "repo-metadata",
        help="Snapshot repository metadata for a selected repo (no clone, no file contents).",
    )
    add_github_repo_selector(github_repo)
    github_repo.add_argument("--request-id", help="Optional source request id satisfied by this snapshot.")
    github_repo.add_argument("--candidate-id", help="Optional discovery candidate id satisfied by this snapshot.")
    github_release = github_commands.add_parser(
        "release-metadata",
        help="Snapshot release asset metadata (latest release, or --tag).",
    )
    add_github_repo_selector(github_release)
    github_release.add_argument("--tag", help="Release tag to snapshot. Omit for the latest release.")
    github_release.add_argument("--request-id", help="Optional source request id satisfied by this snapshot.")
    github_release.add_argument("--candidate-id", help="Optional discovery candidate id satisfied by this snapshot.")
    github_archive = github_commands.add_parser(
        "download-archive",
        help="Download a source archive for an explicit ref (no extraction, no code execution).",
    )
    add_github_repo_selector(github_archive)
    github_archive.add_argument("--ref", required=True, help="Branch, tag, or commit SHA to archive.")
    github_archive.add_argument("--request-id", help="Optional source request id satisfied by this download.")
    github_archive.add_argument("--candidate-id", help="Optional discovery candidate id satisfied by this download.")

    web = providers.add_parser("web", help="Bounded HTTPS web acquisition commands.")
    web_commands = web.add_subparsers(dest="command", required=True)
    web_get = web_commands.add_parser("get", help="Capture one allow-listed HTTPS web page.")
    web_get.add_argument("--url", required=True, help="Allow-listed HTTPS URL to capture.")
    web_get.add_argument("--request-id", help="Optional source request id satisfied by this download.")
    web_get.add_argument("--candidate-id", help="Optional discovery candidate id satisfied by this download.")
    web_get.add_argument("--source-type", default="web_page", help="Source type to record in provenance.")
    web_get.add_argument("--publisher", help="Publisher or authority label to record in provenance.")
    web_get.add_argument("--jurisdiction", help="Jurisdiction label to record in provenance.")
    web_get.add_argument("--terms-url", help="Origin terms URL reviewed for this capture.")
    web_get.add_argument(
        "--evidence-area",
        action="append",
        default=[],
        help="Evidence area supported by this capture. Repeat for multiple areas.",
    )
    web_get.add_argument(
        "--insecure-tls-documented",
        help=argparse.SUPPRESS,
    )
    web_get.add_argument("--publication-date", help="Operator-verified publication date as YYYY-MM-DD.")
    web_get.add_argument("--effective-date", help="Operator-verified effective date as YYYY-MM-DD.")
    web_get.add_argument("--validity-period", help="Operator-verified ISO validity interval, start/end.")
    web_get.add_argument("--date-note", help="Operator note explaining currentness/date availability.")
    web_get.add_argument("--valid-for-year", help="Operator-verified year this source applies to, as YYYY.")
    web_get.add_argument(
        "--standards-metadata",
        help="Workspace-relative JSON/YAML file to merge into provenance.standards.",
    )
    return parser.parse_args(argv)


def add_github_repo_selector(parser: argparse.ArgumentParser) -> None:
    """Require an explicit repository; acquisition never auto-selects one."""
    parser.add_argument("--repo", help="Repository as owner/repo, for example acme/rag-toolkit.")
    parser.add_argument("--url", help="Repository URL, for example https://github.com/acme/rag-toolkit.")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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


def validate_raw_target_path(value: Any, label: str) -> str:
    relative = validate_workspace_relative_path(value, label).rstrip("/")
    path = PurePosixPath(relative)
    if len(path.parts) < 2 or path.parts[0] != "raw":
        raise SystemExit(f"research.yml {label} must be under the raw/ evidence directory: {value}")
    return relative


def integrations_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("integrations")
    return value if isinstance(value, dict) else {}


def acquisition_config(config: dict[str, Any]) -> dict[str, Any]:
    acquisition = integrations_config(config).get("acquisition")
    if acquisition is None:
        raise FetchSourcesError(
            "ACQUISITION_DISABLED",
            "Acquisition is disabled: missing integrations.acquisition in research.yml.",
            remediation=(
                "Set integrations.acquisition.enabled: true, choose allowed providers, "
                "and rerun from an explicit fetch workflow."
            ),
        )
    if not isinstance(acquisition, dict):
        raise SystemExit("research.yml integrations.acquisition must be a mapping")
    enabled = acquisition.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SystemExit("research.yml integrations.acquisition.enabled must be a boolean")
    if not enabled:
        raise FetchSourcesError(
            "ACQUISITION_DISABLED",
            "Acquisition is disabled: integrations.acquisition.enabled is not true.",
            remediation=(
                "Set integrations.acquisition.enabled: true, choose allowed providers, "
                "and rerun from an explicit fetch workflow."
            ),
        )
    return acquisition


def validate_provider_list(value: Any, label: str, *, require_non_empty: bool = False) -> list[str]:
    try:
        providers = validate_provider_ids(
            value,
            phase="acquisition",
            require_non_empty=require_non_empty,
        )
    except ProviderListError as exc:
        raise SystemExit(f"research.yml {label} {exc}") from exc
    return list(providers.providers)


def max_downloads_per_run(acquisition: dict[str, Any]) -> int:
    value = acquisition.get("max_downloads_per_run", 10)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SystemExit("research.yml integrations.acquisition.max_downloads_per_run must be a positive integer")
    return value


def require_license_check(acquisition: dict[str, Any]) -> bool:
    value = acquisition.get("require_license_check", True)
    if not isinstance(value, bool):
        raise SystemExit("research.yml integrations.acquisition.require_license_check must be a boolean")
    return value


def resolve_target_root(project_root: Path, acquisition: dict[str, Any]) -> Path:
    value = acquisition.get("target_root", ACQUISITION_DEFAULT_TARGET_ROOT)
    relative = validate_raw_target_path(value, "integrations.acquisition.target_root")
    return safe_workspace_path(project_root, project_root / relative, "integrations.acquisition.target_root")


def require_provider_allowed(provider: str, providers: list[str]) -> None:
    if provider not in providers:
        raise FetchSourcesError(
            "ACQUISITION_PROVIDER_DISABLED",
            f"Acquisition provider {provider!r} is not listed in integrations.acquisition.providers.",
            remediation="Add the provider to integrations.acquisition.providers or choose an enabled provider.",
        )


def enforce_max_downloads(requested: int, configured_limit: int) -> None:
    if requested > configured_limit:
        raise FetchSourcesError(
            "ACQUISITION_LIMIT_EXCEEDED",
            (
                f"Requested {requested} result(s), which exceeds "
                f"integrations.acquisition.max_downloads_per_run={configured_limit}."
            ),
            remediation="Lower the requested count or raise max_downloads_per_run after reviewing provider limits.",
        )


def command_requested_count(args: argparse.Namespace) -> int:
    if args.command in {"search", "resolve"}:
        return args.max_results
    return 1


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_run_budget_state(path: Path, *, requested_run_id: str | None) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FetchSourcesError(
            "ACQUISITION_RUN_STATE_INVALID",
            f"Cannot read retained acquisition run state {path}: {exc}",
            recoverable=False,
            remediation="Repair or recover the retained run-state artifact before acquiring more evidence.",
        ) from exc
    state = document.get("state") if isinstance(document, dict) and isinstance(document.get("state"), dict) else {}
    run_id = document.get("run_id") if isinstance(document, dict) else None
    started_at = parse_utc_timestamp(document.get("started_at") if isinstance(document, dict) else None)
    if (
        not isinstance(document, dict)
        or not isinstance(run_id, str)
        or run_id != path.parent.name
        or not isinstance(state.get("current"), str)
        or started_at is None
    ):
        raise FetchSourcesError(
            "ACQUISITION_RUN_STATE_INVALID",
            f"Retained acquisition run state has an invalid shape: {path}",
            recoverable=False,
            remediation="Repair or recover the retained run-state artifact before acquiring more evidence.",
        )
    if document.get("_pending_event") is not None:
        raise FetchSourcesError(
            "ACQUISITION_RUN_RECOVERY_REQUIRED",
            f"Run {run_id} has an interrupted mutation and cannot accept acquisition evidence.",
            remediation=f"Run run_controller.py recover --run-id {run_id} before retrying acquisition.",
        )
    return {
        "run_id": run_id,
        "started_at": started_at,
        "state": state["current"],
        "path": path,
        "requested": requested_run_id is not None,
    }


def resolve_acquisition_run(project_root: Path, requested_run_id: str | None) -> dict[str, Any] | None:
    runs_root = project_root / "runs"
    if requested_run_id is not None:
        run_id = requested_run_id.strip()
        if not RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."} or ".." in run_id:
            raise FetchSourcesError(
                "ACQUISITION_RUN_ID_INVALID",
                f"Invalid acquisition run id: {requested_run_id!r}",
                recoverable=False,
                remediation="Use a filename-safe active run id from runs/<run-id>/run-state.json.",
            )
        state_path = runs_root / run_id / "run-state.json"
        if not state_path.is_file():
            raise FetchSourcesError(
                "ACQUISITION_RUN_UNKNOWN",
                f"No retained run state exists for acquisition run {run_id}.",
                remediation="Start the run with run_controller.py or pass an existing active --run-id.",
            )
        selected = _load_run_budget_state(state_path, requested_run_id=run_id)
        if selected["state"] in RUN_TERMINAL_STATES:
            raise FetchSourcesError(
                "ACQUISITION_RUN_TERMINAL",
                f"Acquisition run {run_id} is already terminal: {selected['state']}.",
                recoverable=False,
                remediation="Start a new run before acquiring additional evidence.",
            )
        return selected

    if not runs_root.is_dir():
        return None
    active: list[dict[str, Any]] = []
    for state_path in sorted(runs_root.glob("*/run-state.json")):
        selected = _load_run_budget_state(state_path, requested_run_id=None)
        if selected["state"] not in RUN_TERMINAL_STATES:
            active.append(selected)
    if not active:
        return None
    if len(active) > 1:
        raise FetchSourcesError(
            "ACQUISITION_RUN_ID_REQUIRED",
            "Multiple active runs exist; cumulative acquisition budget ownership is ambiguous.",
            remediation="Pass --run-id for the active run that owns this acquisition.",
        )
    return active[0]


def acquisition_context(
    project_root: Path,
    config: dict[str, Any],
    provider: str,
    requested_count: int,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    acquisition = acquisition_config(config)
    providers = validate_provider_list(
        acquisition.get("providers", []),
        "integrations.acquisition.providers",
        require_non_empty=True,
    )
    require_provider_allowed(provider, providers)
    limit = max_downloads_per_run(acquisition)
    enforce_max_downloads(requested_count, limit)
    license_required = require_license_check(acquisition)
    target_root = resolve_target_root(project_root, acquisition)
    return {
        "provider": provider,
        "enabled_providers": providers,
        "target_root": target_root,
        "max_downloads_per_run": limit,
        "require_license_check": license_required,
        "acquisition": acquisition,
        "run_budget": resolve_acquisition_run(project_root, run_id),
    }


def fetch_error_from_transport(exc: AcquisitionTransportError) -> FetchSourcesError:
    return FetchSourcesError(
        exc.error_code,
        exc.message,
        remediation=exc.remediation,
    )


def enforce_payload_size(payload: bytes, max_bytes: int, label: str) -> None:
    if len(payload) > max_bytes:
        raise FetchSourcesError(
            "ACQUISITION_CONTENT_TOO_LARGE",
            f"{label} response ({len(payload)} bytes) exceeds the configured limit of {max_bytes} bytes.",
            remediation="Raise the reviewed byte cap or acquire a smaller source artifact.",
        )


def bounded_fetch_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    headers: dict[str, str],
    allowed_domains: list[str] | None = None,
    resolve_hostnames: bool = True,
    expected_content_types: tuple[str, ...] | list[str],
) -> bytes:
    try:
        result = bounded_download(
            url,
            allowed_domains=allowed_domains,
            max_bytes=max_bytes,
            timeout=timeout,
            headers=headers,
            resolve_hostnames=resolve_hostnames,
            expected_content_types=expected_content_types,
        )
    except AcquisitionTransportError as exc:
        raise fetch_error_from_transport(exc) from exc
    return result.content


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def provenance_sidecar_path(target: Path) -> Path:
    return Path(str(target) + PROVENANCE_SIDECAR_SUFFIX)


def safe_workspace_path(project_root: Path, target: Path, label: str) -> Path:
    """Reject linked ancestors and paths that resolve outside the workspace."""
    root = project_root.resolve()
    try:
        relative = target.relative_to(project_root)
    except ValueError as exc:
        raise FetchSourcesError(
            "ACQUISITION_PATH_UNSAFE",
            f"{label} is outside the research workspace: {target}",
            remediation="Choose a canonical workspace-relative path without symlinked ancestors.",
        ) from exc
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise FetchSourcesError(
                "ACQUISITION_PATH_UNSAFE",
                f"{label} traverses a symbolic link: {current}",
                remediation="Replace the linked path with a real directory inside the workspace.",
            )
    resolved = target.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise FetchSourcesError(
            "ACQUISITION_PATH_UNSAFE",
            f"{label} resolves outside the research workspace: {target}",
            remediation="Choose a canonical workspace-relative path without symlinked ancestors.",
        )
    return target


def acquisition_marker_path(target: Path) -> Path:
    """Return the hidden marker proving an incomplete artifact is tool-owned."""
    return target.with_name(f".{target.name}{ACQUISITION_INCOMPLETE_SUFFIX}")


def acquisition_workspace_lock_path(project_root: Path) -> Path:
    return project_root.joinpath(*ACQUISITION_LOCK_RELATIVE)


def acquisition_target_lock_path(target: Path, project_root: Path | None = None) -> Path:
    normalized_target = os.path.normcase(str(target.resolve(strict=False)))
    identity = hashlib.sha256(normalized_target.encode("utf-8")).hexdigest()[:20]
    lock_root = (
        acquisition_workspace_lock_path(project_root).parent
        if project_root is not None
        else target.parent / ".locks"
    )
    return lock_root / f"acquisition-target-{identity}.lock"


def acquisition_marker_document(target: Path, project_root: Path | None = None) -> dict[str, Any]:
    relative_target = None
    if project_root is not None:
        try:
            relative_target = target.relative_to(project_root).as_posix()
        except ValueError:
            relative_target = None
    return {
        "schema_version": SCHEMA_VERSION,
        "document_type": "acquisition_incomplete_marker",
        "transaction_id": f"acq-{uuid.uuid4().hex}",
        "target_name": target.name,
        "target_path": relative_target,
        "started_at": timestamp_utc(),
    }


def write_acquisition_marker(target: Path, project_root: Path | None = None) -> Path:
    marker = acquisition_marker_path(target)
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("xb") as handle:
            handle.write((json.dumps(acquisition_marker_document(target, project_root), sort_keys=True) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise FetchSourcesError(
            "ACQUISITION_RECOVERY_REQUIRED",
            f"An incomplete acquisition marker already exists for {target}.",
            remediation="Retry through the acquisition command so the marker-backed output is quarantined safely.",
        ) from exc
    except OSError as exc:
        raise FetchSourcesError(
            "ACQUISITION_WRITE_FAILED",
            f"Cannot create incomplete acquisition marker for {target}: {exc}",
            remediation=(
                "Restore write access or free space and preserve any partial marker; "
                "inventory will refuse marker-backed output until recovery."
            ),
        ) from exc
    return marker


def validate_acquisition_marker(target: Path) -> dict[str, Any]:
    marker = acquisition_marker_path(target)
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FetchSourcesError(
            "ACQUISITION_MARKER_INVALID",
            f"Incomplete acquisition marker is unreadable for {target}: {exc}",
            recoverable=False,
            remediation="Preserve the marker and payload for operator review; do not promote or delete raw evidence automatically.",
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("document_type") != "acquisition_incomplete_marker"
        or document.get("target_name") != target.name
        or not isinstance(document.get("transaction_id"), str)
        or not document["transaction_id"].startswith("acq-")
    ):
        raise FetchSourcesError(
            "ACQUISITION_MARKER_INVALID",
            f"Incomplete acquisition marker does not match target {target}.",
            recoverable=False,
            remediation="Preserve the marker and payload for operator review; do not promote or delete raw evidence automatically.",
        )
    return document


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Write bytes through a same-directory temporary and atomic promotion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        # A hidden partial is deliberately retained when it cannot be moved or
        # removed. Source inventory ignores dot-prefixed paths, and the next
        # acquisition can quarantine it without treating it as evidence.
        raise FetchSourcesError(
            "ACQUISITION_WRITE_FAILED",
            f"Cannot atomically write acquisition output {path}: {exc}",
            remediation=(
                "Restore write access or free space, preserve the hidden partial, "
                "then retry so marker-backed output can be quarantined safely."
            ),
        ) from exc


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def quarantine_incomplete_artifact(target: Path) -> list[Path]:
    """Move only marker-backed acquisition outputs out of the evidence tree."""
    marker = acquisition_marker_path(target)
    if not marker.exists():
        return []
    validate_acquisition_marker(target)
    quarantine = target.parent / ".acquisition-quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    sidecar = provenance_sidecar_path(target)
    candidates = [target, sidecar, marker]
    candidates.extend(target.parent.glob(f".{target.name}.*.partial"))
    candidates.extend(sidecar.parent.glob(f".{sidecar.name}.*.partial"))
    candidates.extend(marker.parent.glob(f".{marker.name}.*.partial"))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            continue
        destination = quarantine / f"{path.name}.{uuid.uuid4().hex}.interrupted"
        path.replace(destination)
        moved.append(destination)
    return moved


def _target_from_sidecar(sidecar: Path) -> Path:
    return Path(str(sidecar)[: -len(PROVENANCE_SIDECAR_SUFFIX)])


def _sidecar_matches_run(document: dict[str, Any], run_budget: dict[str, Any]) -> bool:
    run_id = document.get("acquisition_run_id")
    if isinstance(run_id, str) and run_id.strip():
        return run_id.strip() == run_budget["run_id"]
    retrieved_at = parse_utc_timestamp(document.get("retrieved_at"))
    return retrieved_at is not None and retrieved_at >= run_budget["started_at"]


def retained_acquisition_usage(project_root: Path, run_budget: dict[str, Any]) -> dict[str, int]:
    """Count committed, deduplicated acquisition evidence for one retained run."""
    downloads = 0
    github_archive_bytes = 0
    seen_targets: set[str] = set()
    raw_root = project_root / "raw"
    if not raw_root.is_dir():
        return {"downloads": 0, "github_archive_bytes": 0}
    for sidecar in sorted(raw_root.rglob(f"*{PROVENANCE_SIDECAR_SUFFIX}")):
        if sidecar.is_symlink() or not sidecar.is_file():
            continue
        target = _target_from_sidecar(sidecar)
        if acquisition_marker_path(target).exists() or not target.exists():
            continue
        try:
            document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(document, dict) or not _sidecar_matches_run(document, run_budget):
            continue
        retrieved_by = document.get("retrieved_by")
        if not isinstance(retrieved_by, str) or not retrieved_by.startswith("fetch_sources.py/"):
            continue
        target_key = target.relative_to(project_root).as_posix()
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        downloads += 1
        artifact_kind = document.get("repository_artifact_kind")
        if artifact_kind != "source_archive":
            continue
        byte_count = document.get("byte_count")
        if isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count >= 0:
            github_archive_bytes += byte_count
        elif target.is_file():
            try:
                github_archive_bytes += target.stat().st_size
            except OSError:
                continue
    return {"downloads": downloads, "github_archive_bytes": github_archive_bytes}


def github_archive_run_limit(acquisition: dict[str, Any]) -> int:
    github = acquisition.get("github") if isinstance(acquisition.get("github"), dict) else {}
    value = github.get("max_archive_bytes", GITHUB_DEFAULT_MAX_ARCHIVE_BYTES)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SystemExit("research.yml integrations.acquisition.github.max_archive_bytes must be a positive integer")
    return value


def enforce_retained_acquisition_budget(
    project_root: Path,
    context: dict[str, Any],
    *,
    additional_downloads: int,
    additional_github_archive_bytes: int,
) -> dict[str, int]:
    run_budget = context.get("run_budget")
    if not isinstance(run_budget, dict):
        return {"downloads": 0, "github_archive_bytes": 0}
    usage = retained_acquisition_usage(project_root, run_budget)
    download_limit = int(context["max_downloads_per_run"])
    if usage["downloads"] + additional_downloads > download_limit:
        raise FetchSourcesError(
            "ACQUISITION_LIMIT_EXCEEDED",
            (
                f"Run {run_budget['run_id']} already retained {usage['downloads']} acquisition download(s); "
                f"the next commit would exceed max_downloads_per_run={download_limit}."
            ),
            remediation="Start a new run or raise max_downloads_per_run after reviewing provider limits.",
        )
    github_limit = github_archive_run_limit(context["acquisition"])
    if usage["github_archive_bytes"] + additional_github_archive_bytes > github_limit:
        raise FetchSourcesError(
            "GITHUB_ARCHIVE_BUDGET_EXCEEDED",
            (
                f"Run {run_budget['run_id']} already retained {usage['github_archive_bytes']} GitHub archive byte(s); "
                f"the next commit would exceed max_archive_bytes={github_limit}."
            ),
            remediation="Start a new run or raise the reviewed GitHub archive byte budget.",
        )
    return usage


def acquisition_provenance_fields(context: dict[str, Any], *, byte_count: int | None = None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    run_budget = context.get("run_budget")
    if isinstance(run_budget, dict):
        fields["acquisition_run_id"] = run_budget["run_id"]
    if byte_count is not None:
        fields["byte_count"] = byte_count
    return fields


@contextmanager
def acquisition_artifact_transaction(
    target: Path,
    *,
    project_root: Path | None = None,
    context: dict[str, Any] | None = None,
    additional_github_archive_bytes: int = 0,
):
    """Keep raw artifacts invisible unless both payload and provenance commit.

    The marker is created before promotion and removed only after the payload
    and provenance sidecar both exist. A crash therefore leaves positive proof
    that the next retry may quarantine the tool-owned incomplete output, while
    an unrelated user file is never moved implicitly.
    """
    with ExitStack() as locks:
        if project_root is not None:
            safe_workspace_path(project_root, target, "acquisition target")
            safe_workspace_path(project_root, provenance_sidecar_path(target), "acquisition provenance sidecar")
            safe_workspace_path(project_root, acquisition_marker_path(target), "acquisition transaction marker")
            locks.enter_context(
                workspace_lock(acquisition_workspace_lock_path(project_root), purpose="acquisition evidence commit")
            )
        locks.enter_context(
            workspace_lock(
                acquisition_target_lock_path(target, project_root),
                purpose=f"acquisition target {target.name}",
            )
        )
        ensure_download_target_available(target)
        if project_root is not None and context is not None:
            enforce_retained_acquisition_budget(
                project_root,
                context,
                additional_downloads=1,
                additional_github_archive_bytes=additional_github_archive_bytes,
            )
        marker = write_acquisition_marker(target, project_root)
        try:
            yield
            if not target.exists() or not provenance_sidecar_path(target).is_file():
                raise FetchSourcesError(
                    "ACQUISITION_COMMIT_INCOMPLETE",
                    f"Acquisition did not produce both payload and provenance for {target}.",
                    remediation="Retry the acquisition; marker-backed incomplete output will be quarantined first.",
                )
            marker.unlink()
        except BaseException as exc:
            try:
                quarantine_incomplete_artifact(target)
            except (OSError, FetchSourcesError):
                # Preserve marker-backed artifacts when quarantine itself is not safe.
                pass
            if isinstance(exc, OSError):
                raise FetchSourcesError(
                    "ACQUISITION_WRITE_FAILED",
                    f"Cannot commit acquisition payload and provenance for {target}: {exc}",
                    remediation=(
                        "Restore write access or free space, preserve the marker-backed output, "
                        "then retry so it can be quarantined safely."
                    ),
                ) from exc
            raise


def write_provenance_sidecar(
    target: Path,
    *,
    origin_url: str,
    license_value: str | None,
    retrieved_by: str,
    downloaded_pdf_url: str | None = None,
    downloaded_archive_url: str | None = None,
    repository_owner: str | None = None,
    repository_name: str | None = None,
    repository_full_name: str | None = None,
    repository_artifact_kind: str | None = None,
    repository_ref: str | None = None,
    commit_sha: str | None = None,
    academic_provider: str | None = None,
    academic_source_type: str | None = None,
    venue: str | None = None,
    publication_year: int | None = None,
    oa_status: str | None = None,
    peer_review_status: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    published: str | None = None,
    arxiv_id: str | None = None,
    openalex_work_id: str | None = None,
    openalex_publication_year: int | None = None,
    doi: str | None = None,
    doi_source: str | None = None,
    openalex_enrichment_status: str | None = None,
    openalex_enrichment_error: str | None = None,
    request_id: str | None = None,
    candidate_id: str | None = None,
    terms_url: str | None = None,
    terms_note: str | None = None,
    notes: str | None = None,
    retrieved_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    record: dict[str, Any] = {
        "origin_url": origin_url,
        "license": license_value,
        "retrieved_at": retrieved_at or timestamp_utc(),
        "retrieved_by": retrieved_by,
    }
    if downloaded_pdf_url:
        record["downloaded_pdf_url"] = downloaded_pdf_url
    if downloaded_archive_url:
        record["downloaded_archive_url"] = downloaded_archive_url
    if repository_owner:
        record["repository_owner"] = repository_owner
    if repository_name:
        record["repository_name"] = repository_name
    if repository_full_name:
        record["repository_full_name"] = repository_full_name
    if repository_artifact_kind:
        record["repository_artifact_kind"] = repository_artifact_kind
    if repository_ref:
        record["repository_ref"] = repository_ref
    if commit_sha:
        record["commit_sha"] = commit_sha
    if academic_provider:
        record["academic_provider"] = academic_provider
    if academic_source_type:
        record["academic_source_type"] = academic_source_type
    if venue:
        record["venue"] = venue
    if publication_year is not None:
        record["publication_year"] = publication_year
    if oa_status:
        record["oa_status"] = oa_status
    if peer_review_status:
        record["peer_review_status"] = peer_review_status
    if title:
        record["title"] = collapse_whitespace(title)
    if authors:
        clean_authors = [collapse_whitespace(author) for author in authors if isinstance(author, str) and author.strip()]
        if clean_authors:
            record["authors"] = clean_authors
    if published:
        record["published"] = collapse_whitespace(published)
    if arxiv_id:
        record["arxiv_id"] = arxiv_id
    if openalex_work_id:
        record["openalex_work_id"] = openalex_work_id
    if openalex_publication_year is not None:
        record["openalex_publication_year"] = openalex_publication_year
    if doi:
        record["doi"] = doi
    if doi_source:
        record["doi_source"] = doi_source
    if openalex_enrichment_status:
        record["openalex_enrichment_status"] = openalex_enrichment_status
    if openalex_enrichment_error:
        record["openalex_enrichment_error"] = openalex_enrichment_error
    if target.is_file():
        record["checksum"] = file_checksum(target)
    if request_id:
        record["request_id"] = request_id
    if candidate_id:
        record["candidate_id"] = candidate_id
    if terms_url:
        record["terms_url"] = terms_url
    if terms_note:
        record["terms_note"] = terms_note
    if notes:
        record["notes"] = notes
    for key, value in (extra or {}).items():
        if key in record or value is None:
            continue
        if isinstance(value, (str, int, float, bool, list, dict)) or value is None:
            record[key] = value
    sidecar = provenance_sidecar_path(target)
    atomic_write_text(sidecar, yaml.safe_dump(record, sort_keys=False))
    return sidecar


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=False)


def relative_label(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_workspace_output_path(project_root: Path, value: str) -> Path:
    relative = validate_workspace_relative_path(value, "fetch_sources output")
    return safe_workspace_path(project_root, project_root / relative, "fetch_sources output")


def read_workspace_mapping(project_root: Path, value: str, *, label: str) -> dict[str, Any]:
    relative = validate_workspace_relative_path(value, label)
    path = safe_workspace_path(project_root, project_root / relative, label)
    if not path.is_file():
        raise FetchSourcesError(
            "ACQUISITION_METADATA_MISSING",
            f"{label} does not exist: {value}",
            remediation="Write the selected candidate metadata file before acquiring the web snapshot.",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FetchSourcesError(
            "ACQUISITION_METADATA_UNREADABLE",
            f"Cannot read {label}: {value}",
            remediation="Check the workspace-relative path and file permissions, then retry.",
        ) from exc
    try:
        if path.suffix.lower() == ".json":
            document = json.loads(text)
        else:
            document = yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise FetchSourcesError(
            "ACQUISITION_METADATA_INVALID",
            f"{label} must be a JSON or YAML mapping: {value}",
            remediation="Fix the standards metadata file to contain one mapping object.",
        ) from exc
    if not isinstance(document, dict):
        raise FetchSourcesError(
            "ACQUISITION_METADATA_INVALID",
            f"{label} must be a JSON or YAML mapping: {value}",
            remediation="Fix the standards metadata file to contain one mapping object.",
        )
    return dict(document)


def normalize_arxiv_id(value: str) -> str:
    cleaned = value.strip()
    if cleaned.lower().startswith("arxiv:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    cleaned = cleaned.lower()
    if not ARXIV_ID_RE.match(cleaned):
        raise FetchSourcesError(
            "ARXIV_ID_INVALID",
            f"Invalid arXiv id {value!r}; expected a versioned new-style id like 2601.00001v1.",
            remediation="Pass a versioned post-2007 arXiv id such as 2601.00001v1.",
        )
    return cleaned


def parse_arxiv_id_list(value: str | None) -> list[str]:
    if value is None:
        return []
    ids = [item.strip() for item in value.split(",")]
    ids = [item for item in ids if item]
    if not ids:
        raise FetchSourcesError(
            "ARXIV_ID_INVALID",
            "--id-list must contain at least one arXiv id.",
            remediation="Pass a comma-separated list such as 2601.00001v1,2601.00002v2.",
        )
    return [normalize_arxiv_id(item) for item in ids]


def collapse_whitespace(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())


def child_text(element: ET.Element, path: str) -> str | None:
    found = element.find(path, ARXIV_ATOM_NS)
    if found is None or found.text is None:
        return None
    return collapse_whitespace(found.text)


def arxiv_id_from_entry_id(value: str | None) -> str | None:
    if not value:
        return None
    path = urlparse(value).path
    candidate = path.rsplit("/", 1)[-1] if path else value.rsplit("/", 1)[-1]
    candidate = candidate.removesuffix(".pdf").strip().lower()
    return candidate or None


def arxiv_abs_url(arxiv_id: str) -> str:
    return ARXIV_ABS_URL.format(id=arxiv_id)


def arxiv_pdf_url(arxiv_id: str) -> str:
    return ARXIV_PDF_URL.format(id=arxiv_id)


def arxiv_source_url(arxiv_id: str) -> str:
    return ARXIV_SOURCE_URL.format(id=arxiv_id)


def arxiv_publication_year(arxiv_id: str) -> int | None:
    match = re.match(r"^(?P<yy>\d{2})(?P<mm>\d{2})\.", arxiv_id)
    if not match:
        return None
    return 2000 + int(match.group("yy"))


def parse_arxiv_atom(payload: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)  # noqa: S314 - stdlib-only Atom parsing; no runtime XML dependency
    except ET.ParseError as exc:
        raise FetchSourcesError(
            "ACQUISITION_RESPONSE_INVALID",
            f"arXiv returned invalid Atom XML: {exc}",
            remediation="Retry later or inspect the provider response outside the workspace.",
        ) from exc

    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ARXIV_ATOM_NS):
        arxiv_id = arxiv_id_from_entry_id(child_text(entry, "atom:id"))
        if not arxiv_id:
            continue
        links = entry.findall("atom:link", ARXIV_ATOM_NS)
        abs_url = next(
            (
                link.get("href")
                for link in links
                if link.get("rel") == "alternate" and isinstance(link.get("href"), str)
            ),
            arxiv_abs_url(arxiv_id),
        )
        pdf_url = next(
            (
                link.get("href")
                for link in links
                if link.get("title") == "pdf" and isinstance(link.get("href"), str)
            ),
            arxiv_pdf_url(arxiv_id),
        )
        record: dict[str, Any] = {
            "id": arxiv_id,
            "title": child_text(entry, "atom:title") or "",
            "summary": child_text(entry, "atom:summary") or "",
            "authors": [
                name
                for author in entry.findall("atom:author", ARXIV_ATOM_NS)
                for name in [child_text(author, "atom:name")]
                if name
            ],
            "published": child_text(entry, "atom:published"),
            "updated": child_text(entry, "atom:updated"),
            "categories": [
                term
                for category in entry.findall("atom:category", ARXIV_ATOM_NS)
                for term in [category.get("term")]
                if isinstance(term, str) and term.strip()
            ],
            "abs_url": abs_url,
            "pdf_url": pdf_url,
            "source_url": arxiv_source_url(arxiv_id),
        }
        for key, path in (
            ("doi", "arxiv:doi"),
            ("comment", "arxiv:comment"),
            ("journal_ref", "arxiv:journal_ref"),
        ):
            value = child_text(entry, path)
            if value:
                record[key] = value
        results.append(record)
    return results


def arxiv_metadata_for_download(arxiv_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Best-effort provider metadata lookup for a download sidecar."""
    url = arxiv_query_url(None, [arxiv_id], 1)
    transport = active_arxiv_transport()
    try:
        arxiv_wait_for_rate_limit()
        payload = transport(url, ARXIV_TIMEOUT_SECONDS)
        if not isinstance(payload, bytes):
            raise FetchSourcesError(
                "ACQUISITION_RESPONSE_INVALID",
                "arXiv transport returned a non-byte response.",
                remediation="Fix the acquisition transport adapter and retry.",
            )
        enforce_payload_size(payload, ARXIV_DEFAULT_MAX_RESPONSE_BYTES, "arXiv")
        records = parse_arxiv_atom(payload)
    except HTTPError as exc:
        close_http_error(exc)
        return None, f"arXiv metadata lookup failed with HTTP {exc.code}."
    except (FetchSourcesError, TimeoutError, URLError, OSError) as exc:
        return None, f"arXiv metadata lookup failed: {exc}"
    for record in records:
        if record.get("id") == arxiv_id:
            return record, None
    return None, f"arXiv metadata lookup returned no exact record for {arxiv_id}."


def arxiv_metadata_sidecar_fields(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    fields: dict[str, Any] = {}
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        fields["title"] = title
    authors = metadata.get("authors")
    if isinstance(authors, list):
        fields["authors"] = [author for author in authors if isinstance(author, str) and author.strip()]
    published = metadata.get("published")
    if isinstance(published, str) and published.strip():
        fields["published"] = published
    doi = metadata.get("doi")
    if isinstance(doi, str) and doi.strip():
        fields["doi"] = doi.strip()
        fields["doi_source"] = "arxiv-atom"
    return fields


def arxiv_download_notes(metadata_warning: str | None) -> str:
    base = "License not inferable from provider metadata."
    if metadata_warning:
        return f"{base} {metadata_warning}"
    return base


def urllib_transport(url: str, timeout: float) -> bytes:
    path = urlparse(url).path
    if "/api/query" in path:
        expected_content_types = ARXIV_METADATA_CONTENT_TYPES
    elif "/pdf/" in path:
        expected_content_types = ARXIV_PDF_CONTENT_TYPES
    else:
        expected_content_types = ARXIV_SOURCE_CONTENT_TYPES
    return bounded_fetch_bytes(
        url,
        max_bytes=ARXIV_DEFAULT_MAX_RESPONSE_BYTES,
        timeout=timeout,
        allowed_domains=["arxiv.org", "export.arxiv.org"],
        headers={
            "User-Agent": "evidence-wiki fetch_sources.py/1.0; contact: local-workspace-agent",
        },
        resolve_hostnames=True,
        expected_content_types=expected_content_types,
    )


def active_arxiv_transport():
    return ARXIV_TRANSPORT or urllib_transport


def arxiv_wait_for_rate_limit() -> None:
    global ARXIV_LAST_REQUEST_AT
    now = float(ARXIV_CLOCK())
    if ARXIV_LAST_REQUEST_AT is not None:
        elapsed = now - ARXIV_LAST_REQUEST_AT
        if elapsed < ARXIV_REQUEST_INTERVAL_SECONDS:
            ARXIV_SLEEP(ARXIV_REQUEST_INTERVAL_SECONDS - elapsed)
            now = float(ARXIV_CLOCK())
    ARXIV_LAST_REQUEST_AT = now


def is_retryable_http_error(exc: HTTPError) -> bool:
    return exc.code == 429 or 500 <= exc.code <= 599


def retry_backoff_seconds(attempt: int) -> float:
    """Return deterministic exponential backoff bounded by a fixed ceiling."""
    exponent = max(0, int(attempt) - 1)
    return min(RETRY_BACKOFF_MAX_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (2**exponent))


def close_http_error(exc: HTTPError) -> None:
    """Close provider error response handles before wrapping them."""
    try:
        exc.close()
    except Exception:  # pragma: no cover - defensive cleanup only
        return


def arxiv_fetch_url(url: str) -> bytes:
    transport = active_arxiv_transport()
    last_error: BaseException | None = None
    for attempt in range(1, ARXIV_MAX_ATTEMPTS + 1):
        arxiv_wait_for_rate_limit()
        try:
            payload = transport(url, ARXIV_TIMEOUT_SECONDS)
            if not isinstance(payload, bytes):
                raise FetchSourcesError(
                    "ACQUISITION_RESPONSE_INVALID",
                    "arXiv transport returned a non-byte response.",
                    remediation="Fix the acquisition transport adapter and retry.",
                )
            enforce_payload_size(payload, ARXIV_DEFAULT_MAX_RESPONSE_BYTES, "arXiv")
            if not payload:
                raise FetchSourcesError(
                    "ACQUISITION_RESPONSE_INVALID",
                    f"arXiv returned an empty response for {redact_url(url)}.",
                    remediation="Retry later or inspect the provider response outside the workspace.",
                )
            return payload
        except FetchSourcesError:
            raise
        except HTTPError as exc:
            last_error = exc
            close_http_error(exc)
            if attempt < ARXIV_MAX_ATTEMPTS and is_retryable_http_error(exc):
                ARXIV_SLEEP(retry_backoff_seconds(attempt))
                continue
            raise FetchSourcesError(
                "ACQUISITION_NETWORK_ERROR",
                f"arXiv request failed with HTTP {exc.code}: {redact_url(url)}",
                remediation="Retry later, lower request volume, or inspect provider availability.",
            ) from None
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < ARXIV_MAX_ATTEMPTS:
                ARXIV_SLEEP(retry_backoff_seconds(attempt))
                continue
    raise FetchSourcesError(
        "ACQUISITION_NETWORK_ERROR",
        f"arXiv request failed after {ARXIV_MAX_ATTEMPTS} attempt(s): {redact_diagnostic(last_error)}",
        remediation="Retry later, check network access, or lower request volume.",
    )


def openalex_api_key() -> str | None:
    value = os.environ.get("OPENALEX_API_KEY")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def openalex_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": (
            "evidence-wiki fetch_sources.py/1.0; "
            "OpenAlex acquisition; set OPENALEX_API_KEY for higher usage limits"
        ),
    }


def urllib_openalex_transport(url: str, timeout: float, headers: dict[str, str]) -> bytes:
    # Unlike arXiv/GitHub, OpenAlex has no domain allowlist: work metadata
    # legitimately points at arbitrary open-access publisher hosts (the PDF
    # location), so this is the one real transport that must resolve and
    # reject hostnames pointing at non-public addresses on its own.
    return bounded_fetch_bytes(
        url,
        max_bytes=OPENALEX_DEFAULT_MAX_RESPONSE_BYTES,
        timeout=timeout,
        headers=headers,
        resolve_hostnames=True,
        expected_content_types=(
            OPENALEX_JSON_CONTENT_TYPES
            if (urlparse(url).hostname or "").lower() == "api.openalex.org"
            else OPENALEX_PDF_CONTENT_TYPES
        ),
    )


def active_openalex_transport():
    return OPENALEX_TRANSPORT or urllib_openalex_transport


def doi_headers() -> dict[str, str]:
    return {
        "Accept": "*/*",
        "User-Agent": "evidence-wiki fetch_sources.py/1.0; DOI corroboration",
    }


def urllib_doi_transport(url: str, timeout: float, headers: dict[str, str]) -> DownloadResult:
    validate_https_url(url, allowed_domains=["doi.org", "dx.doi.org"], resolve_hostnames=True)
    redirect_chain: list[str] = []
    opener = build_default_opener(
        allowed_domains=None,
        redirect_chain=redirect_chain,
        insecure_tls_reason=None,
        resolve_hostnames=True,
    )
    request = Request(url, headers=headers, method="HEAD")  # noqa: S310 - validated DOI HTTPS URL.
    try:
        with opener(request, timeout) as response:
            final_url = response_url(response, url)
            validate_https_url(final_url, resolve_hostnames=True)
            if final_url != url and final_url not in redirect_chain:
                redirect_chain.append(final_url)
            return result_from_bytes(
                b"",
                url=final_url,
                content_type=header_value(getattr(response, "headers", None), "Content-Type"),
                http_status=response_status(response),
                redirect_chain=redirect_chain,
            )
    except HTTPError as exc:
        final_url = response_url(exc, url)
        try:
            validate_https_url(final_url, resolve_hostnames=True)
            content_type = header_value(getattr(exc, "headers", None), "Content-Type")
        finally:
            exc.close()
        return result_from_bytes(
            b"",
            url=final_url,
            content_type=content_type,
            http_status=exc.code,
            redirect_chain=redirect_chain,
        )


def active_doi_transport():
    return DOI_TRANSPORT or urllib_doi_transport


def openalex_wait_for_rate_limit() -> None:
    global OPENALEX_LAST_REQUEST_AT
    now = float(OPENALEX_CLOCK())
    if OPENALEX_LAST_REQUEST_AT is not None:
        elapsed = now - OPENALEX_LAST_REQUEST_AT
        if elapsed < OPENALEX_REQUEST_INTERVAL_SECONDS:
            OPENALEX_SLEEP(OPENALEX_REQUEST_INTERVAL_SECONDS - elapsed)
            now = float(OPENALEX_CLOCK())
    OPENALEX_LAST_REQUEST_AT = now


def openalex_url(path: str, params: dict[str, str | int | None] | None = None) -> str:
    query: dict[str, str] = {
        key: str(value)
        for key, value in (params or {}).items()
        if value is not None
    }
    api_key = openalex_api_key()
    if api_key:
        query["api_key"] = api_key
    encoded = urlencode(query)
    return f"{OPENALEX_API_URL}{path}" + (f"?{encoded}" if encoded else "")


def redact_openalex_secret(value: Any) -> str:
    key = openalex_api_key()
    return redact_diagnostic(value, secrets=[key] if key else [])


def openalex_fetch_url(url: str) -> bytes:
    transport = active_openalex_transport()
    last_error: BaseException | None = None
    safe_url = redact_openalex_secret(url)
    for attempt in range(1, OPENALEX_MAX_ATTEMPTS + 1):
        openalex_wait_for_rate_limit()
        try:
            payload = transport(url, OPENALEX_TIMEOUT_SECONDS, openalex_headers())
            if not isinstance(payload, bytes):
                raise FetchSourcesError(
                    "ACQUISITION_RESPONSE_INVALID",
                    "OpenAlex transport returned a non-byte response.",
                    remediation="Fix the acquisition transport adapter and retry.",
                )
            enforce_payload_size(payload, OPENALEX_DEFAULT_MAX_RESPONSE_BYTES, "OpenAlex")
            if not payload:
                raise FetchSourcesError(
                    "ACQUISITION_RESPONSE_INVALID",
                    f"OpenAlex returned an empty response for {safe_url}.",
                    remediation="Retry later or inspect the provider response outside the workspace.",
                )
            return payload
        except FetchSourcesError:
            raise
        except HTTPError as exc:
            last_error = exc
            close_http_error(exc)
            if exc.code in {401, 403}:
                raise FetchSourcesError(
                    "OPENALEX_AUTH_REQUIRED",
                    f"OpenAlex request failed with HTTP {exc.code}: {safe_url}",
                    remediation=(
                        "Set OPENALEX_API_KEY in the process environment, verify the key, "
                        "and rerun the explicit acquisition command."
                    ),
                ) from None
            if exc.code == 429:
                if attempt < OPENALEX_MAX_ATTEMPTS:
                    OPENALEX_SLEEP(retry_backoff_seconds(attempt))
                    continue
                raise FetchSourcesError(
                    "OPENALEX_RATE_LIMITED",
                    f"OpenAlex request was rate limited with HTTP 429: {safe_url}",
                    remediation="Retry later, reduce request volume, or set OPENALEX_API_KEY for a larger usage budget.",
                ) from None
            if attempt < OPENALEX_MAX_ATTEMPTS and 500 <= exc.code <= 599:
                OPENALEX_SLEEP(retry_backoff_seconds(attempt))
                continue
            raise FetchSourcesError(
                "ACQUISITION_NETWORK_ERROR",
                f"OpenAlex request failed with HTTP {exc.code}: {safe_url}",
                remediation="Retry later, check network access, or lower request volume.",
            ) from None
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < OPENALEX_MAX_ATTEMPTS:
                OPENALEX_SLEEP(retry_backoff_seconds(attempt))
                continue
    raise FetchSourcesError(
        "ACQUISITION_NETWORK_ERROR",
        f"OpenAlex request failed after {OPENALEX_MAX_ATTEMPTS} attempt(s): {redact_openalex_secret(last_error)}",
        remediation="Retry later, check network access, or lower request volume.",
    )


def openalex_json_response(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchSourcesError(
            "ACQUISITION_RESPONSE_INVALID",
            f"OpenAlex returned invalid JSON: {exc}",
            remediation="Retry later or inspect the provider response outside the workspace.",
        ) from exc
    if not isinstance(document, dict):
        raise FetchSourcesError(
            "ACQUISITION_RESPONSE_INVALID",
            "OpenAlex returned JSON that was not an object.",
            remediation="Retry later or inspect the provider response outside the workspace.",
        )
    return document


def normalize_openalex_title(value: str | None) -> str:
    return collapse_whitespace(value or "").casefold()


def compact_openalex_location(location: Any) -> dict[str, Any] | None:
    if not isinstance(location, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("is_oa", "landing_page_url", "pdf_url", "license", "license_id"):
        value = location.get(key)
        if isinstance(value, (str, bool)) or value is None:
            result[key] = value
    source = location.get("source")
    if isinstance(source, dict):
        display_name = source.get("display_name")
        if isinstance(display_name, str) and display_name.strip():
            result["source_display_name"] = collapse_whitespace(display_name)
    return result


def location_license(location: dict[str, Any]) -> str | None:
    _slug, spdx = location_license_details(location)
    return spdx


def location_license_details(location: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("license", "license_id"):
        value = location.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            spdx = OPENALEX_LICENSE_TO_SPDX.get(text.casefold())
            return text, spdx if spdx in SPDX_LICENSE_IDS else None
    return None, None


def openalex_location_venue(location: dict[str, Any] | None) -> str | None:
    if not isinstance(location, dict):
        return None
    source = location.get("source")
    if not isinstance(source, dict):
        return None
    display_name = source.get("display_name")
    if isinstance(display_name, str) and display_name.strip():
        return collapse_whitespace(display_name)
    return None


def openalex_location_candidates(work: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("best_oa_location", "primary_location"):
        value = work.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    locations = work.get("locations")
    if isinstance(locations, list):
        candidates.extend(location for location in locations if isinstance(location, dict))
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for location in candidates:
        marker = (
            location.get("landing_page_url") if isinstance(location.get("landing_page_url"), str) else None,
            location.get("pdf_url") if isinstance(location.get("pdf_url"), str) else None,
        )
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(location)
    return unique


def http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def select_openalex_oa_pdf_location(work: dict[str, Any]) -> dict[str, Any] | None:
    for location in openalex_location_candidates(work):
        pdf_url = http_url(location.get("pdf_url"))
        if location.get("is_oa") is True and pdf_url:
            return location
    return None


def clean_openalex_doi(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.lower().startswith("doi:"):
        text = text.split(":", 1)[1].strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {"doi.org", "dx.doi.org"}:
        text = parsed.path.lstrip("/")
    return text if OPENALEX_DOI_RE.match(text) else None


def openalex_work_id_from_work(work: dict[str, Any]) -> str | None:
    value = work.get("id")
    if not isinstance(value, str):
        return None
    try:
        return openalex_work_id(value)
    except FetchSourcesError:
        return None


def openalex_work_venue(work: dict[str, Any], preferred_location: dict[str, Any] | None = None) -> str | None:
    for location in [preferred_location, work.get("primary_location"), work.get("best_oa_location")]:
        venue = openalex_location_venue(location if isinstance(location, dict) else None)
        if venue:
            return venue
    for location in openalex_location_candidates(work):
        venue = openalex_location_venue(location)
        if venue:
            return venue
    return None


def openalex_work_license(work: dict[str, Any], preferred_location: dict[str, Any] | None = None) -> str | None:
    _slug, spdx = openalex_work_license_details(work, preferred_location=preferred_location)
    return spdx


def openalex_work_license_details(
    work: dict[str, Any],
    preferred_location: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    for location in [preferred_location, *openalex_location_candidates(work)]:
        if not isinstance(location, dict):
            continue
        slug, spdx = location_license_details(location)
        if slug:
            return slug, spdx
    return None, None


def openalex_author_names(work: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    raw_authors = work.get("authors")
    if isinstance(raw_authors, list):
        for author in raw_authors:
            if isinstance(author, str) and author.strip():
                authors.append(collapse_whitespace(author))
            elif isinstance(author, dict):
                name = author.get("display_name") or author.get("name")
                if isinstance(name, str) and name.strip():
                    authors.append(collapse_whitespace(name))
    authorships = work.get("authorships")
    if isinstance(authorships, list):
        for authorship in authorships:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author")
            if isinstance(author, dict):
                name = author.get("display_name")
                if isinstance(name, str) and name.strip():
                    authors.append(collapse_whitespace(name))
    result: list[str] = []
    for author in authors:
        if author not in result:
            result.append(author)
    return result


def sidecar_author_names(sidecar_data: dict[str, Any]) -> list[str]:
    authors = sidecar_data.get("authors")
    if not isinstance(authors, list):
        return []
    return [collapse_whitespace(author) for author in authors if isinstance(author, str) and author.strip()]


def resolved_url_matches_arxiv_abs(final_url: str | None, arxiv_id: str) -> bool:
    if not final_url:
        return False
    parsed = urlparse(final_url)
    host = (parsed.hostname or "").casefold()
    if host != "arxiv.org":
        return False
    path = parsed.path.rstrip("/").casefold()
    versionless = versionless_arxiv_id(arxiv_id).casefold()
    return path in {
        f"/abs/{versionless}",
        f"/abs/{normalize_arxiv_id(arxiv_id).casefold()}",
    }


def doi_resolution_error_payload(status: str, *, error: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "resolved_url": None, "matches_arxiv_id": False}
    if error:
        result["error"] = collapse_whitespace(error)
    return result


def datacite_arxiv_doi_resolution(doi: str | None, arxiv_id: str | None) -> dict[str, Any]:
    if not doi or not arxiv_id:
        return {"status": "not_attempted", "matches_arxiv_id": False}
    versionless = versionless_arxiv_id(arxiv_id)
    expected = datacite_arxiv_doi(versionless)
    if doi.casefold() != expected.casefold():
        return {"status": "doi_mismatch", "resolved_url": None, "matches_arxiv_id": False}
    url = f"https://doi.org/{quote(expected, safe='/:')}"
    try:
        result = active_doi_transport()(url, DOI_TIMEOUT_SECONDS, doi_headers())
    except (AcquisitionTransportError, TimeoutError, URLError, OSError) as exc:
        return doi_resolution_error_payload("network_error", error=str(exc))

    status = result.http_status
    if status is not None and not (200 <= int(status) < 400):
        return doi_resolution_error_payload("not_found", error=f"HTTP {status}")
    resolved_url = result.final_url
    matches = resolved_url_matches_arxiv_abs(resolved_url, arxiv_id)
    return {
        "status": "resolved" if matches else "redirect_mismatch",
        "resolved_url": resolved_url,
        "matches_arxiv_id": matches,
    }


OPENALEX_IDENTITY_FIELDS = (
    "openalex_title_lag",
    "openalex_identity_conflict",
    "openalex_reported_title",
    "openalex_reported_authors",
    "openalex_reported_publication_year",
    "openalex_identity_evidence",
    "doi_resolution",
)


def apply_openalex_identity_evidence(
    sidecar_data: dict[str, Any],
    work: dict[str, Any],
    *,
    canonical_doi: str | None,
) -> None:
    for key in OPENALEX_IDENTITY_FIELDS:
        sidecar_data.pop(key, None)

    local_title = collapse_whitespace(sidecar_data.get("title")) if isinstance(sidecar_data.get("title"), str) else None
    provider_title = collapse_whitespace(work.get("display_name")) if isinstance(work.get("display_name"), str) else None
    local_authors = sidecar_author_names(sidecar_data)
    provider_authors = openalex_author_names(work)
    author_comparison = author_sets_match(local_authors, provider_authors)
    local_year = sidecar_data.get("publication_year") if isinstance(sidecar_data.get("publication_year"), int) else None
    provider_year = work.get("publication_year") if isinstance(work.get("publication_year"), int) else None
    title_matched = (
        bool(local_title and provider_title)
        and normalize_openalex_title(local_title) == normalize_openalex_title(provider_title)
    )

    evidence = {
        "local_title": local_title,
        "openalex_title": provider_title,
        "title": "matched" if title_matched else "mismatch",
        "authors": "matched" if author_comparison["matched"] else "mismatch",
        "author_matches": author_comparison["matches"],
        "local_publication_year": local_year,
        "openalex_publication_year": provider_year,
    }
    if title_matched:
        return

    sidecar_data["openalex_reported_title"] = provider_title
    if provider_year is not None:
        sidecar_data["openalex_reported_publication_year"] = provider_year
    sidecar_data["openalex_identity_evidence"] = evidence
    if author_comparison["matched"]:
        sidecar_data["openalex_title_lag"] = True
        return

    sidecar_data["openalex_identity_conflict"] = True
    sidecar_data["openalex_reported_authors"] = provider_authors
    sidecar_data["doi_resolution"] = datacite_arxiv_doi_resolution(
        canonical_doi,
        sidecar_data.get("arxiv_id") if isinstance(sidecar_data.get("arxiv_id"), str) else None,
    )


def openalex_oa_status(work: dict[str, Any]) -> str | None:
    open_access = work.get("open_access")
    if not isinstance(open_access, dict):
        return None
    value = open_access.get("oa_status")
    return collapse_whitespace(value) if isinstance(value, str) and value.strip() else None


def openalex_academic_source_type(
    work: dict[str, Any],
    venue: str | None,
    *,
    metadata_only: bool = False,
) -> str | None:
    if metadata_only:
        return "metadata_only"
    work_type = work.get("type")
    text = collapse_whitespace(work_type) if isinstance(work_type, str) else None
    if text == "preprint":
        return "preprint"
    if text == "article" and venue:
        return "journal_article"
    return text


def openalex_peer_review_status(work: dict[str, Any], venue: str | None) -> str:
    work_type = work.get("type")
    if isinstance(work_type, str) and work_type.strip().casefold() == "preprint":
        return "preprint"
    if venue:
        return "publisher_indexed"
    return "unknown"


def openalex_academic_metadata(
    work: dict[str, Any],
    *,
    preferred_location: dict[str, Any] | None = None,
    metadata_only: bool = False,
) -> dict[str, Any]:
    venue = openalex_work_venue(work, preferred_location)
    return {
        "academic_provider": "openalex",
        "academic_source_type": openalex_academic_source_type(work, venue, metadata_only=metadata_only),
        "venue": venue,
        "publication_year": work.get("publication_year") if isinstance(work.get("publication_year"), int) else None,
        "oa_status": openalex_oa_status(work),
        "peer_review_status": openalex_peer_review_status(work, venue),
        "openalex_work_id": openalex_work_id_from_work(work),
        "doi": clean_openalex_doi(work.get("doi")),
    }


def compact_openalex_work(work: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source_key, target_key in (
        ("id", "id"),
        ("doi", "doi"),
        ("display_name", "display_name"),
        ("publication_year", "publication_year"),
        ("type", "type"),
    ):
        value = work.get(source_key)
        if isinstance(value, (str, int)) or value is None:
            result[target_key] = collapse_whitespace(value) if isinstance(value, str) else value
    open_access = work.get("open_access")
    if isinstance(open_access, dict):
        result["open_access"] = {
            key: value
            for key in ("is_oa", "oa_status", "oa_url")
            for value in [open_access.get(key)]
            if isinstance(value, (str, bool)) or value is None
        }
    location = select_openalex_oa_pdf_location(work)
    provider_license_slug, license_value = openalex_work_license_details(work, preferred_location=location)
    if provider_license_slug:
        result["provider_license_slug"] = provider_license_slug
    if license_value:
        result["license"] = license_value
    if location:
        result["oa_pdf_url"] = http_url(location.get("pdf_url"))
        landing_page_url = http_url(location.get("landing_page_url"))
        if landing_page_url:
            result["oa_landing_page_url"] = landing_page_url
        compact_location = compact_openalex_location(location)
        if compact_location:
            result["oa_location"] = compact_location
    return result


def deduplicate_openalex_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep provider order while collapsing repeated identities.

    OpenAlex searches can repeat the same work through different result paths.
    A duplicate must not create false title ambiguity, while distinct work IDs or
    DOIs with the same title must never be collapsed into one identity.
    """
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        work_id = result.get("id") if isinstance(result.get("id"), str) else None
        doi = result.get("doi") if isinstance(result.get("doi"), str) else None
        if work_id:
            marker = ("id", work_id.casefold())
        elif doi:
            marker = ("doi", doi.casefold())
        else:
            marker = (
                "metadata",
                json.dumps(
                    {
                        "display_name": result.get("display_name"),
                        "publication_year": result.get("publication_year"),
                    },
                    sort_keys=True,
                ),
            )
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(result)
    return unique


def openalex_work_api_path(value: str) -> str:
    return f"/works/{quote(value, safe='')}"


def openalex_work_id(value: str) -> str:
    candidate = value.strip()
    if candidate.lower().startswith("openalex:"):
        candidate = candidate.split(":", 1)[1].strip()
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower().endswith("openalex.org"):
        candidate = parsed.path.rsplit("/", 1)[-1]
    if not OPENALEX_ID_RE.match(candidate):
        raise FetchSourcesError(
            "OPENALEX_ID_INVALID",
            f"Invalid OpenAlex work id {value!r}; expected an id like W260100001.",
            remediation="Pass an OpenAlex work id such as W260100001 or https://openalex.org/W260100001.",
        )
    return candidate.upper()


def normalize_openalex_id_or_doi(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise FetchSourcesError(
            "OPENALEX_ID_INVALID",
            "--id-or-doi must be a non-empty OpenAlex work id or DOI.",
            remediation="Pass an OpenAlex work id such as W260100001 or a DOI such as 10.5555/example.",
        )
    try:
        return openalex_work_id(candidate)
    except FetchSourcesError:
        pass
    lowered = candidate.lower()
    if lowered.startswith("doi:"):
        doi = candidate.split(":", 1)[1].strip()
    else:
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc.lower() in {"doi.org", "dx.doi.org"}:
            doi = parsed.path.lstrip("/")
        else:
            doi = candidate
    if OPENALEX_DOI_RE.match(doi):
        return f"doi:{doi}"
    raise FetchSourcesError(
        "OPENALEX_ID_INVALID",
        f"Invalid OpenAlex id or DOI {value!r}; expected W... or a DOI such as 10.5555/example.",
        remediation="Pass an explicit OpenAlex work id from resolve output or a DOI.",
    )


def openalex_api_notice() -> dict[str, Any]:
    api_key = openalex_api_key()
    if api_key:
        return {"api_key_used": True}
    return {
        "api_key_used": False,
        "notice": "OPENALEX_API_KEY is not set; using keyless OpenAlex access with conservative request pacing.",
    }


def openalex_get_work_by_identifier(identifier: str) -> dict[str, Any]:
    url = openalex_url(openalex_work_api_path(identifier), {"select": OPENALEX_SELECT_FIELDS})
    return openalex_json_response(openalex_fetch_url(url))


def openalex_get_work_by_id(work_id: str) -> dict[str, Any]:
    return openalex_get_work_by_identifier(openalex_work_id(work_id))


def openalex_fetch_work_for_enrichment(identifier: str) -> dict[str, Any] | None:
    url = openalex_url(openalex_work_api_path(identifier), {"select": OPENALEX_SELECT_FIELDS})
    safe_url = redact_openalex_secret(url)
    transport = active_openalex_transport()
    last_error: BaseException | None = None
    for attempt in range(1, OPENALEX_MAX_ATTEMPTS + 1):
        openalex_wait_for_rate_limit()
        try:
            payload = transport(url, OPENALEX_TIMEOUT_SECONDS, openalex_headers())
            if not isinstance(payload, bytes):
                raise FetchSourcesError(
                    "ACQUISITION_RESPONSE_INVALID",
                    "OpenAlex transport returned a non-byte response.",
                    remediation="Fix the acquisition transport adapter and retry.",
                )
            enforce_payload_size(payload, OPENALEX_DEFAULT_MAX_RESPONSE_BYTES, "OpenAlex")
            return openalex_json_response(payload)
        except FetchSourcesError:
            raise
        except HTTPError as exc:
            last_error = exc
            close_http_error(exc)
            if exc.code == 404:
                return None
            if exc.code in {401, 403}:
                raise FetchSourcesError(
                    "OPENALEX_AUTH_REQUIRED",
                    f"OpenAlex request failed with HTTP {exc.code}: {safe_url}",
                    remediation=(
                        "Set OPENALEX_API_KEY in the process environment, verify the key, "
                        "and rerun the explicit acquisition command."
                    ),
                ) from None
            if exc.code == 429:
                if attempt < OPENALEX_MAX_ATTEMPTS:
                    OPENALEX_SLEEP(retry_backoff_seconds(attempt))
                    continue
                raise FetchSourcesError(
                    "OPENALEX_RATE_LIMITED",
                    f"OpenAlex request was rate limited with HTTP 429: {safe_url}",
                    remediation="Retry later, reduce request volume, or set OPENALEX_API_KEY for a larger usage budget.",
                ) from None
            if attempt < OPENALEX_MAX_ATTEMPTS and 500 <= exc.code <= 599:
                OPENALEX_SLEEP(retry_backoff_seconds(attempt))
                continue
            raise FetchSourcesError(
                "ACQUISITION_NETWORK_ERROR",
                f"OpenAlex request failed with HTTP {exc.code}: {safe_url}",
                remediation="Retry later, check network access, or lower request volume.",
            ) from None
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < OPENALEX_MAX_ATTEMPTS:
                OPENALEX_SLEEP(retry_backoff_seconds(attempt))
                continue
    raise FetchSourcesError(
        "ACQUISITION_NETWORK_ERROR",
        f"OpenAlex request failed after {OPENALEX_MAX_ATTEMPTS} attempt(s): {redact_openalex_secret(last_error)}",
        remediation="Retry later, check network access, or lower request volume.",
    )


def manifest_path_for_workspace(project_root: Path) -> Path:
    config = load_config(project_root)
    sources_config = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    manifest_path = validate_workspace_relative_path(
        sources_config.get("manifest_path", "sources/manifest.jsonl"),
        "sources.manifest_path",
    )
    return project_root / manifest_path


def load_manifest_records(project_root: Path) -> list[dict[str, Any]]:
    manifest_path = manifest_path_for_workspace(project_root)
    if not manifest_path.is_file():
        raise FetchSourcesError(
            "MANIFEST_MISSING",
            f"Cannot enrich OpenAlex identity without a manifest: {manifest_path}",
            remediation="Run source_inventory.py after acquisition, then rerun openalex enrich.",
        )
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FetchSourcesError(
                "MANIFEST_INVALID",
                f"Invalid JSONL in {manifest_path}:{line_number}: {exc}",
                remediation="Repair or regenerate the source manifest, then rerun openalex enrich.",
            ) from exc
        if isinstance(record, dict):
            records.append(record)
    return records


def record_provenance(record: dict[str, Any]) -> dict[str, Any]:
    provenance = record.get("provenance")
    return provenance if isinstance(provenance, dict) else {}


def sidecar_path_for_record(project_root: Path, record: dict[str, Any]) -> Path | None:
    provenance = record_provenance(record)
    sidecar_path = provenance.get("sidecar_path")
    if isinstance(sidecar_path, str) and sidecar_path.strip():
        return project_root / validate_workspace_relative_path(sidecar_path, "provenance.sidecar_path")
    raw_paths = record.get("raw_paths")
    if isinstance(raw_paths, list):
        raw_path = next((path for path in raw_paths if isinstance(path, str) and path.strip()), None)
        if raw_path:
            return provenance_sidecar_path(project_root / validate_workspace_relative_path(raw_path, "raw_paths[]"))
    return None


def load_sidecar_mapping(sidecar: Path) -> dict[str, Any]:
    if not sidecar.is_file():
        raise FetchSourcesError(
            "SIDECAR_MISSING",
            f"Cannot enrich missing provenance sidecar: {sidecar}",
            remediation="Run source_inventory.py to refresh provenance paths or reacquire the source.",
        )
    document = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise FetchSourcesError(
            "SIDECAR_INVALID",
            f"Provenance sidecar must be a YAML mapping: {sidecar}",
            remediation="Repair the sidecar or reacquire the source.",
        )
    return document


def write_sidecar_mapping(sidecar: Path, data: dict[str, Any]) -> None:
    atomic_write_text(sidecar, yaml.safe_dump(data, sort_keys=False))


def versionless_arxiv_id(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id, flags=re.IGNORECASE)


def datacite_arxiv_doi(arxiv_id: str) -> str:
    return f"10.48550/arXiv.{versionless_arxiv_id(arxiv_id)}"


def selected_openalex_enrichment_records(records: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.source_ids:
        requested = set(args.source_ids)
        selected = [record for record in records if isinstance(record.get("id"), str) and record["id"] in requested]
        found = {record["id"] for record in selected if isinstance(record.get("id"), str)}
        missing = sorted(requested - found)
        if missing:
            raise FetchSourcesError(
                "SOURCE_NOT_FOUND",
                f"Manifest does not contain requested source id(s): {', '.join(missing)}",
                remediation="Run source_inventory.py or pass a source id from sources/manifest.jsonl.",
            )
        return selected
    selected = []
    for record in records:
        provenance = record_provenance(record)
        if provenance.get("retrieved_by") != "fetch_sources.py/arxiv":
            continue
        if args.request_id and provenance.get("request_id") != args.request_id:
            continue
        selected.append(record)
    return selected


def sidecar_lookup_doi(sidecar_data: dict[str, Any]) -> tuple[str | None, str | None]:
    doi = sidecar_data.get("doi")
    if isinstance(doi, str) and doi.strip():
        source = sidecar_data.get("doi_source")
        return doi.strip(), source.strip() if isinstance(source, str) and source.strip() else "arxiv-atom"
    arxiv_id = sidecar_data.get("arxiv_id")
    if isinstance(arxiv_id, str) and arxiv_id.strip():
        return datacite_arxiv_doi(normalize_arxiv_id(arxiv_id)), "datacite-derived"
    return None, None


def enrich_openalex_sidecar(project_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    source_id = record.get("id") if isinstance(record.get("id"), str) else "<unknown>"
    sidecar = sidecar_path_for_record(project_root, record)
    if sidecar is None:
        return {"source_id": source_id, "status": "skipped_missing_sidecar", "network_io_executed": False}
    sidecar_data = load_sidecar_mapping(sidecar)
    if sidecar_data.get("retrieved_by") != "fetch_sources.py/arxiv":
        return {
            "source_id": source_id,
            "status": "skipped_not_arxiv",
            "sidecar_path": relative_label(project_root, sidecar),
            "network_io_executed": False,
        }
    lookup_doi, doi_source = sidecar_lookup_doi(sidecar_data)
    if lookup_doi is None or doi_source is None:
        sidecar_data["openalex_enrichment_status"] = "unresolved"
        sidecar_data["openalex_enrichment_error"] = "missing_arxiv_id_or_doi"
        write_sidecar_mapping(sidecar, sidecar_data)
        return {
            "source_id": source_id,
            "status": "unresolved",
            "reason": "missing_arxiv_id_or_doi",
            "sidecar_path": relative_label(project_root, sidecar),
            "network_io_executed": False,
        }

    identifier = normalize_openalex_id_or_doi(lookup_doi)
    work = openalex_fetch_work_for_enrichment(identifier)
    if work is None:
        sidecar_data["openalex_enrichment_status"] = "unresolved"
        sidecar_data["openalex_enrichment_error"] = "not_found"
        sidecar_data["doi_source"] = doi_source
        write_sidecar_mapping(sidecar, sidecar_data)
        return {
            "source_id": source_id,
            "status": "unresolved",
            "reason": "not_found",
            "doi_source": doi_source,
            "sidecar_path": relative_label(project_root, sidecar),
            "network_io_executed": True,
        }

    provider_license_slug, license_value = openalex_work_license_details(work)
    openalex_year = work.get("publication_year") if isinstance(work.get("publication_year"), int) else None
    sidecar_data["openalex_enrichment_status"] = "resolved"
    sidecar_data.pop("openalex_enrichment_error", None)
    work_id = openalex_work_id_from_work(work)
    if work_id:
        sidecar_data["openalex_work_id"] = work_id
    canonical_doi = clean_openalex_doi(work.get("doi"))
    if canonical_doi:
        sidecar_data["doi"] = canonical_doi
    sidecar_data["doi_source"] = doi_source
    if provider_license_slug:
        sidecar_data["provider_license_slug"] = provider_license_slug
        sidecar_data["license_source"] = "openalex"
    if license_value:
        sidecar_data["license"] = license_value
    elif provider_license_slug:
        sidecar_data["license"] = "unresolved"
        if not isinstance(sidecar_data.get("terms_url"), str) or not sidecar_data["terms_url"].strip():
            origin_url = sidecar_data.get("origin_url")
            if isinstance(origin_url, str) and origin_url.strip():
                sidecar_data["terms_url"] = origin_url
    oa_status = openalex_oa_status(work)
    if oa_status:
        sidecar_data["oa_status"] = oa_status
    if openalex_year is not None:
        sidecar_data["openalex_publication_year"] = openalex_year
    apply_openalex_identity_evidence(sidecar_data, work, canonical_doi=canonical_doi or lookup_doi)
    write_sidecar_mapping(sidecar, sidecar_data)
    return {
        "source_id": source_id,
        "status": "resolved",
        "doi_source": doi_source,
        "openalex_work_id": work_id,
        "sidecar_path": relative_label(project_root, sidecar),
        "network_io_executed": True,
    }


def run_openalex_resolve(_project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    query = collapse_whitespace(args.query)
    if not query:
        raise FetchSourcesError(
            "CONFIG_INVALID",
            "openalex resolve requires a non-empty --query value.",
            remediation="Pass the exact work title to resolve before downloading.",
        )
    url = openalex_url(
        "/works",
        {
            "search": query,
            "per_page": args.max_results,
            "select": OPENALEX_SELECT_FIELDS,
        },
    )
    document = openalex_json_response(openalex_fetch_url(url))
    raw_results = document.get("results")
    if not isinstance(raw_results, list):
        raise FetchSourcesError(
            "ACQUISITION_RESPONSE_INVALID",
            "OpenAlex resolve response did not contain a results list.",
            remediation="Retry later or inspect the provider response outside the workspace.",
        )
    provider_results = [compact_openalex_work(work) for work in raw_results if isinstance(work, dict)]
    results = deduplicate_openalex_results(provider_results)
    normalized_query = normalize_openalex_title(query)
    exact_results = [
        result
        for result in results
        if normalize_openalex_title(result.get("display_name") if isinstance(result.get("display_name"), str) else None)
        == normalized_query
    ]
    ambiguous = len(exact_results) > 1
    if ambiguous and not args.allow_unconfirmed:
        raise FetchSourcesError(
            "OPENALEX_RESOLUTION_AMBIGUOUS",
            f"OpenAlex resolution returned multiple distinct exact-title works for query {query!r}.",
            remediation="Inspect candidates manually, then use openalex get --id-or-doi with an explicit OpenAlex ID or DOI.",
        )
    if not exact_results and not args.allow_unconfirmed:
        raise FetchSourcesError(
            "OPENALEX_RESOLUTION_UNCERTAIN",
            f"OpenAlex resolution returned no exact-enough result for query {query!r}.",
            remediation="Inspect candidates manually, then use openalex get --id-or-doi with an explicit OpenAlex ID or DOI.",
        )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provider": "openalex",
        "command": "resolve",
        "entity": args.entity,
        "query": query,
        "max_results": args.max_results,
        "provider_result_count": len(provider_results),
        "count": len(results),
        "candidate_count": len(results),
        "exact_match_count": len(exact_results),
        "resolved": exact_results[0] if len(exact_results) == 1 else None,
        "resolution_status": "ambiguous" if ambiguous else ("resolved" if exact_results else "unconfirmed"),
        "results": results,
        **openalex_api_notice(),
    }
    if ambiguous:
        report["recommended_action"] = "review"
        report["ambiguity_reason"] = "multiple_distinct_exact_title_matches"
    elif not exact_results:
        report["recommended_action"] = "review"
        report["limitation"] = NEGATIVE_CLAIM_PROBE_LIMITATION
    return report


def run_openalex_get(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    identifier = normalize_openalex_id_or_doi(args.id_or_doi)
    raw_work = openalex_get_work_by_identifier(identifier)
    work = compact_openalex_work(raw_work)
    report = {
        "schema_version": SCHEMA_VERSION,
        "provider": "openalex",
        "command": "get",
        "id_or_doi": args.id_or_doi,
        "work": work,
        **openalex_api_notice(),
    }
    if args.output:
        target_root = context["target_root"]
        target = resolve_workspace_output_path(project_root, args.output)
        ensure_target_under_root(target_root, target)
        origin_url = raw_work.get("id") if isinstance(raw_work.get("id"), str) else None
        work_id = openalex_work_id_from_work(raw_work)
        if origin_url is None and work_id is not None:
            origin_url = f"https://openalex.org/{work_id}"
        if origin_url is None:
            origin_url = f"https://openalex.org/{identifier.removeprefix('doi:')}"
        provider_license_slug, spdx_license = openalex_work_license_details(raw_work)
        license_value = spdx_license or ("unresolved" if provider_license_slug else None)
        license_note = None
        if provider_license_slug and spdx_license is None:
            license_note = (
                f"OpenAlex reported raw license term {provider_license_slug!r}; "
                "no safe SPDX mapping is available."
            )
        elif provider_license_slug is None:
            license_note = "License not surfaced by OpenAlex location metadata."
        rendered_work = json.dumps(work, indent=2, sort_keys=True) + "\n"
        with acquisition_artifact_transaction(target, project_root=project_root, context=context):
            atomic_write_text(target, rendered_work)
            sidecar = write_provenance_sidecar(
                target,
                origin_url=origin_url,
                license_value=license_value,
                retrieved_by="fetch_sources.py/openalex",
                request_id=args.request_id,
                candidate_id=args.candidate_id,
                terms_url=origin_url if provider_license_slug and spdx_license is None else None,
                notes=license_note,
                extra={
                    "provider_license_slug": provider_license_slug,
                    "license_source": "openalex" if provider_license_slug else None,
                    **acquisition_provenance_fields(context, byte_count=len(rendered_work.encode("utf-8"))),
                },
                **openalex_academic_metadata(raw_work, metadata_only=True),
            )
        report.update(
            {
                "target_path": relative_label(project_root, target),
                "sidecar_path": relative_label(project_root, sidecar),
                "origin_url": origin_url,
                "license": license_value,
                "provider_license_slug": provider_license_slug,
            }
        )
    return report


def ensure_target_under_root(target_root: Path, target: Path) -> None:
    resolved_root = target_root.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise FetchSourcesError(
            "CONFIG_INVALID",
            f"OpenAlex --output must stay under configured target_root: {target_root}",
            remediation="Choose an output path under integrations.acquisition.target_root, such as raw/papers/name.pdf.",
        )


def openalex_pdf_target(project_root: Path, target_root: Path, args: argparse.Namespace, work_id: str) -> Path:
    if args.output:
        target = resolve_workspace_output_path(project_root, args.output)
        ensure_target_under_root(target_root, target)
        return target
    return target_root / f"openalex-{work_id}.pdf"


def run_openalex_download_pdf(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    work_id = openalex_work_id(args.work_id)
    target_root = context["target_root"]
    target = openalex_pdf_target(project_root, target_root, args, work_id)
    work = openalex_get_work_by_id(work_id)
    location = select_openalex_oa_pdf_location(work)
    if location is None:
        raise FetchSourcesError(
            "OPENALEX_PDF_UNAVAILABLE",
            f"OpenAlex work {work_id} does not expose an open-access PDF URL in locations metadata.",
            remediation="Choose another OpenAlex work or deliver the paper manually with a provenance sidecar.",
        )
    pdf_url = http_url(location.get("pdf_url"))
    if pdf_url is None:
        raise FetchSourcesError(
            "OPENALEX_PDF_UNAVAILABLE",
            f"OpenAlex work {work_id} does not expose a downloadable open-access PDF URL.",
            remediation="Choose another OpenAlex work or deliver the paper manually with a provenance sidecar.",
        )
    payload = openalex_fetch_url(pdf_url)
    origin_url = work.get("id") if isinstance(work.get("id"), str) else f"https://openalex.org/{work_id}"
    provider_license_slug, spdx_license = location_license_details(location)
    license_value = spdx_license or ("unresolved" if provider_license_slug else None)
    license_note = None
    if provider_license_slug and spdx_license is None:
        license_note = (
            f"OpenAlex reported raw license term {provider_license_slug!r}; "
            "no safe SPDX mapping is available."
        )
    elif provider_license_slug is None:
        license_note = "License not surfaced by OpenAlex location metadata."
    with acquisition_artifact_transaction(target, project_root=project_root, context=context):
        atomic_write_bytes(target, payload)
        sidecar = write_provenance_sidecar(
            target,
            origin_url=origin_url,
            downloaded_pdf_url=pdf_url,
            license_value=license_value,
            retrieved_by="fetch_sources.py/openalex",
            request_id=args.request_id,
            candidate_id=args.candidate_id,
            terms_url=(
                http_url(location.get("landing_page_url")) or origin_url
                if provider_license_slug and spdx_license is None
                else None
            ),
            notes=license_note,
            extra={
                "provider_license_slug": provider_license_slug,
                "license_source": "openalex" if provider_license_slug else None,
                **acquisition_provenance_fields(context, byte_count=len(payload)),
            },
            **openalex_academic_metadata(work, preferred_location=location),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "openalex",
        "command": "download-pdf",
        "work_id": work_id,
        "target_path": relative_label(project_root, target),
        "sidecar_path": relative_label(project_root, sidecar),
        "origin_url": origin_url,
        "downloaded_pdf_url": pdf_url,
        "license": license_value,
        "provider_license_slug": provider_license_slug,
        **openalex_api_notice(),
    }


def run_openalex_enrich(project_root: Path, _context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    records = selected_openalex_enrichment_records(load_manifest_records(project_root), args)
    results = [enrich_openalex_sidecar(project_root, record) for record in records]
    resolved_count = sum(1 for result in results if result.get("status") == "resolved")
    unresolved_count = sum(1 for result in results if result.get("status") == "unresolved")
    skipped_count = len(results) - resolved_count - unresolved_count
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "openalex",
        "command": "enrich",
        "source_ids": args.source_ids or [],
        "all_arxiv": bool(args.all_arxiv),
        "request_id": args.request_id,
        "count": len(results),
        "resolved_count": resolved_count,
        "unresolved_count": unresolved_count,
        "skipped_count": skipped_count,
        "network_io_executed": any(bool(result.get("network_io_executed")) for result in results),
        "results": results,
        **openalex_api_notice(),
    }


def run_openalex_command(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "resolve":
        return run_openalex_resolve(project_root, args)
    if args.command == "get":
        return run_openalex_get(project_root, context, args)
    if args.command == "download-pdf":
        return run_openalex_download_pdf(project_root, context, args)
    if args.command == "enrich":
        return run_openalex_enrich(project_root, context, args)
    raise FetchSourcesError(
        "NOT_IMPLEMENTED",
        f"openalex {args.command} is not implemented.",
        remediation="Choose openalex resolve, openalex get, openalex download-pdf, or openalex enrich.",
    )


def arxiv_query_url(query: str | None, id_list: list[str], max_results: int) -> str:
    params = {"start": "0", "max_results": str(max_results)}
    if query:
        params["search_query"] = query
    if id_list:
        params["id_list"] = ",".join(id_list)
    return f"{ARXIV_API_URL}?{urlencode(params)}"


def run_arxiv_search(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    query = args.query.strip() if isinstance(args.query, str) and args.query.strip() else None
    id_list = parse_arxiv_id_list(args.id_list)
    if not query and not id_list:
        raise FetchSourcesError(
            "CONFIG_INVALID",
            "arxiv search requires --query, --id-list, or both.",
            remediation="Pass --query TEXT for search, --id-list CSV for specific papers, or both.",
        )
    url = arxiv_query_url(query, id_list, args.max_results)
    results = parse_arxiv_atom(arxiv_fetch_url(url))
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "provider": "arxiv",
        "command": "search",
        "query": query,
        "id_list": id_list,
        "max_results": args.max_results,
        "count": len(results),
        "results": results,
    }
    if args.output:
        output_path = resolve_workspace_output_path(project_root, args.output)
        atomic_write_text(output_path, compact_json(report) + "\n")
        return {
            "schema_version": SCHEMA_VERSION,
            "provider": "arxiv",
            "command": "search",
            "output_path": relative_label(project_root, output_path),
            "count": len(results),
        }
    return report


def ensure_download_target_available(target: Path) -> None:
    marker = acquisition_marker_path(target)
    if marker.exists():
        try:
            quarantine_incomplete_artifact(target)
        except OSError as exc:
            raise FetchSourcesError(
                "ACQUISITION_RECOVERY_FAILED",
                f"Cannot quarantine interrupted acquisition output for {target}: {exc}",
                remediation=(
                    "Preserve the marker-backed output, restore write access, and retry. "
                    "Do not promote it into evidence manually."
                ),
            ) from exc
    sidecar = provenance_sidecar_path(target)
    if target.exists() or sidecar.exists():
        raise FetchSourcesError(
            "ACQUISITION_TARGET_EXISTS",
            f"Refusing to overwrite existing acquisition target or sidecar: {target}",
            remediation="Move or review the existing raw evidence before retrying the download.",
        )


def validate_tar_member(member: tarfile.TarInfo) -> PurePosixPath:
    if "\\" in member.name:
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            f"arXiv source archive uses a non-portable separator: {member.name!r}",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        )
    name = member.name
    if not name.strip():
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            "arXiv source archive contains an empty member name.",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        )
    try:
        encoded_name = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            f"arXiv source archive contains an unencodable member name: {member.name!r}",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        ) from exc
    if len(encoded_name) > ARXIV_MAX_MEMBER_NAME_BYTES:
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            f"arXiv source archive member name is too long: {member.name!r}",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        )
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            f"arXiv source archive member escapes extraction root: {member.name!r}",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        )
    for part in path.parts:
        device_stem = part.split(".", 1)[0].casefold()
        if (
            any(ord(character) < 32 or character in '<>:"|?*' for character in part)
            or part.endswith((" ", "."))
            or device_stem in WINDOWS_RESERVED_NAMES
        ):
            raise FetchSourcesError(
                "ACQUISITION_ARCHIVE_UNSAFE",
                f"arXiv source archive has a non-portable member: {member.name!r}",
                remediation="Reject the archive or inspect it manually outside the workspace.",
            )
        if len(part.encode("utf-8")) > ARXIV_MAX_MEMBER_PART_BYTES:
            raise FetchSourcesError(
                "ACQUISITION_ARCHIVE_UNSAFE",
                f"arXiv source archive path component is too long: {member.name!r}",
                remediation="Reject the archive or inspect it manually outside the workspace.",
            )
    if member.issym() or member.islnk():
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            f"arXiv source archive contains a link member: {member.name!r}",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        )
    if not (member.isdir() or member.isfile()):
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            f"arXiv source archive contains an unsupported member type: {member.name!r}",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        )
    return path


def ensure_inside_root(root: Path, target: Path, member_name: str) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise FetchSourcesError(
            "ACQUISITION_ARCHIVE_UNSAFE",
            f"arXiv source archive member escapes extraction root: {member_name!r}",
            remediation="Reject the archive or inspect it manually outside the workspace.",
        )


def extract_arxiv_source_archive(payload: bytes, target_dir: Path) -> None:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}-", dir=target_dir.parent))
    extracted_files = 0
    try:
        try:
            archive = tarfile.open(fileobj=BytesIO(payload), mode="r:*")
        except tarfile.TarError as exc:
            raise FetchSourcesError(
                "ACQUISITION_RESPONSE_INVALID",
                f"arXiv source response is not a readable tar archive: {exc}",
                remediation="Retry later or inspect the provider response outside the workspace.",
            ) from exc
        with archive:
            members: list[tarfile.TarInfo] = []
            uncompressed_bytes = 0
            while True:
                member = archive.next()
                if member is None:
                    break
                if len(members) >= ARXIV_MAX_MEMBERS:
                    raise FetchSourcesError(
                        "ACQUISITION_ARCHIVE_LIMIT_EXCEEDED",
                        f"arXiv source archive contains more than {ARXIV_MAX_MEMBERS} members.",
                        remediation="Reject the archive or inspect it manually outside the workspace.",
                    )
                if member.isfile():
                    uncompressed_bytes += member.size
                    if uncompressed_bytes > ARXIV_MAX_UNCOMPRESSED_BYTES:
                        raise FetchSourcesError(
                            "ACQUISITION_ARCHIVE_LIMIT_EXCEEDED",
                            "arXiv source archive exceeds the uncompressed-byte safety limit.",
                            remediation="Reject the archive or inspect it manually outside the workspace.",
                        )
                members.append(member)
            portable_members: dict[str, str] = {}
            validated_members: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            for member in members:
                member_path = validate_tar_member(member)
                identity = "/".join(unicodedata.normalize("NFC", part).casefold() for part in member_path.parts)
                previous = portable_members.get(identity)
                if previous is not None:
                    raise FetchSourcesError(
                        "ACQUISITION_ARCHIVE_UNSAFE",
                        f"arXiv source archive members collide portably: {previous!r} and {member.name!r}",
                        remediation="Reject the ambiguous archive or inspect it manually outside the workspace.",
                    )
                portable_members[identity] = member.name
                validated_members.append((member, member_path))
                ensure_inside_root(temp_dir, temp_dir / member_path, member.name)
            for member, member_path in validated_members:
                destination = temp_dir / member_path
                ensure_inside_root(temp_dir, destination, member.name)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise FetchSourcesError(
                        "ACQUISITION_RESPONSE_INVALID",
                        f"arXiv source archive member could not be read: {member.name!r}",
                        remediation="Retry later or inspect the provider response outside the workspace.",
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                extracted_files += 1
        if extracted_files < 1:
            raise FetchSourcesError(
                "ACQUISITION_RESPONSE_INVALID",
                "arXiv source archive contained no regular files.",
                remediation="Retry later or inspect the provider response outside the workspace.",
            )
        temp_dir.rename(target_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def write_download_report(
    project_root: Path,
    *,
    arxiv_id: str,
    artifact_format: str,
    target: Path,
    sidecar: Path,
    origin_url: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "arxiv",
        "command": "download",
        "id": arxiv_id,
        "format": artifact_format,
        "target_path": relative_label(project_root, target),
        "sidecar_path": relative_label(project_root, sidecar),
        "origin_url": origin_url,
    }


def run_arxiv_download(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    arxiv_id = normalize_arxiv_id(args.id)
    target_root = context["target_root"]
    target_root.mkdir(parents=True, exist_ok=True)
    origin_url = arxiv_abs_url(arxiv_id)
    metadata, metadata_warning = arxiv_metadata_for_download(arxiv_id)
    metadata_fields = arxiv_metadata_sidecar_fields(metadata)
    notes = arxiv_download_notes(metadata_warning)
    if args.artifact_format == "pdf":
        target = target_root / f"{arxiv_id}.pdf"
        payload = arxiv_fetch_url(arxiv_pdf_url(arxiv_id))
        with acquisition_artifact_transaction(target, project_root=project_root, context=context):
            atomic_write_bytes(target, payload)
            sidecar = write_provenance_sidecar(
                target,
                origin_url=origin_url,
                license_value="unresolved",
                retrieved_by="fetch_sources.py/arxiv",
                academic_provider="arxiv",
                academic_source_type="preprint",
                venue="arXiv",
                publication_year=arxiv_publication_year(arxiv_id),
                oa_status="green",
                peer_review_status="preprint",
                arxiv_id=arxiv_id,
                request_id=args.request_id,
                candidate_id=args.candidate_id,
                terms_url=origin_url,
                notes=notes,
                extra=acquisition_provenance_fields(context, byte_count=len(payload)),
                **metadata_fields,
            )
        return write_download_report(
            project_root,
            arxiv_id=arxiv_id,
            artifact_format=args.artifact_format,
            target=target,
            sidecar=sidecar,
            origin_url=origin_url,
        )

    target = target_root / f"arxiv-{arxiv_id}"
    payload = arxiv_fetch_url(arxiv_source_url(arxiv_id))
    with acquisition_artifact_transaction(target, project_root=project_root, context=context):
        extract_arxiv_source_archive(payload, target)
        sidecar = write_provenance_sidecar(
            target,
            origin_url=origin_url,
            license_value="unresolved",
            retrieved_by="fetch_sources.py/arxiv",
            academic_provider="arxiv",
            academic_source_type="preprint",
            venue="arXiv",
            publication_year=arxiv_publication_year(arxiv_id),
            oa_status="green",
            peer_review_status="preprint",
            arxiv_id=arxiv_id,
            request_id=args.request_id,
            candidate_id=args.candidate_id,
            terms_url=origin_url,
            notes=notes,
            extra=acquisition_provenance_fields(context, byte_count=len(payload)),
            **metadata_fields,
        )
    return write_download_report(
        project_root,
        arxiv_id=arxiv_id,
        artifact_format=args.artifact_format,
        target=target,
        sidecar=sidecar,
        origin_url=origin_url,
    )


def run_arxiv_command(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "search":
        return run_arxiv_search(project_root, args)
    if args.command == "download":
        return run_arxiv_download(project_root, context, args)
    raise FetchSourcesError(
        "NOT_IMPLEMENTED",
        f"arxiv {args.command} is not implemented.",
        remediation="Choose arxiv search or arxiv download.",
    )


# --- Web acquisition ---------------------------------------------------------


def validate_domain_list(value: Any, label: str, *, require_non_empty: bool = False) -> list[str]:
    if value is None:
        domains: list[str] = []
    elif isinstance(value, list):
        domains = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise SystemExit(f"research.yml {label} must be a list of non-empty domain names")
            domain = item.strip().lower()
            if "://" in domain or "/" in domain or "\\" in domain:
                raise SystemExit(f"research.yml {label} entries must be domain names, not URLs: {item}")
            domains.append(domain)
    else:
        raise SystemExit(f"research.yml {label} must be a list of domain names")
    if require_non_empty and not domains:
        raise FetchSourcesError(
            "ACQUISITION_DOMAIN_NOT_ALLOWED",
            "web get requires integrations.acquisition.web.allowed_domains to contain at least one domain.",
            remediation="Configure reviewed domains under integrations.acquisition.web.allowed_domains before fetching.",
        )
    duplicates = sorted({domain for domain in domains if domains.count(domain) > 1})
    if duplicates:
        raise SystemExit(f"research.yml {label} has duplicate domain(s): {', '.join(duplicates)}")
    return domains


def web_config(acquisition: dict[str, Any]) -> dict[str, Any]:
    value = acquisition.get("web")
    web = value if isinstance(value, dict) else {}
    allowed_domains = validate_domain_list(
        web.get("allowed_domains", []),
        "integrations.acquisition.web.allowed_domains",
        require_non_empty=True,
    )
    max_bytes = web.get("max_download_bytes", WEB_DEFAULT_MAX_DOWNLOAD_BYTES)
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise SystemExit("research.yml integrations.acquisition.web.max_download_bytes must be a positive integer")
    target_root_value = web.get("target_root", WEB_DEFAULT_TARGET_ROOT)
    target_root = validate_raw_target_path(target_root_value, "integrations.acquisition.web.target_root")
    return {
        "allowed_domains": allowed_domains,
        "max_download_bytes": max_bytes,
        "target_root": target_root,
    }


def resolve_web_target_root(project_root: Path, acquisition: dict[str, Any]) -> Path:
    return project_root / str(web_config(acquisition)["target_root"])


def web_target_filename(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "web"
    path = PurePosixPath(parsed.path or "/")
    suffix = path.suffix.lower()
    if suffix not in {".html", ".htm", ".txt", ".json", ".xml", ".md"}:
        suffix = ".html"
    elif suffix == ".htm":
        suffix = ".html"
    path_without_suffix = path.with_suffix("").as_posix().strip("/")
    base = f"{host}-{path_without_suffix or 'index'}"
    if parsed.query:
        base = f"{base}-{hashlib.sha256(parsed.query.encode('utf-8')).hexdigest()[:10]}"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base).strip("-").lower()
    return f"{slug or 'web-snapshot'}{suffix}"


def web_headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        "User-Agent": "evidence-wiki fetch_sources.py/1.0; web acquisition",
    }


def coerce_download_result(value: Any, *, url: str, max_bytes: int) -> DownloadResult:
    if isinstance(value, DownloadResult):
        enforce_payload_size(value.content, max_bytes, "web")
        return value
    raise FetchSourcesError(
        "ACQUISITION_RESPONSE_INVALID",
        "web transport must return response status, media type, URL, and verified-TLS metadata.",
        remediation="Fix the web acquisition transport adapter so policy can be checked before promotion.",
    )


def web_fetch_url(url: str, config: dict[str, Any], args: argparse.Namespace) -> DownloadResult:
    allowed_domains = list(config["allowed_domains"])
    max_bytes = int(config["max_download_bytes"])
    if getattr(args, "insecure_tls_documented", None) is not None:
        raise FetchSourcesError(
            "ACQUISITION_TLS_FAILED",
            "Automated acquisition cannot disable TLS certificate verification.",
            remediation="Use an endpoint with a valid, trusted TLS certificate chain.",
        )
    try:
        validate_https_url(
            url,
            allowed_domains=allowed_domains,
            resolve_hostnames=WEB_TRANSPORT is None,
        )
    except AcquisitionTransportError as exc:
        raise fetch_error_from_transport(exc) from exc
    if WEB_TRANSPORT is None:
        try:
            return bounded_download(
                url,
                allowed_domains=allowed_domains,
                max_bytes=max_bytes,
                timeout=WEB_TIMEOUT_SECONDS,
                headers=web_headers(),
                expected_content_types=WEB_EXPECTED_CONTENT_TYPES,
            )
        except AcquisitionTransportError as exc:
            raise fetch_error_from_transport(exc) from exc
    result = coerce_download_result(
        WEB_TRANSPORT(url, WEB_TIMEOUT_SECONDS, web_headers(), allowed_domains, max_bytes),
        url=url,
        max_bytes=max_bytes,
    )
    try:
        return validate_download_result(
            result,
            source_url=url,
            allowed_domains=allowed_domains,
            max_bytes=max_bytes,
            expected_content_types=WEB_EXPECTED_CONTENT_TYPES,
            # Injected transports are deterministic test adapters. The shipped
            # network path above always performs strict DNS resolution.
            resolve_hostnames=False,
        )
    except AcquisitionTransportError as exc:
        raise fetch_error_from_transport(exc) from exc


def optional_stripped(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def iso_date_text(value: Any, flag: str) -> str | None:
    text = optional_stripped(value)
    if text is None:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise FetchSourcesError(
            "ACQUISITION_DATE_METADATA_INVALID",
            f"{flag} must be an ISO date in YYYY-MM-DD form.",
            remediation="Verify the date on the retrieved page or authoritative metadata, then retry.",
        ) from exc
    return parsed.isoformat()


def valid_for_year_value(value: Any) -> int | None:
    text = optional_stripped(value)
    if text is None:
        return None
    if not re.fullmatch(r"\d{4}", text):
        raise FetchSourcesError(
            "ACQUISITION_DATE_METADATA_INVALID",
            "--valid-for-year must be a four-digit year.",
            remediation="Pass a year such as 2026, or omit --valid-for-year.",
        )
    return int(text)


def validate_web_validity_period(value: Any) -> str | None:
    text = optional_stripped(value)
    if text is None:
        return None
    if "/" not in text:
        raise FetchSourcesError(
            "ACQUISITION_DATE_METADATA_INVALID",
            "--validity-period must use ISO interval syntax start/end.",
            remediation="Pass a value such as 2026-01-01/2026-12-31, or omit --validity-period.",
        )
    start_text, end_text = (part.strip() for part in text.split("/", 1))
    start = iso_date_text(start_text, "--validity-period start") if start_text else None
    end = iso_date_text(end_text, "--validity-period end") if end_text else None
    if start is not None and end is not None and date.fromisoformat(end) < date.fromisoformat(start):
        raise FetchSourcesError(
            "ACQUISITION_DATE_METADATA_INVALID",
            "--validity-period must not end before it starts.",
            remediation="Verify the validity interval and retry.",
        )
    return f"{start or ''}/{end or ''}"


def web_date_metadata(args: argparse.Namespace) -> dict[str, Any]:
    publication_date = iso_date_text(args.publication_date, "--publication-date")
    effective_date = iso_date_text(args.effective_date, "--effective-date")
    validity_period = validate_web_validity_period(args.validity_period)
    valid_for_year = valid_for_year_value(args.valid_for_year)
    date_note = optional_stripped(args.date_note)
    if args.date_note is not None and date_note is None:
        raise FetchSourcesError(
            "ACQUISITION_DATE_METADATA_INVALID",
            "--date-note requires a non-empty note.",
            remediation="Document the date/currentness note or omit --date-note.",
        )
    metadata: dict[str, Any] = {}
    date_metadata: dict[str, Any] = {}
    if publication_date:
        metadata["publication_date"] = publication_date
    if effective_date:
        metadata["effective_date"] = effective_date
    if validity_period:
        metadata["validity_period"] = validity_period
    if valid_for_year is not None:
        date_metadata["valid_for_year"] = valid_for_year
    if date_note:
        metadata["date_not_available"] = date_note
        date_metadata["note"] = date_note
    if date_metadata:
        metadata["date_metadata"] = date_metadata
    return metadata


def non_empty_list(values: list[str]) -> list[str]:
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def run_web_get(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    acquisition = context["acquisition"]
    config = web_config(acquisition)
    target_root = resolve_web_target_root(project_root, acquisition)
    date_metadata = web_date_metadata(args)
    standards_metadata = (
        read_workspace_mapping(project_root, args.standards_metadata, label="--standards-metadata")
        if args.standards_metadata
        else None
    )
    result = web_fetch_url(args.url, config, args)
    target = target_root / web_target_filename(args.url)

    evidence_areas = non_empty_list(args.evidence_area or [])
    extra = {
        "url": args.url,
        "final_url": result.final_url,
        "source_type": optional_stripped(args.source_type) or "web_page",
        "publisher": optional_stripped(args.publisher),
        "jurisdiction": optional_stripped(args.jurisdiction),
        "supported_evidence_areas": evidence_areas or None,
        "byte_count": result.byte_count,
        "content_type": result.content_type,
        "http_status": result.http_status,
        "redirect_chain": result.redirect_chain,
        "tls_verified": result.tls_verified,
        "standards": standards_metadata,
        **acquisition_provenance_fields(context),
        **date_metadata,
    }
    with acquisition_artifact_transaction(target, project_root=project_root, context=context):
        atomic_write_bytes(target, result.content)
        sidecar = write_provenance_sidecar(
            target,
            origin_url=args.url,
            license_value=None,
            retrieved_by=WEB_RETRIEVED_BY,
            request_id=args.request_id,
            candidate_id=args.candidate_id,
            terms_url=optional_stripped(args.terms_url),
            notes="License not inferred by generic web acquisition.",
            extra=extra,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "web",
        "command": "get",
        "url": args.url,
        "final_url": result.final_url,
        "target_path": relative_label(project_root, target),
        "sidecar_path": relative_label(project_root, sidecar),
        "byte_count": result.byte_count,
        "content_type": result.content_type,
        "http_status": result.http_status,
        "tls_verified": result.tls_verified,
    }


def run_web_command(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "get":
        return run_web_get(project_root, context, args)
    raise FetchSourcesError(
        "NOT_IMPLEMENTED",
        f"web {args.command} is not implemented.",
        remediation="Choose web get.",
    )


# --- GitHub transport --------------------------------------------------------


def github_token() -> str | None:
    """Read GITHUB_TOKEN from the environment only. Never persisted or emitted."""
    value = os.environ.get("GITHUB_TOKEN")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def github_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": GITHUB_USER_AGENT,
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def urllib_github_transport(url: str, timeout: float, headers: dict[str, str]) -> bytes:
    expected_content_types = (
        GITHUB_ARCHIVE_CONTENT_TYPES if "/tarball/" in urlparse(url).path else GITHUB_JSON_CONTENT_TYPES
    )
    return bounded_fetch_bytes(
        url,
        max_bytes=GITHUB_DEFAULT_MAX_RESPONSE_BYTES,
        timeout=timeout,
        headers=headers,
        allowed_domains=["api.github.com", "github.com", "codeload.github.com"],
        resolve_hostnames=True,
        expected_content_types=expected_content_types,
    )


def active_github_transport():
    return GITHUB_TRANSPORT or urllib_github_transport


def github_wait_for_rate_limit() -> None:
    global GITHUB_LAST_REQUEST_AT
    now = float(GITHUB_CLOCK())
    if GITHUB_LAST_REQUEST_AT is not None:
        elapsed = now - GITHUB_LAST_REQUEST_AT
        if elapsed < GITHUB_REQUEST_INTERVAL_SECONDS:
            GITHUB_SLEEP(GITHUB_REQUEST_INTERVAL_SECONDS - elapsed)
            now = float(GITHUB_CLOCK())
    GITHUB_LAST_REQUEST_AT = now


def github_fetch_bytes(
    url: str,
    *,
    accept: str = "application/vnd.github+json",
    allow_empty: bool = False,
    not_found_code: str = "GITHUB_NOT_FOUND",
    not_found_message: str | None = None,
    not_found_remediation: str | None = None,
    max_bytes: int | None = None,
) -> bytes:
    transport = GITHUB_TRANSPORT
    byte_limit = max_bytes or GITHUB_DEFAULT_MAX_RESPONSE_BYTES
    last_error: BaseException | None = None
    for attempt in range(1, GITHUB_MAX_ATTEMPTS + 1):
        github_wait_for_rate_limit()
        try:
            headers = github_headers(accept)
            if transport is None:
                payload = bounded_fetch_bytes(
                    url,
                    max_bytes=byte_limit,
                    timeout=GITHUB_TIMEOUT_SECONDS,
                    headers=headers,
                    allowed_domains=["api.github.com", "github.com", "codeload.github.com"],
                    resolve_hostnames=True,
                    expected_content_types=(
                        GITHUB_ARCHIVE_CONTENT_TYPES
                        if "/tarball/" in urlparse(url).path
                        else GITHUB_JSON_CONTENT_TYPES
                    ),
                )
            else:
                payload = transport(url, GITHUB_TIMEOUT_SECONDS, headers)
            if not isinstance(payload, bytes):
                raise FetchSourcesError(
                    "ACQUISITION_RESPONSE_INVALID",
                    "GitHub transport returned a non-byte response.",
                    remediation="Fix the acquisition transport adapter and retry.",
                )
            if transport is not None and max_bytes is None:
                enforce_payload_size(payload, byte_limit, "GitHub")
            if not payload and not allow_empty:
                raise FetchSourcesError(
                    "ACQUISITION_RESPONSE_INVALID",
                    f"GitHub returned an empty response for {redact_url(url)}.",
                    remediation="Retry later or inspect the provider response outside the workspace.",
                )
            return payload
        except FetchSourcesError:
            raise
        except HTTPError as exc:
            last_error = exc
            status = exc.code
            close_http_error(exc)
            if status == 401:
                raise FetchSourcesError(
                    "GITHUB_AUTH_REQUIRED",
                    f"GitHub request failed with HTTP 401: {redact_url(url)}",
                    remediation=(
                        "Set a valid GITHUB_TOKEN in the process environment and rerun, "
                        "or unset an invalid token to use unauthenticated acquisition."
                    ),
                ) from None
            if status == 429 and attempt < GITHUB_MAX_ATTEMPTS:
                GITHUB_SLEEP(retry_backoff_seconds(attempt))
                continue
            if status in {403, 429}:
                raise FetchSourcesError(
                    "GITHUB_RATE_LIMITED",
                    f"GitHub rate-limited the request with HTTP {status}: {redact_url(url)}",
                    remediation=(
                        "Retry later or set GITHUB_TOKEN in the process environment for a "
                        "higher rate limit."
                    ),
                ) from None
            if status == 404:
                raise FetchSourcesError(
                    not_found_code,
                    redact_diagnostic(not_found_message)
                    if not_found_message
                    else f"GitHub returned HTTP 404 (not found): {redact_url(url)}",
                    remediation=not_found_remediation
                    or "Verify the owner/repo, ref, or tag exists and is accessible.",
                ) from None
            if attempt < GITHUB_MAX_ATTEMPTS and 500 <= status <= 599:
                GITHUB_SLEEP(retry_backoff_seconds(attempt))
                continue
            raise FetchSourcesError(
                "ACQUISITION_NETWORK_ERROR",
                f"GitHub request failed with HTTP {status}: {redact_url(url)}",
                remediation="Retry later, check network access, or lower request volume.",
            ) from None
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
            if attempt < GITHUB_MAX_ATTEMPTS:
                GITHUB_SLEEP(retry_backoff_seconds(attempt))
                continue
    raise FetchSourcesError(
        "ACQUISITION_NETWORK_ERROR",
        f"GitHub request failed after {GITHUB_MAX_ATTEMPTS} attempt(s): {redact_diagnostic(last_error)}",
        remediation="Retry later, check network access, or lower request volume.",
    )


def github_json_object(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FetchSourcesError(
            "ACQUISITION_RESPONSE_INVALID",
            f"GitHub returned invalid JSON: {exc}",
            remediation="Retry later or inspect the provider response outside the workspace.",
        ) from exc
    if not isinstance(document, dict):
        raise FetchSourcesError(
            "ACQUISITION_RESPONSE_INVALID",
            "GitHub returned JSON that was not an object.",
            remediation="Retry later or inspect the provider response outside the workspace.",
        )
    return document


# --- GitHub acquisition helpers ----------------------------------------------


def github_api_notice() -> dict[str, Any]:
    """Report only whether a token authenticated the run, never the token value."""
    return {"token_used": github_token() is not None}


def resolve_owner_repo(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve an explicit owner/repo from --repo or --url. Never from search."""
    repo_arg = getattr(args, "repo", None)
    url_arg = getattr(args, "url", None)
    provided = [value for value in (repo_arg, url_arg) if isinstance(value, str) and value.strip()]
    if len(provided) != 1:
        raise FetchSourcesError(
            "GITHUB_REPO_INVALID",
            "GitHub acquisition requires exactly one of --repo owner/repo or --url.",
            remediation="Pass --repo owner/repo or --url https://github.com/owner/repo for the selected repository.",
        )
    if isinstance(repo_arg, str) and repo_arg.strip():
        candidate = repo_arg.strip()
    else:
        parsed = urlparse(url_arg.strip())
        host = parsed.netloc.lower().removeprefix("www.")
        if host != "github.com":
            raise FetchSourcesError(
                "GITHUB_REPO_INVALID",
                f"Unsupported GitHub URL host {url_arg!r}; expected github.com.",
                remediation="Pass a github.com repository URL such as https://github.com/owner/repo.",
            )
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise FetchSourcesError(
                "GITHUB_REPO_INVALID",
                f"GitHub URL {url_arg!r} does not name an owner and repository.",
                remediation="Pass a repository URL such as https://github.com/owner/repo.",
            )
        candidate = f"{parts[0]}/{parts[1].removesuffix('.git')}"
    if not GITHUB_OWNER_REPO_RE.match(candidate):
        raise FetchSourcesError(
            "GITHUB_REPO_INVALID",
            f"Invalid GitHub repository {candidate!r}; expected owner/repo.",
            remediation="Pass owner/repo using letters, digits, '.', '_', or '-'.",
        )
    owner, repo = candidate.split("/", 1)
    return owner, repo


def validate_github_ref(value: str) -> str:
    ref = value.strip()
    if not ref or ".." in ref or not GITHUB_REF_RE.match(ref):
        raise FetchSourcesError(
            "GITHUB_REPO_INVALID",
            f"Invalid GitHub ref {value!r}; expected a branch, tag, or commit SHA.",
            remediation="Pass a ref such as main, v1.2.0, or a 40-character commit SHA.",
        )
    return ref


def github_license_key(repo: dict[str, Any]) -> str | None:
    """Return the detected SPDX license key, treating NOASSERTION as unknown."""
    license_obj = repo.get("license")
    if not isinstance(license_obj, dict):
        return None
    for key in ("spdx_id", "key"):
        value = license_obj.get(key)
        if isinstance(value, str) and value.strip() and value.strip().upper() != "NOASSERTION":
            return value.strip()
    return None


def github_repo_html_url(owner: str, repo: str, metadata: dict[str, Any]) -> str:
    value = metadata.get("html_url")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return f"https://github.com/{owner}/{repo}"


def resolve_github_target_root(project_root: Path, acquisition: dict[str, Any]) -> Path:
    github_cfg = acquisition.get("github")
    if isinstance(github_cfg, dict) and github_cfg.get("target_root") is not None:
        relative = validate_raw_target_path(
            github_cfg.get("target_root"), "integrations.acquisition.github.target_root"
        )
        return project_root / relative
    return project_root / GITHUB_DEFAULT_TARGET_ROOT


def github_max_archive_bytes(acquisition: dict[str, Any]) -> int:
    github_cfg = acquisition.get("github")
    if isinstance(github_cfg, dict) and github_cfg.get("max_archive_bytes") is not None:
        value = github_cfg.get("max_archive_bytes")
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SystemExit(
                "research.yml integrations.acquisition.github.max_archive_bytes must be a positive integer"
            )
        return value
    return GITHUB_DEFAULT_MAX_ARCHIVE_BYTES


def github_repo_api_url(owner: str, repo: str) -> str:
    return f"{GITHUB_API_URL}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"


def github_get_repo(owner: str, repo: str) -> dict[str, Any]:
    return github_json_object(
        github_fetch_bytes(
            github_repo_api_url(owner, repo),
            not_found_message=f"GitHub repository {owner}/{repo} was not found or is not accessible.",
            not_found_remediation="Verify the owner/repo and that the repository is public or your token can read it.",
        )
    )


def github_resolve_commit_sha(owner: str, repo: str, ref: str) -> str | None:
    """Resolve a ref to a commit SHA. Returns None when GitHub omits it."""
    url = f"{github_repo_api_url(owner, repo)}/commits/{quote(ref, safe='')}"
    document = github_json_object(
        github_fetch_bytes(
            url,
            not_found_code="GITHUB_NOT_FOUND",
            not_found_message=f"GitHub ref {ref!r} was not found in {owner}/{repo}.",
            not_found_remediation="Pass an existing branch, tag, or commit SHA.",
        )
    )
    sha = document.get("sha")
    return sha.strip() if isinstance(sha, str) and sha.strip() else None


def compact_github_repo(metadata: dict[str, Any]) -> dict[str, Any]:
    """Select stable repository fields for the snapshot; never include secrets."""
    snapshot: dict[str, Any] = {}
    for key in (
        "full_name",
        "html_url",
        "description",
        "default_branch",
        "size",
        "stargazers_count",
        "forks_count",
        "archived",
        "fork",
        "pushed_at",
        "updated_at",
    ):
        value = metadata.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            snapshot[key] = value
    snapshot["license"] = github_license_key(metadata)
    return snapshot


def compact_github_release(release: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in ("tag_name", "name", "html_url", "published_at", "draft", "prerelease", "tarball_url", "zipball_url"):
        value = release.get(key)
        if isinstance(value, (str, bool)) or value is None:
            snapshot[key] = value
    assets_in = release.get("assets")
    assets: list[dict[str, Any]] = []
    if isinstance(assets_in, list):
        for asset in assets_in:
            if not isinstance(asset, dict):
                continue
            compact_asset: dict[str, Any] = {}
            for key in ("name", "content_type", "size", "browser_download_url", "digest", "updated_at"):
                value = asset.get(key)
                if isinstance(value, (str, int)) or value is None:
                    compact_asset[key] = value
            assets.append(compact_asset)
    snapshot["assets"] = assets
    return snapshot


def github_snapshot_target(target_root: Path, owner: str, repo: str, suffix: str) -> Path:
    return target_root / f"github-{owner}-{repo}-{suffix}"


def github_ref_slug(ref: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-")
    return slug or "ref"


def write_github_snapshot(target: Path, snapshot: dict[str, Any]) -> None:
    atomic_write_text(target, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")


def run_github_repo_metadata(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    owner, repo = resolve_owner_repo(args)
    target_root = resolve_github_target_root(project_root, context["acquisition"])
    target = github_snapshot_target(target_root, owner, repo, "metadata.json")
    metadata = github_get_repo(owner, repo)
    snapshot = compact_github_repo(metadata)
    rendered_snapshot = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    origin_url = github_repo_html_url(owner, repo, metadata)
    license_value = github_license_key(metadata)
    default_branch = metadata.get("default_branch") if isinstance(metadata.get("default_branch"), str) else None
    with acquisition_artifact_transaction(target, project_root=project_root, context=context):
        atomic_write_text(target, rendered_snapshot)
        sidecar = write_provenance_sidecar(
            target,
            origin_url=origin_url,
            license_value=license_value,
            retrieved_by=GITHUB_RETRIEVED_BY,
            repository_owner=owner,
            repository_name=repo,
            repository_full_name=f"{owner}/{repo}",
            repository_artifact_kind="repository_metadata",
            repository_ref=default_branch,
            request_id=args.request_id,
            candidate_id=args.candidate_id,
            notes=None if license_value else "License not detected by GitHub metadata.",
            extra=acquisition_provenance_fields(context, byte_count=len(rendered_snapshot.encode("utf-8"))),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "github",
        "command": "repo-metadata",
        "repo": f"{owner}/{repo}",
        "target_path": relative_label(project_root, target),
        "sidecar_path": relative_label(project_root, sidecar),
        "origin_url": origin_url,
        "license": license_value,
        **github_api_notice(),
    }


def run_github_release_metadata(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    owner, repo = resolve_owner_repo(args)
    tag = args.tag.strip() if isinstance(args.tag, str) and args.tag.strip() else None
    target_root = resolve_github_target_root(project_root, context["acquisition"])
    suffix = f"release-{github_ref_slug(tag)}.json" if tag else "release-latest.json"
    target = github_snapshot_target(target_root, owner, repo, suffix)
    if tag:
        url = f"{github_repo_api_url(owner, repo)}/releases/tags/{quote(tag, safe='')}"
        not_found = f"GitHub release tag {tag!r} was not found in {owner}/{repo}."
    else:
        url = f"{github_repo_api_url(owner, repo)}/releases/latest"
        not_found = f"GitHub repository {owner}/{repo} has no published release."
    release = github_json_object(
        github_fetch_bytes(
            url,
            not_found_code="GITHUB_RELEASE_UNAVAILABLE",
            not_found_message=not_found,
            not_found_remediation="Choose a repository or tag with a published release, or capture a source archive instead.",
        )
    )
    snapshot = compact_github_release(release)
    rendered_snapshot = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    origin_url = snapshot.get("html_url") or github_repo_html_url(owner, repo, {})
    with acquisition_artifact_transaction(target, project_root=project_root, context=context):
        atomic_write_text(target, rendered_snapshot)
        sidecar = write_provenance_sidecar(
            target,
            origin_url=origin_url,
            license_value=None,
            retrieved_by=GITHUB_RETRIEVED_BY,
            repository_owner=owner,
            repository_name=repo,
            repository_full_name=f"{owner}/{repo}",
            repository_artifact_kind="release_metadata",
            repository_ref=snapshot.get("tag_name") if isinstance(snapshot.get("tag_name"), str) else tag,
            request_id=args.request_id,
            candidate_id=args.candidate_id,
            notes="Release asset metadata snapshot; assets are not downloaded.",
            extra=acquisition_provenance_fields(context, byte_count=len(rendered_snapshot.encode("utf-8"))),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "github",
        "command": "release-metadata",
        "repo": f"{owner}/{repo}",
        "tag": snapshot.get("tag_name") or tag,
        "target_path": relative_label(project_root, target),
        "sidecar_path": relative_label(project_root, sidecar),
        "origin_url": origin_url,
        "asset_count": len(snapshot.get("assets", [])),
        **github_api_notice(),
    }


def github_archive_too_large(measured_bytes: int, limit_bytes: int, source: str) -> FetchSourcesError:
    return FetchSourcesError(
        "GITHUB_ARCHIVE_TOO_LARGE",
        (
            f"GitHub archive {source} ({measured_bytes} bytes) exceeds the configured limit "
            f"of {limit_bytes} bytes."
        ),
        remediation=(
            "Raise integrations.acquisition.github.max_archive_bytes after review, or capture a "
            "smaller ref or specific files manually."
        ),
    )


def run_github_download_archive(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    owner, repo = resolve_owner_repo(args)
    ref = validate_github_ref(args.ref)
    acquisition = context["acquisition"]
    target_root = resolve_github_target_root(project_root, acquisition)
    max_bytes = github_max_archive_bytes(acquisition)
    target = github_snapshot_target(target_root, owner, repo, f"{github_ref_slug(ref)}.tar.gz")

    metadata = github_get_repo(owner, repo)
    origin_url = github_repo_html_url(owner, repo, metadata)
    license_value = github_license_key(metadata)
    # Pre-download guard: refuse before fetching the archive when the repository's
    # reported size already exceeds the limit. size is in KB per the GitHub API.
    size_kb = metadata.get("size")
    if isinstance(size_kb, int) and not isinstance(size_kb, bool) and size_kb * 1024 > max_bytes:
        raise github_archive_too_large(size_kb * 1024, max_bytes, "repository size metadata")

    commit_sha = github_resolve_commit_sha(owner, repo, ref)
    archive_url = f"{github_repo_api_url(owner, repo)}/tarball/{quote(ref, safe='')}"
    payload = github_fetch_bytes(
        archive_url,
        accept="application/vnd.github+json",
        not_found_message=f"GitHub ref {ref!r} was not found in {owner}/{repo}.",
        not_found_remediation="Pass an existing branch, tag, or commit SHA.",
        max_bytes=max_bytes,
    )
    # Post-download guard: enforce the hard byte ceiling on the actual archive
    # before it is written to disk.
    if len(payload) > max_bytes:
        raise github_archive_too_large(len(payload), max_bytes, "download")

    with acquisition_artifact_transaction(
        target,
        project_root=project_root,
        context=context,
        additional_github_archive_bytes=len(payload),
    ):
        atomic_write_bytes(target, payload)
        sidecar = write_provenance_sidecar(
            target,
            origin_url=origin_url,
            license_value=license_value,
            retrieved_by=GITHUB_RETRIEVED_BY,
            downloaded_archive_url=archive_url,
            repository_owner=owner,
            repository_name=repo,
            repository_full_name=f"{owner}/{repo}",
            repository_artifact_kind="source_archive",
            repository_ref=ref,
            commit_sha=commit_sha,
            request_id=args.request_id,
            candidate_id=args.candidate_id,
            notes=None if license_value else "License not detected by GitHub metadata.",
            extra=acquisition_provenance_fields(context, byte_count=len(payload)),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": "github",
        "command": "download-archive",
        "repo": f"{owner}/{repo}",
        "ref": ref,
        "commit_sha": commit_sha,
        "target_path": relative_label(project_root, target),
        "sidecar_path": relative_label(project_root, sidecar),
        "origin_url": origin_url,
        "downloaded_archive_url": archive_url,
        "license": license_value,
        "archive_bytes": len(payload),
        "max_archive_bytes": max_bytes,
        **github_api_notice(),
    }


def run_github_command(project_root: Path, context: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "repo-metadata":
        return run_github_repo_metadata(project_root, context, args)
    if args.command == "release-metadata":
        return run_github_release_metadata(project_root, context, args)
    if args.command == "download-archive":
        return run_github_download_archive(project_root, context, args)
    raise FetchSourcesError(
        "NOT_IMPLEMENTED",
        f"github {args.command} is not implemented.",
        remediation="Choose github repo-metadata, github release-metadata, or github download-archive.",
    )


def run_provider_command(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(args.project_root).expanduser().resolve()
    config = load_config(project_root)
    context = acquisition_context(
        project_root,
        config,
        args.provider,
        command_requested_count(args),
        run_id=args.run_id,
    )
    if args.provider == "arxiv":
        return run_arxiv_command(project_root, context, args)
    if args.provider == "openalex":
        return run_openalex_command(project_root, context, args)
    if args.provider == "github":
        return run_github_command(project_root, context, args)
    if args.provider == "web":
        return run_web_command(project_root, context, args)
    raise FetchSourcesError(
        "NOT_IMPLEMENTED",
        (
            f"{args.provider} {args.command} passed acquisition validation, but provider transport "
            "is not implemented."
        ),
        remediation="Choose an implemented provider command or add the missing provider adapter before retrying.",
    )


def render_text_report(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.output_format == "json")
    try:
        report = run_provider_command(args)
    except FetchSourcesError as exc:
        emit_error(
            redact_diagnostic(exc.message),
            json_mode=json_mode,
            error_code=exc.error_code,
            recoverable=exc.recoverable,
            remediation=redact_diagnostic(exc.remediation) if exc.remediation else None,
        )
        return EXIT_INVALID
    except LockUnavailableError as exc:
        emit_error(
            str(exc),
            json_mode=json_mode,
            error_code=exc.error_code,
            recoverable=True,
            remediation=getattr(exc, "remediation", None),
            details=exc.details,
        )
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    if args.output_format == "json":
        print(compact_json(report))
    else:
        sys.stdout.write(render_text_report(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
