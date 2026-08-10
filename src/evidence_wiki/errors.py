"""Typed exceptions for the embeddable EvidenceWiki API.

Workspace scripts report every fatal condition as a schema-1.0 *error envelope*
carrying a stable ``error_code``. A subprocess caller reads that envelope off
stderr; an in-process caller wants an exception instead. This module defines the
exception hierarchy those envelopes map onto, so a host can catch a family
(``except CoverageError``) or dispatch on the exact code, and always has the
original code, remediation, and recoverability in hand.

The authoritative code vocabulary lives in the packaged asset
``workspace-template/scripts/_script_errors.py``. That file is an asset rather
than an importable module, so this module deliberately declares the mapping
itself and never imports the asset at import time; ``tests/test_library_errors.py``
loads the asset and proves the two agree. Codes this version has never seen --
a newer workspace, a newer domain pack -- degrade to the base class with the
code preserved rather than raising, so an older host keeps working.
"""

from __future__ import annotations

from typing import Any

#: Error-envelope schema this module reads. Matches ``_script_errors.SCHEMA_VERSION``.
SCHEMA_VERSION = "1.0"

#: ``error_code`` used when an envelope carries none at all.
UNKNOWN_ERROR_CODE = "UNKNOWN"

#: ``error_code`` used when the input is not a usable error envelope.
MALFORMED_ENVELOPE_ERROR_CODE = "ENVELOPE_MALFORMED"

#: Fallback remediation. Mirrors the fallback in ``_script_errors.remediation_for``.
DEFAULT_REMEDIATION = "Read the message, fix the input or workspace state, and rerun the command."

#: Process exit status a workspace script uses for a fatal, caller-fixable error.
EXIT_INVALID = 2

#: Process exit status a workspace script uses when another agent holds the resource.
EXIT_CONFLICT = 3

#: Process exit status the orchestration controller uses when another driver holds
#: the session lock. Distinct from ``EXIT_CONFLICT`` because a shell-only caller must
#: be able to tell "retry in a moment" from a question claim held by another agent,
#: and distinct from ``evidence_wiki.orchestration.EXIT_RUNNER_FAILED`` (5) because
#: the same ``evidence-wiki orchestrate`` command family returns that for a managed
#: run whose runner failed -- a condition that must *not* be retried.
EXIT_DRIVER_BUSY = 6

# Mirrors ``_script_errors.default_recoverable``: a held or fresh claim is not a
# thing the caller can retry its way out of, every other condition is.
_NON_RECOVERABLE_CODES = frozenset({"CLAIM_HELD", "CLAIM_NOT_STALE"})

# Codes whose emitting script exits with something other than ``EXIT_INVALID``.
#
# This table is the only place the reconstruction can learn a non-default status:
# no error envelope in this package carries an ``exit_code`` key, so a code absent
# here reports ``EXIT_INVALID`` regardless of what its script actually exited with.
# ``docs/library-api.md`` promises ``exit_code`` is "the status the CLI would have
# exited with", so every workspace-script refusal that exits with something else
# belongs here — omitting one makes the two doors disagree about the same failure.
_EXIT_CODE_OVERRIDES: dict[str, int] = {
    "CLAIM_HELD": EXIT_CONFLICT,
    "CLAIM_NOT_STALE": EXIT_CONFLICT,
    "ORCHESTRATION_DRIVER_BUSY": EXIT_DRIVER_BUSY,
}


def default_recoverable(error_code: str) -> bool:
    """Return whether retrying is meaningful for a code, absent an explicit flag."""
    return error_code not in _NON_RECOVERABLE_CODES


def default_exit_code(error_code: str) -> int:
    """Return the process exit status a workspace script uses for a code."""
    return _EXIT_CODE_OVERRIDES.get(error_code, EXIT_INVALID)


class EvidenceWikiError(Exception):
    """Base class for every failure the library API reports.

    Instances carry the full error envelope: hosts log :attr:`message` as-is,
    branch on :attr:`error_code`, surface :attr:`remediation` to an operator,
    and use :attr:`recoverable` to decide whether a retry is worth attempting.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        recoverable: bool | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code: str = error_code
        self.message: str = message
        self.recoverable: bool = default_recoverable(error_code) if recoverable is None else recoverable
        self.remediation: str = DEFAULT_REMEDIATION if remediation is None else remediation
        self.details: dict[str, Any] = dict(details) if details else {}
        self.exit_code: int = default_exit_code(error_code) if exit_code is None else exit_code

    def __str__(self) -> str:
        return self.message


class ConfigError(EvidenceWikiError):
    """The workspace, its configuration, or the runtime cannot support the call."""


class LockError(EvidenceWikiError):
    """The workspace write lock is held by another process."""


class ClaimError(EvidenceWikiError):
    """A question claim could not be taken, stolen, released, or resolved."""


class QuestionError(EvidenceWikiError):
    """A question, its slug, or its answer page is unusable."""


class CoverageError(EvidenceWikiError):
    """A coverage manifest is missing, invalid, or blocking an answer."""


class GroundingError(EvidenceWikiError):
    """A claim is not grounded in a verifiable quote from an accepted source."""


class RequestError(EvidenceWikiError):
    """A source request could not be opened, scoped, fulfilled, or delegated."""


class SourceError(EvidenceWikiError):
    """A source, its manifest entry, or its provenance record is unusable."""


class IntakeError(EvidenceWikiError):
    """Externally supplied intake was refused by a cap, a rate limit, or a signature check."""


class ProviderError(EvidenceWikiError):
    """A discovery or acquisition provider refused, failed, or returned something unusable."""


class RunError(EvidenceWikiError):
    """A run, its baseline, or one of its events is invalid for the requested transition."""


class OrchestrationError(EvidenceWikiError):
    """An orchestration's state, scope, or trusted inputs will not support the step."""


class UsageError(EvidenceWikiError):
    """The call itself is malformed: a missing or empty option, or an operation this build does not implement."""


# Registry mapping an error code to its family. A key ending in ``_`` is a code
# *prefix*; any other key is an exact code. Exact keys win over prefixes, and a
# longer prefix wins over a shorter one, so the handful of codes that share a
# prefix with a different family (``QUESTION_NOT_CLAIMED`` is a claim failure,
# ``QUESTION_REOPEN_DELEGATED`` is a request handoff) land where they belong.
ERROR_FAMILIES: dict[str, type[EvidenceWikiError]] = {
    # Workspace / runtime preconditions.
    "CONFIG_": ConfigError,
    "WORKSPACE_UNREADABLE": ConfigError,
    "DEPENDENCY_MISSING": ConfigError,
    "TOOLING_MISSING": ConfigError,
    # An upgrade that cannot write is a workspace-level filesystem problem, the
    # same family an operator is already checking when the workspace is unreadable.
    "UPGRADE_WRITE_FAILED": ConfigError,
    # Concurrency.
    "LOCK_UNAVAILABLE": LockError,
    # Claim lifecycle.
    "CLAIM_": ClaimError,
    "STEAL_": ClaimError,
    "STATUS_NOT_": ClaimError,
    "QUESTION_NOT_CLAIMED": ClaimError,
    # Questions and their answer pages.
    "QUESTION_": QuestionError,
    "SLUG_": QuestionError,
    "PAGE_INVALID": QuestionError,
    "ANSWER_": QuestionError,
    "RESOLUTION_REASON_INVALID": QuestionError,
    # Coverage manifests.
    "COVERAGE_": CoverageError,
    "FACET_SCOPE_CONFLICT": CoverageError,
    # Grounding.
    "GROUNDING_": GroundingError,
    # Source requests.
    "REQUEST_": RequestError,
    "SOURCE_REQUEST_FULFILL_DELEGATED": RequestError,
    "QUESTION_REOPEN_DELEGATED": RequestError,
    # Recorded against a request's failed attempt, so it travels with requests.
    "ATTEMPT_FAILURE_CODE_INVALID": RequestError,
    # Sources and their inventory.
    "SOURCE_": SourceError,
    "MANIFEST_": SourceError,
    "INVENTORY_": SourceError,
    # External intake.
    "INTAKE_": IntakeError,
    "HANDOFF_SIGNATURE_INVALID": IntakeError,
    # Discovery and acquisition providers.
    "PROVIDER_": ProviderError,
    "ACQUISITION_": ProviderError,
    "DISCOVERY_": ProviderError,
    "GITHUB_": ProviderError,
    "OPENALEX_": ProviderError,
    "ARXIV_": ProviderError,
    # Runs.
    "RUN_": RunError,
    "BASELINE_": RunError,
    "EVENT_": RunError,
    "FINAL_VERDICT_REQUIRED": RunError,
    # Orchestrations.
    #
    # The controller emits a second group of codes that carry no ``ORCHESTRATION_``
    # prefix. They are the protocol's own refusals -- a submitted result that does
    # not validate, a work order that does not, an action id that names nothing --
    # and a host driving the session catches them in the same place it catches
    # everything else about that session. Without these entries they fell through
    # to the base class, so ``except OrchestrationError`` around ``session.submit``
    # missed ``RESULT_INVALID``, which is the most common refusal that call has.
    "ORCHESTRATION_": OrchestrationError,
    "ACTION_ID_INVALID": OrchestrationError,
    "ACTION_NOT_PENDING": OrchestrationError,
    "ARTIFACT_PATH_INVALID": OrchestrationError,
    "CANDIDATE_STORE_INVALID": OrchestrationError,
    "CONTROL_ARTIFACT_TAMPERED": OrchestrationError,
    "RESULT_CONFLICT": OrchestrationError,
    "RESULT_INVALID": OrchestrationError,
    "RESULT_UNREADABLE": OrchestrationError,
    "WORK_ORDER_INVALID": OrchestrationError,
    # Workspace health, reported by the shared health check rather than by one
    # script: the workspace itself is unusable, which is the ConfigError story.
    "REQUIRED_DEPENDENCY_MISSING": ConfigError,
    "RESEARCH_CONFIG_INCOMPLETE": ConfigError,
    "RESEARCH_CONFIG_INVALID": ConfigError,
    "WORKSPACE_CONFIGURED_DIRECTORY_MISSING": ConfigError,
    "WORKSPACE_REQUIRED_DIRECTORY_MISSING": ConfigError,
    "WORKSPACE_REQUIRED_FILE_MISSING": ConfigError,
    "WORKSPACE_ROOT_MISSING": ConfigError,
    # The normalized-record contract (CR-2). A record that breaches it is a
    # statement about a source, not about the workspace.
    "NORMALIZED_CONTRACT_": SourceError,
    "SIDECAR_INVALID": SourceError,
    "SIDECAR_MISSING": SourceError,
    # Discovery candidates and the provider budgets discovery spends.
    "ACADEMIC_PROVIDER_": ProviderError,
    "SEARCH_PROVIDER_": ProviderError,
    "CANDIDATE_": ProviderError,
    "JURISDICTION_": ProviderError,
    # Run execution: budgets the run controller enforces, and how a runner ended.
    "BUDGET_": RunError,
    "RUNNER_": RunError,
    # Per-policy human review (CR-1), recorded against a question.
    "REVIEWER_INVALID": QuestionError,
    "REVIEW_": QuestionError,
    # Argument-level refusals, raised before any workspace state is consulted.
    # ``NOT_IMPLEMENTED`` belongs here rather than with the providers: it means
    # the operation the caller named does not exist in this build, not that a
    # provider interaction failed, so retry logic keyed on ProviderError should
    # not pick it up.
    "AGENT_ID_INVALID": UsageError,
    "QUERY_MISSING": UsageError,
    "VALUE_INVALID": UsageError,
    "NOT_IMPLEMENTED": UsageError,
}


def error_class_for(error_code: object) -> type[EvidenceWikiError]:
    """Return the exception class for a code, or the base class when unrecognized."""
    if not isinstance(error_code, str) or not error_code:
        return EvidenceWikiError
    exact = ERROR_FAMILIES.get(error_code)
    if exact is not None and not error_code.endswith("_"):
        return exact
    best_prefix = ""
    best_class: type[EvidenceWikiError] = EvidenceWikiError
    for key, family in ERROR_FAMILIES.items():
        if key.endswith("_") and len(key) > len(best_prefix) and error_code.startswith(key):
            best_prefix = key
            best_class = family
    return best_class


def _envelope_str(envelope: dict[str, Any], key: str) -> str | None:
    value = envelope.get(key)
    return value if isinstance(value, str) else None


def error_from_envelope(envelope: object) -> EvidenceWikiError:
    """Build a typed exception from a schema-1.0 error envelope.

    Never raises. The parameter is annotated ``object`` rather than ``dict``
    precisely because tolerating input that is *not* the documented shape is the
    contract: this runs on the failure path, where a second exception would bury
    the original diagnosis. Anything unrecognized degrades to the base class:

    * a code this version does not know keeps its code and gets
      :class:`EvidenceWikiError`, so a newer workspace does not break an older host;
    * an envelope with no code reports :data:`UNKNOWN_ERROR_CODE`;
    * input that is not a usable envelope reports the malformation in its message
      and keeps whatever code it did carry.
    """
    if not isinstance(envelope, dict):
        return EvidenceWikiError(
            MALFORMED_ENVELOPE_ERROR_CODE,
            f"Malformed EvidenceWiki error envelope: expected a JSON object, got {type(envelope).__name__}.",
            remediation=DEFAULT_REMEDIATION,
            details={"envelope_type": type(envelope).__name__},
        )

    raw_code = envelope.get("error_code")
    error_code = raw_code if isinstance(raw_code, str) and raw_code else UNKNOWN_ERROR_CODE

    raw_details = envelope.get("details")
    details: dict[str, Any] = dict(raw_details) if isinstance(raw_details, dict) else {}

    remediation = _envelope_str(envelope, "remediation")
    recoverable = envelope.get("recoverable")
    raw_exit_code = envelope.get("exit_code")
    # ``bool`` is an ``int`` subclass; an exit status spelled ``True`` is noise, not a status.
    exit_code = raw_exit_code if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool) else None

    message = _envelope_str(envelope, "message")
    if message is None:
        # A missing message means the envelope did not survive whatever produced
        # it. Report that plainly and stay on the base class rather than dressing
        # a broken envelope up as a specific, actionable family.
        return EvidenceWikiError(
            error_code,
            f"Malformed EvidenceWiki error envelope: no usable 'message' (error_code: {error_code}).",
            recoverable=recoverable if isinstance(recoverable, bool) else None,
            remediation=remediation,
            details=details,
            exit_code=exit_code,
        )

    return error_class_for(error_code)(
        error_code,
        message,
        recoverable=recoverable if isinstance(recoverable, bool) else None,
        remediation=remediation,
        details=details,
        exit_code=exit_code,
    )
