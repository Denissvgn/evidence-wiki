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
    "DOMAIN_PACK_INVALID": (
        "Fix the domain pack so it passes evidence-wiki pack validate, then rerun the command."
    ),
    "DOMAIN_PACK_UNTRACKED": (
        "Restore or align the installed pack tree and its research.yml name, version, and contract; "
        "then run evidence-wiki pack adopt, and refresh only after adoption succeeds."
    ),
    "DOMAIN_PACK_REFRESH_CONFLICT": (
        "Review the reported config: and file: targets, then rerun with a path-specific "
        "--keep-local or --accept-pack resolution for each conflict."
    ),
    "DOMAIN_PACK_STATE_INVALID": (
        "Restore domain-packs/.evidence-wiki-state.yml from a known-good backup or reinitialize "
        "the workspace before refreshing its domain pack."
    ),
    "DOMAIN_PACK_TRANSACTION_INCOMPLETE": (
        "Run a write-mode evidence-wiki pack command to recover the interrupted transaction; "
        "dry-run and doctor only report it."
    ),
    "DOMAIN_PACK_WRITE_FAILED": (
        "Restore write access and free space for the workspace, inspect any reported transaction, "
        "then retry the pack command."
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
    # run_controller.py
    "BUDGET_EXCEEDED": (
        "Record an approved override with run_controller.py override-manual-url-budget --run-id RUN_ID "
        "--agent-id AGENT --new-limit N --override-reason TEXT --approved-by WHO, then retry the transition "
        "or finish command."
    ),
    "BUDGET_OVERRIDE_INVALID": (
        "Rerun override-manual-url-budget with --new-limit greater than the current manual URL delivery "
        "limit the message reports; an override only raises that budget, never lowers it."
    ),
    "RUN_ACADEMIC_PROVIDER_ACCOUNTING_EXISTS": (
        "Preserve the retained artifact for audit, choose a fresh run id, and start a new run. "
        "Do not truncate or replace provider accounting by hand."
    ),
    "RUN_COMPLETION_NOT_READY": (
        "Resolve every returned readiness finding and rerun finish, or close the run honestly with "
        "--final-verdict no_ship when that transition is legal."
    ),
    "RUN_COMPLETION_READINESS_INVALID": (
        "Restore scripts/publication_readiness.py from the starter with evidence-wiki upgrade so it returns "
        "a document carrying a string verdict, then rerun finish."
    ),
    "RUN_COMPLETION_READINESS_UNREADABLE": (
        "Reproduce the reported failure with scripts/publication_readiness.py --format json, fix the workspace "
        "state or dependency it names, then rerun finish."
    ),
    "RUN_EVENTS_INVALID": (
        "Preserve runs/<run_id>/events.jsonl, then restore a verified copy or repair the reported line or "
        "duplicate event id and run run_controller.py recover --run-id RUN_ID --agent-id AGENT "
        "before the next mutation."
    ),
    "RUN_EVENT_ID_CONFLICT": (
        "Preserve runs/<run_id>/events.jsonl and reconcile the two records that share the reported event id "
        "by hand; a committed event is never rewritten, so no command resolves the conflict for you."
    ),
    "RUN_MUTATION_RECOVERY_REQUIRED": (
        "Inspect runs/<run_id>/run-state.json and events.jsonl, then run run_controller.py recover --run-id "
        "RUN_ID --agent-id AGENT before attempting another mutation."
    ),
    "RUN_MUTATION_WRITE_FAILED": (
        "Restore write access or free space for the run directory, retain any generated .tmp artifact, then "
        "run run_controller.py recover --run-id RUN_ID --agent-id AGENT before retrying the mutation."
    ),
    "RUN_PENDING_EVENT_INVALID": (
        "Preserve runs/<run_id>/run-state.json for audit and restore a verified copy whose _pending_event is "
        "an object with a string event_id before rerunning run_controller.py recover --run-id RUN_ID "
        "--agent-id AGENT."
    ),
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
    "REQUEST_ALREADY_FULFILLED": (
        "A fulfilled request has evidence and no failed attempt to record; open a new request "
        "if the delivered source turned out to be unusable."
    ),
    "ATTEMPT_FAILURE_CODE_INVALID": (
        "Use an acquisition-attempt failure code documented in docs/source-delivery.md."
    ),
    "REQUEST_KIND_INVALID": (
        "Use a built-in request kind, or a pack kind namespaced like pack:<pack-name>/<kind-id>."
    ),
    "REQUEST_KIND_UNDECLARED": (
        "Declare the kind under domain_pack.request_kinds in the active domain pack, or use a built-in kind."
    ),
    "REQUEST_SCOPE_INVALID": (
        "Pass scope pairs as key=value with a lowercase key, as documented in docs/source-delivery.md."
    ),
    "REQUEST_SCOPE_MISMATCH": (
        "Fulfil the request with a source whose provenance scope agrees with it, or open a request "
        "whose scope matches the delivered evidence."
    ),
    "REQUEST_SCOPE_MISSING": (
        "Stamp the delivered source's provenance sidecar with the request's scope keys, or rerun "
        "without --require-scope."
    ),
    "FACET_SCOPE_CONFLICT": (
        "Link a request whose scope facet_id matches this facet, or open a new request for it."
    ),
    "SOURCE_REQUEST_FULFILL_DELEGATED": (
        "Fulfil or record an attempt against this request while executing the delegated acquisition work "
        "order that scopes it, or finish the active session first."
    ),
    "QUESTION_REOPEN_DELEGATED": (
        "Reopen this question while executing the work order that scopes it, or finish the active session first."
    ),
    "ORCHESTRATION_STATE_UNREADABLE": (
        "Restore the orchestration control tree; unreadable session state cannot authorize a mutation."
    ),
    "SOURCE_UNKNOWN": "Run scripts/source_inventory.py --report and choose a source id present in the manifest.",
    "TOOLING_MISSING": "Restore or upgrade the workspace scripts from the starter.",
    "INTAKE_TOTAL_CAP_EXCEEDED": (
        "Resolve, defer, reject, or raise run.max_open_questions_total after reviewing the workspace backlog."
    ),
    "INTAKE_RATE_LIMITED": "Retry after the intake window expires or raise run.max_intake_per_hour deliberately.",
    "INTAKE_FIELD_TOO_LONG": (
        "Shorten question, text, summary, context, or metadata fields before retrying intake."
    ),
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
    "PROVIDER_NOT_REGISTERED": (
        "Install a distribution that registers the provider under the evidence_wiki.acquisition_providers "
        "or evidence_wiki.discovery_providers entry-point group, or remove the id from the research.yml "
        "provider list."
    ),
    "PROVIDER_REGISTRATION_INVALID": (
        "Upgrade or fix the named distribution so its provider declares allowed_domains, terms_urls, "
        "license_inference, and a supported provider_api_version, or remove the provider from research.yml "
        "until it does."
    ),
    "PROVIDER_REQUEST_INVALID": (
        "Rewrite the request document as a single JSON object the provider accepts, using the reason the "
        "provider reported, then rerun the command."
    ),
    "PROVIDER_PLAN_INVALID": (
        "Upgrade the provider so its planned requests are HTTPS, carry only declared credential "
        "placeholders in headers, and stay within the per-command request cap."
    ),
    "ACQUISITION_DOMAIN_NOT_DECLARED": (
        "Request a URL whose host the provider declares in allowed_domains, or install a provider "
        "version that declares that host."
    ),
    "ACQUISITION_PROVIDER_RATE_LIMITED": (
        "Wait for the provider's declared rate-limit window to clear, then rerun with fewer requests "
        "or raise integrations.acquisition.max_downloads_per_run after reviewing provider limits."
    ),
    "PROVIDER_ACCOUNTING_ARGUMENT_INVALID": (
        "Fix the reservation arguments: pass an existing run directory, a positive request count, "
        "and a rate-limit declaration of the documented shape."
    ),
    "PROVIDER_ACCOUNTING_LEDGER_INVALID": (
        "Preserve the run's provider-request ledger for audit and start a fresh run. A damaged ledger "
        "is never treated as an empty budget."
    ),
    "PROVIDER_ACCOUNTING_WRITE_FAILED": (
        "Restore write access and free space for the run directory, then rerun the command."
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
    "GROUNDING_REQUIRED": (
        "Add a grounding frontmatter list whose entries each carry claim, source_id, and exactly one "
        "form of evidence: quote (with optional location_hint) or anchor with pointer and expected."
    ),
    "GROUNDING_INVALID": (
        "Fix the grounding frontmatter so each entry has a non-empty claim and source_id plus exactly "
        "one form: a non-empty quote, or an anchor whose pointer and expected are both present."
    ),
    "GROUNDING_QUOTE_INVALID": "Revise the grounding quote to match normalized source content, or normalize the cited source first.",
    "GROUNDING_ANCHOR_INVALID": (
        "Point each failed anchor at a field the cited record's structured view holds, and state that "
        "field's value in expected; re-normalize the source if it carries no structured view yet."
    ),
    "GROUNDING_FILE_INVALID": (
        "Write the grounding file as YAML or JSON carrying a top-level 'grounding:' list, or a bare "
        "list of entry mappings; use 'grounding: []' to clear a question's grounding."
    ),
    "GROUNDING_VERIFIER_REQUIRED": "Pass --verified-by AGENT_ID when writing quote-verification metadata.",
    "REQUEST_NOT_LINKED": "Link the source request to this question slug before using it to block the question.",
    "RESOLUTION_REASON_INVALID": "Pass a non-empty reason for blocked, deferred, or rejected outcomes.",
    "VALUE_INVALID": "Pass non-empty option values.",
    "SLUG_INVALID": "Pass a non-empty question slug without path separators.",
    "SLUG_UNKNOWN": "Use a question slug that exists under wiki/questions/.",
    "PAGE_INVALID": "Fix the question page frontmatter and rerun the command.",
    # question_resolve.py / coverage_manifest.py
    "STATUS_NOT_REOPENABLE": "Reopen only blocked questions.",
    "SOURCE_NOT_NORMALIZED": "Inventory and normalize the delivered source before reopening.",
    "COVERAGE_CLAIM_PROBE_INVALID": (
        "Record only method_or_artifact_existence probes with claim_verdict unconfirmed, "
        "arXiv and OpenAlex results, zero exact matches, and the required limitation text."
    ),
    "REVIEWER_INVALID": "Pass a non-empty --reviewer value to approve, or --reviewed-by to review.",
    "REVIEW_POLICY_UNKNOWN": (
        "Pass --policy with one identifier from the question's human_review_policies; "
        "scripts/question_status.py --format json lists each question's human_review_pending_policies."
    ),
    "REVIEW_VERDICT_INVALID": "Pass --verdict accepted or --verdict rejected.",
    "REVIEW_ALREADY_RECORDED": (
        "Reviews are append-only, so the accepted entry stands: review one of the question's still-pending "
        "policies instead. To overturn the acceptance, record --verdict rejected for the policy named in "
        "this refusal, which returns the question to open for rework."
    ),
    "STATUS_NOT_REVIEWABLE": (
        "Review only a question in human_review; scripts/question_status.py lists those under Pending Human Review."
    ),
    "STATUS_NOT_APPROVABLE": (
        "Approve only a question in human_review; scripts/question_status.py lists those under Pending Human Review."
    ),
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
    if text.startswith("Request already fulfilled:"):
        return "REQUEST_ALREADY_FULFILLED"
    if text.startswith("Unknown attempt failure code:"):
        return "ATTEMPT_FAILURE_CODE_INVALID"
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


class ScriptRefusal(Exception):
    """A refused operation, raised by a script's ``run_<op>`` library seam.

    Every wrapped script has two callers. ``main`` renders a refusal as the JSON
    error envelope on stderr plus a process exit code; an embedding host calls
    the ``run_<op>`` seam in-process and wants the same refusal as an exception
    it can catch. This is the one refusal type both callers share, so a seam
    never prints and ``main`` never has to reinvent what a refusal means.

    ``to_envelope`` delegates to :func:`error_envelope`, so the envelope a host
    reads off this exception and the envelope the CLI writes to stderr are built
    by exactly one code path and cannot drift apart.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        exit_code: int,
        recoverable: bool | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
        text_line: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.exit_code = int(exit_code)
        self.recoverable = recoverable
        self.remediation = remediation
        self.details = details
        # Text mode is not JSON: there a refusal is one human-readable stderr line.
        # `refused (CODE): message` is the form every coded refusal in this package
        # already prints, so it is the default. A refusal recovered from a
        # ``SystemExit`` message prints the bare message instead, because that is
        # what ``handle_system_exit`` has always printed and the seam rewrite must
        # not change a single byte of it.
        self.text_line = text_line if text_line is not None else f"refused ({error_code}): {message}"

    @classmethod
    def from_system_exit(
        cls,
        exc: SystemExit,
        *,
        exit_code: int,
        error_code: str | None = None,
        recoverable: bool | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> ScriptRefusal:
        """Convert a ``SystemExit(str)`` funnel into this refusal, as ``handle_system_exit`` would.

        A ``SystemExit`` that carries no message is process control rather than a
        refusal, and is re-raised untouched — the same choice ``handle_system_exit``
        makes, kept here so a seam gets it right without repeating the check.
        """
        if not isinstance(exc.code, str):
            raise exc
        message = exc.code
        return cls(
            error_code or classify_error_code(message),
            message,
            exit_code=exit_code,
            recoverable=recoverable,
            remediation=remediation,
            details=details,
            text_line=message,
        )

    def to_envelope(self) -> dict[str, Any]:
        """Return the fatal-error envelope for this refusal."""
        return error_envelope(
            self.error_code,
            self.message,
            recoverable=self.recoverable,
            remediation=self.remediation,
            details=self.details,
        )


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


def is_refusal(exc: object) -> bool:
    """Recognize a :class:`ScriptRefusal` by shape rather than by class identity.

    ``_workspace_module_loader`` isolates every sibling stem on each load,
    ``_script_errors`` included, so each workspace script gets its *own*
    ``ScriptRefusal`` class object::

        cov = load_workspace_module(d, "coverage_manifest")
        exp = load_workspace_module(d, "export_answers")
        cov._script_errors.ScriptRefusal is exp.ScriptRefusal   # False

    A refusal raised inside one script is therefore not an ``isinstance`` of the
    ``ScriptRefusal`` another script imported, and ``except ScriptRefusal`` across
    that boundary would catch nothing while reading as though it handled the case.

    ``except ScriptRefusal`` stays correct for refusals a module raises *itself* --
    which is what every seam and every ``main`` catches, and why this is a latent
    hazard rather than a live bug. At a cross-script boundary use either the
    sibling's own attribute (``except sibling.SomeError``, which names the class
    object that actually exists on the other side) or this predicate, which does
    not depend on class identity at all.
    """
    return callable(getattr(exc, "to_envelope", None)) and isinstance(getattr(exc, "error_code", None), str)


def emit_refusal(refusal: ScriptRefusal, *, json_mode: bool) -> int:
    """Render one refusal exactly as ``main`` always rendered it and return its exit code.

    This is the whole catch arm a wrapped ``main`` needs::

        except ScriptRefusal as refusal:
            return emit_refusal(refusal, json_mode=json_mode)

    Any object :func:`is_refusal` accepts renders here too: the attributes read
    below are the shape that predicate tests for, so a refusal that crossed a
    module-isolation boundary needs no conversion to be emitted.
    """
    if json_mode:
        emit_error(
            refusal.message,
            json_mode=True,
            error_code=refusal.error_code,
            recoverable=refusal.recoverable,
            remediation=refusal.remediation,
            details=refusal.details,
        )
    else:
        print(refusal.text_line, file=sys.stderr)
    return refusal.exit_code


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
