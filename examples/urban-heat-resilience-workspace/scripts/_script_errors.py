#!/usr/bin/env python3
"""Shared fatal-error envelope helpers for workspace scripts."""

from __future__ import annotations

import json
import sys
from typing import Any

SCHEMA_VERSION = "1.0"

_REMEDIATIONS = {
    "DEPENDENCY_MISSING": "Install the missing runtime dependency and rerun the command.",
    "CONFIG_MISSING": "Run from an initialized workspace or pass --project-root to one.",
    "CONFIG_INVALID": "Fix research.yml so it is valid YAML and matches the workspace contract.",
    "WORKSPACE_UNREADABLE": "Check the workspace path and required starter files, then rerun the command.",
    "UPGRADE_WRITE_FAILED": (
        "Restore write access and free space for the target workspace, preview the same command with "
        "--dry-run, then retry the upgrade."
    ),
    "MANIFEST_MISSING": "Run scripts/source_inventory.py --report to create sources/manifest.jsonl.",
    "MANIFEST_INVALID": "Fix malformed manifest JSONL or regenerate it with scripts/source_inventory.py --report.",
    "INVENTORY_CHECKSUM_REQUIRED": (
        "Add provenance sidecars with verified sha256 checksums or rerun without --require-checksum."
    ),
    "INVENTORY_CHECKSUM_MISMATCH": (
        "Replace the delivered source or provenance checksum after review, or rerun without --reject-mismatch."
    ),
    "BASELINE_MISSING": "Capture a baseline first with scripts/run_report.py baseline --output PATH.",
    "BASELINE_INVALID": (
        "Use a scripts/run_report.py baseline artifact or an unmodified question_status.py --format json document."
    ),
    "RUN_ID_REQUIRED": "Pass --run-id, or use run_controller.py start to create a new run.",
    "RUN_ID_INVALID": "Use a plain filename-safe run id such as run-2026-06-29T010203Z.",
    "RUN_EXISTS": "Choose a different --run-id or inspect the existing run under runs/<run_id>/.",
    "RUN_UNKNOWN": "List workspace runs under runs/ and choose an existing run id.",
    "RUN_STATE_INVALID": "Repair or restore runs/<run_id>/run-state.json before continuing the run.",
    "RUN_TRANSITION_INVALID": (
        "Move only to one of the run state's allowed_next_states."
    ),
    "RUN_TERMINAL": "Start a new run; terminal run states are not transitioned in place.",
    "RUN_NOT_STALE": "Wait for the run to exceed the threshold or use a larger stale-run threshold.",
    "RUN_ADOPT_THRESHOLD_REQUIRED": "Pass --if-stale-hours HOURS when adopting a run.",
    "RUN_ABANDON_THRESHOLD_REQUIRED": "Pass --if-stale-hours HOURS when abandoning a run.",
    "FINAL_VERDICT_REQUIRED": "Pass --final-verdict complete, blocked_on_sources, no_ship, or failed.",
    "EVENT_TYPE_INVALID": "Use a documented event type or a namespaced custom type such as custom.operator.note.",
    "EVENT_DATA_INVALID": "Pass --data-json as a JSON object or omit it.",
    "COVERAGE_REQUIRED": "Create or select a coverage manifest and pass only after required facets are covered.",
    "COVERAGE_BLOCKED": "Resolve blocked coverage facets with accepted sources or source requests before answering.",
    "COVERAGE_MANIFEST_INVALID": "Fix the coverage manifest YAML so it matches docs/coverage-manifest.md.",
    "COVERAGE_MANIFEST_EXISTS": "Use the existing manifest, choose another slug, or pass --force deliberately.",
    "COVERAGE_FACET_UNKNOWN": "Choose a facet_id present in the manifest.",
    "COVERAGE_POLICY_UNKNOWN": "Use one of the policy identifiers documented in docs/coverage-manifest.md.",
    "COVERAGE_TEMPLATE_INVALID": "Fix the declarative coverage template before initializing a manifest from it.",
    "QUERY_MISSING": "Provide one or more query terms.",
    "QUESTION_UNKNOWN": "Use a question slug that exists under wiki/questions/.",
    "REQUEST_UNKNOWN": "List requests with scripts/source_requests.py list --format json and choose an existing id.",
    "SOURCE_UNKNOWN": "Run scripts/source_inventory.py --report and choose a source id present in the manifest.",
    "TOOLING_MISSING": "Restore or upgrade the workspace scripts from the starter.",
    "INTAKE_TOTAL_CAP_EXCEEDED": (
        "Resolve, defer, reject, or raise run.max_open_questions_total after reviewing the workspace backlog."
    ),
    "INTAKE_RATE_LIMITED": "Retry after the intake window expires or raise run.max_intake_per_hour deliberately.",
    "INTAKE_FIELD_TOO_LONG": "Shorten question, text, summary, or context fields before retrying intake.",
    "INTAKE_BATCH_TOO_LARGE": "Submit a smaller MCP intake batch or raise run.max_mcp_intake_batch_questions deliberately.",
    "HANDOFF_SIGNATURE_INVALID": (
        "Use the configured handoff secret to sign the handoff block, or unset the secret to keep unsigned mode."
    ),
    "LOCK_UNAVAILABLE": (
        "Retry after the other writer exits, use a filesystem that supports locks, "
        "or set EVIDENCE_WIKI_SINGLE_WRITER=1 only for an operator-controlled single-writer run."
    ),
    "ACQUISITION_DISABLED": (
        "Set integrations.acquisition.enabled: true, choose allowed providers, "
        "and rerun from an explicit fetch workflow."
    ),
    "ACQUISITION_PROVIDER_DISABLED": (
        "Add the provider to integrations.acquisition.providers or choose an enabled provider."
    ),
    "DISCOVERY_DISABLED": (
        "Set integrations.discovery.enabled: true in research.yml to opt in. "
        "Discovery still performs no network I/O until a provider transport is implemented."
    ),
    "DISCOVERY_NETWORK_ERROR": "Retry later, check network access, or lower request volume.",
    "DISCOVERY_RESPONSE_INVALID": "Retry later or inspect the provider response outside the workspace.",
    "GITHUB_AUTH_REQUIRED": (
        "Set a valid GITHUB_TOKEN in the process environment and rerun, "
        "or unset an invalid token to use unauthenticated discovery."
    ),
    "GITHUB_RATE_LIMITED": (
        "Retry later, lower --max-results, or set GITHUB_TOKEN in the process environment for a higher rate limit."
    ),
    "ACQUISITION_LIMIT_EXCEEDED": (
        "Lower the requested count or raise max_downloads_per_run after reviewing provider limits."
    ),
    "ARXIV_ID_INVALID": "Pass a versioned post-2007 arXiv id such as 2601.00001v1.",
    "ACQUISITION_NETWORK_ERROR": "Retry later, check network access, or lower request volume.",
    "ACQUISITION_RESPONSE_INVALID": "Retry later or inspect the provider response outside the workspace.",
    "ACQUISITION_URL_UNSAFE": "Use an HTTPS URL with a public hostname.",
    "ACQUISITION_DOMAIN_NOT_ALLOWED": (
        "Add the reviewed domain to integrations.acquisition.web.allowed_domains or choose another URL."
    ),
    "ACQUISITION_REDIRECT_UNSAFE": "Use a source URL whose redirects stay on reviewed public HTTPS domains.",
    "ACQUISITION_REDIRECT_LIMIT": "Use the canonical final HTTPS URL or review the redirect chain manually.",
    "ACQUISITION_DNS_FAILED": "Retry after DNS is healthy or acquire the source manually after review.",
    "ACQUISITION_STATUS_UNEXPECTED": "Use a source URL that returns a successful 2xx response.",
    "ACQUISITION_MIME_UNEXPECTED": "Use an endpoint that serves the expected media type; do not retain an error page.",
    "ACQUISITION_TLS_FAILED": (
        "Use an endpoint with a valid, trusted TLS certificate chain."
    ),
    "ACQUISITION_CONTENT_TOO_LARGE": "Raise the reviewed byte cap or acquire a smaller source artifact.",
    "ACQUISITION_ARCHIVE_UNSAFE": "Reject the archive or inspect it manually outside the workspace.",
    "ACQUISITION_TARGET_EXISTS": "Move or review the existing raw evidence before retrying the download.",
    "OPENALEX_ID_INVALID": "Pass an explicit OpenAlex work id from resolve output or a DOI.",
    "OPENALEX_RESOLUTION_UNCERTAIN": (
        "Inspect candidates manually, then use openalex get --id-or-doi with an explicit OpenAlex ID or DOI."
    ),
    "OPENALEX_AUTH_REQUIRED": "Set OPENALEX_API_KEY in the process environment and rerun the command.",
    "OPENALEX_RATE_LIMITED": "Retry later, reduce request volume, or set OPENALEX_API_KEY for a larger usage budget.",
    "OPENALEX_PDF_UNAVAILABLE": (
        "Choose another OpenAlex work or deliver the paper manually with a provenance sidecar."
    ),
    "NOT_IMPLEMENTED": "Use a command whose provider transport is implemented, or add the missing adapter first.",
    "CLAIM_HELD": "Use claim --steal --if-older-than for orchestrator-mediated stale-claim recovery.",
    "CLAIM_NOT_STALE": "Wait until the claim is stale or use a larger --if-older-than threshold.",
    "STEAL_THRESHOLD_REQUIRED": "Pass --if-older-than HOURS together with --steal.",
    "STEAL_FLAG_REQUIRED": "Pass --steal when using --if-older-than.",
    "STEAL_NOT_APPLICABLE": "Remove --steal when claiming an open question.",
    "STATUS_NOT_CLAIMABLE": "Only open questions can be claimed unless stealing a stale in_progress claim.",
    "STATUS_NOT_RELEASABLE": "Only in_progress claims held by the same agent are releasable.",
    "STATUS_NOT_RESOLVABLE": "Choose an open or in-progress question; terminal statuses are not rewritten.",
    "QUESTION_NOT_CLAIMED": "Claim the question first or pass --allow-unclaimed for an explicit unclaimed resolution.",
    "ANSWER_SOURCE_REQUIRED": "Pass at least one --source-id or use --allow-uncited for an explicit uncited answer.",
    "ANSWER_PAGE_INVALID": "Pass a workspace-relative answer page under the configured wiki root.",
    "ANSWER_PAGE_MISSING": "Create the answer page under the wiki root before resolving the question as answered.",
    "GROUNDING_REQUIRED": "Add a grounding frontmatter list with claim, source_id, quote, and optional location_hint.",
    "GROUNDING_INVALID": "Fix the grounding frontmatter so each entry has non-empty claim, source_id, and quote fields.",
    "GROUNDING_QUOTE_INVALID": "Revise the grounding quote to match normalized source content, or normalize the cited source first.",
    "GROUNDING_VERIFIER_REQUIRED": "Pass --verified-by AGENT_ID when writing quote-verification metadata.",
    "REQUEST_NOT_LINKED": "Link the source request to this question slug before using it to block the question.",
    "RESOLUTION_REASON_INVALID": "Pass a non-empty reason for blocked, deferred, or rejected outcomes.",
    "VALUE_INVALID": "Pass non-empty option values.",
    "SLUG_INVALID": "Pass a non-empty question slug without path separators.",
    "SLUG_UNKNOWN": "Use a question slug that exists under wiki/questions/.",
    "PAGE_INVALID": "Fix the question page frontmatter and rerun the command.",
    "AGENT_ID_INVALID": "Pass a non-empty --agent-id value.",
}


def json_mode_requested(argv: list[str] | None, *, default_json: bool = False) -> bool:
    """Return True when argv requests JSON/JSONL machine output."""
    args = list(sys.argv[1:] if argv is None else argv)
    for index, arg in enumerate(args):
        if arg == "--dry-run":
            return True
        if arg == "--format" and index + 1 < len(args) and args[index + 1] in {"json", "jsonl"}:
            return True
        if arg in {"--format=json", "--format=jsonl"}:
            return True
    return default_json


def classify_error_code(message: str) -> str:
    text = message.strip()
    lower = text.lower()
    if "pyyaml is required" in lower:
        return "DEPENDENCY_MISSING"
    if "pypdf" in lower and "pdf text extraction requires" in lower:
        return "DEPENDENCY_MISSING"
    if "pdftotext" in lower and ("poppler" in lower or "pdf text extraction requires" in lower):
        return "DEPENDENCY_MISSING"
    if text.startswith("Missing config:") or text.startswith("Missing research.yml:"):
        return "CONFIG_MISSING"
    if text.startswith("Invalid config:") or text.startswith("Invalid research.yml:"):
        return "CONFIG_INVALID"
    if text.startswith("Missing manifest:"):
        return "MANIFEST_MISSING"
    if "Invalid JSONL" in text and "manifest" in lower:
        return "MANIFEST_INVALID"
    if "Invalid manifest record" in text:
        return "MANIFEST_INVALID"
    if "strict checksum mode" in lower and "unverified provenance checksum" in lower:
        return "INVENTORY_CHECKSUM_MISMATCH"
    if "strict checksum mode" in lower and "missing verified checksum" in lower:
        return "INVENTORY_CHECKSUM_REQUIRED"
    if text.startswith("Missing baseline file:"):
        return "BASELINE_MISSING"
    if text.startswith("Invalid baseline JSON") or text.startswith("Baseline must be"):
        return "BASELINE_INVALID"
    if text == "Provide one or more query terms.":
        return "QUERY_MISSING"
    if text.startswith("Unknown question slug:"):
        return "QUESTION_UNKNOWN"
    if text.startswith("Unknown request id:") or "already fulfilled by a different source id" in lower:
        return "REQUEST_UNKNOWN"
    if text.startswith("Unknown source id:"):
        return "SOURCE_UNKNOWN"
    if text.startswith("Missing sibling workspace script:") or text.startswith("Cannot load sibling workspace script:"):
        return "TOOLING_MISSING"
    if text.startswith("Missing packaged script:") or text.startswith("Cannot load packaged script:"):
        return "TOOLING_MISSING"
    if text.startswith("Intake total cap exceeded:"):
        return "INTAKE_TOTAL_CAP_EXCEEDED"
    if text.startswith("Intake rate limit exceeded:"):
        return "INTAKE_RATE_LIMITED"
    if text.startswith("Intake field length exceeded:"):
        return "INTAKE_FIELD_TOO_LONG"
    if text.startswith("Intake batch too large:"):
        return "INTAKE_BATCH_TOO_LARGE"
    if "handoff signature" in lower:
        return "HANDOFF_SIGNATURE_INVALID"
    if "workspace lock" in lower or "lock_unavailable" in lower:
        return "LOCK_UNAVAILABLE"
    if "grounding" in lower and "required" in lower:
        return "GROUNDING_REQUIRED"
    if "grounding" in lower and ("quote" in lower or "verify" in lower):
        return "GROUNDING_QUOTE_INVALID"
    if "grounding" in lower:
        return "GROUNDING_INVALID"
    if "Acquisition is disabled" in text:
        return "ACQUISITION_DISABLED"
    if "Discovery is disabled" in text:
        return "DISCOVERY_DISABLED"
    if "integrations.acquisition.providers" in text and "not listed" in lower:
        return "ACQUISITION_PROVIDER_DISABLED"
    if "integrations.acquisition.max_downloads_per_run" in text and "exceeds" in lower:
        return "ACQUISITION_LIMIT_EXCEEDED"
    if "not implemented" in lower:
        return "NOT_IMPLEMENTED"
    if "research.yml" in lower or "workspace-system.yml" in lower:
        return "CONFIG_INVALID"
    return "WORKSPACE_UNREADABLE"


def default_recoverable(error_code: str) -> bool:
    return error_code not in {"CLAIM_HELD", "CLAIM_NOT_STALE"}


def remediation_for(error_code: str) -> str:
    return _REMEDIATIONS.get(error_code, "Read the message, fix the input or workspace state, and rerun the command.")


def error_envelope(
    error_code: str,
    message: str,
    *,
    recoverable: bool | None = None,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "error_code": error_code,
        "message": message,
        "recoverable": default_recoverable(error_code) if recoverable is None else recoverable,
        "remediation": remediation if remediation is not None else remediation_for(error_code),
    }
    if details:
        envelope["details"] = details
    return envelope


def emit_error(
    message: str,
    *,
    json_mode: bool,
    error_code: str | None = None,
    recoverable: bool | None = None,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if json_mode:
        code = error_code or classify_error_code(message)
        print(
            json.dumps(
                error_envelope(
                    code,
                    message,
                    recoverable=recoverable,
                    remediation=remediation,
                    details=details,
                ),
                indent=2,
                sort_keys=False,
            ),
            file=sys.stderr,
        )
    else:
        print(message, file=sys.stderr)


def handle_system_exit(
    exc: SystemExit,
    *,
    json_mode: bool,
    default_exit_code: int,
    error_code: str | None = None,
    recoverable: bool | None = None,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    if not isinstance(exc.code, str):
        raise exc
    emit_error(
        exc.code,
        json_mode=json_mode,
        error_code=error_code,
        recoverable=recoverable,
        remediation=remediation,
        details=details,
    )
    return default_exit_code
