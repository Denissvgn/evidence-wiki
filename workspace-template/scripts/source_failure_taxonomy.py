#!/usr/bin/env python3
"""Shared source-delivery and acquisition-attempt failure taxonomy.

The codes are intentionally domain-neutral: official web, product, standards,
government, academic publisher, and other manual-delivery paths can all use the
same vocabulary.

Two overlapping vocabularies live here, and the difference is whether a file exists:

``DELIVERY_FAILURE_CODES`` describes a delivery that happened but cannot be trusted as
evidence. The code travels in a ``.provenance.yml`` sidecar beside the delivered artifact,
inventory validates it against this closed set, and a valid code marks the manifest record
unusable. Every code is enumerated in ``docs/source-delivery.md`` and pinned by tests.

``ATTEMPT_FAILURE_CODES`` describes why one acquisition attempt produced nothing, and is
recorded in the request-attempt audit rather than in a sidecar. It is a superset: an
attempt that failed with a plain HTTP 500 says ``http_error`` rather than inventing a
second word for it. The three attempt-only codes are connector-level outcomes that cannot
appear in a sidecar, because there is no delivered file for a sidecar to sit beside.

Keeping the delivery set closed is deliberate. Widening it would make
``delivery_failure_code: no_result`` a valid claim about an artifact that exists, and
would quietly widen what ``unusable_evidence_reasons`` reports for every existing consumer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DELIVERY_FAILURE_CODES = (
    "tls_verification_failed",
    "http_error",
    "javascript_required",
    "official_error_page",
    "not_found",
    "content_too_sparse",
    "license_or_terms_unknown",
    "robots_or_terms_blocked",
    "manual_review_required",
)

DELIVERY_FAILURE_REMEDIATIONS = {
    "tls_verification_failed": "Retry with a trusted TLS chain or deliver a reviewer-approved snapshot with provenance.",
    "http_error": "Retry later, verify the URL, or record the upstream HTTP status in delivery_failure_detail.",
    "javascript_required": "Use an approved browser/manual capture path or request an accessible static/export version.",
    "official_error_page": "Find the canonical current page or record the outage as blocked source acquisition.",
    "not_found": "Verify whether the source moved, was superseded, or should be replaced by a newer official URL.",
    "content_too_sparse": "Acquire a fuller representation before using the source as evidence.",
    "license_or_terms_unknown": "Review source terms or license before reusing the captured content.",
    "robots_or_terms_blocked": "Do not fetch automatically; use a permitted manual review path or alternate source.",
    "manual_review_required": "Keep the source request open until a reviewer records a concrete acquisition decision.",
}

SOURCE_STATUS_VALUES = ("available", "error_page", "not_found", "unavailable")
UNUSABLE_SOURCE_STATUSES = ("error_page", "not_found", "unavailable")

# Connector-level outcomes that produce no artifact at all, so they never appear in a
# provenance sidecar. An external acquirer records them in the request-attempt audit.
ATTEMPT_ONLY_FAILURE_CODES = (
    "provider_throttled",
    "not_authorized",
    "no_result",
)

ATTEMPT_ONLY_FAILURE_REMEDIATIONS = {
    "provider_throttled": "Retry after the connector's declared rate window.",
    "not_authorized": (
        "The acquirer's credentials or egress policy refuse this source; fix authorization "
        "host-side or record the decision and replace the request."
    ),
    "no_result": (
        "The connector completed but returned nothing usable; refine the request or try another source."
    ),
}

ATTEMPT_FAILURE_CODES = DELIVERY_FAILURE_CODES + ATTEMPT_ONLY_FAILURE_CODES

ATTEMPT_FAILURE_REMEDIATIONS = {
    **DELIVERY_FAILURE_REMEDIATIONS,
    **ATTEMPT_ONLY_FAILURE_REMEDIATIONS,
}

# Codes where trying again inside the same session cannot help, because what refused is a
# standing decision rather than a transient condition: authorization, site policy, license
# uncertainty, and anything explicitly waiting on a human. A router may retire a request
# on the first of these instead of spending its remaining attempts.
NON_RETRYABLE_ATTEMPT_FAILURE_CODES = (
    "not_authorized",
    "robots_or_terms_blocked",
    "license_or_terms_unknown",
    "manual_review_required",
)

# Derived, so a newly added code is retryable by default. That is the safer direction:
# retries are already bounded by the per-request attempt budget, while defaulting to
# non-retryable would silently retire requests on a code nobody classified yet.
RETRYABLE_ATTEMPT_FAILURE_CODES = tuple(
    code for code in ATTEMPT_FAILURE_CODES if code not in NON_RETRYABLE_ATTEMPT_FAILURE_CODES
)


def is_delivery_failure_code(value: Any) -> bool:
    return isinstance(value, str) and value in DELIVERY_FAILURE_CODES


def is_source_status_value(value: Any) -> bool:
    return isinstance(value, str) and value in SOURCE_STATUS_VALUES


def delivery_failure_remediation(code: str) -> str | None:
    return DELIVERY_FAILURE_REMEDIATIONS.get(code)


def is_attempt_failure_code(value: Any) -> bool:
    """Return whether ``value`` may be recorded as an acquisition-attempt failure."""
    return isinstance(value, str) and value in ATTEMPT_FAILURE_CODES


def attempt_failure_remediation(code: str) -> str | None:
    return ATTEMPT_FAILURE_REMEDIATIONS.get(code)


def is_retryable_attempt_failure_code(value: Any) -> bool:
    """Return whether another attempt at this request could plausibly succeed.

    An unrecognized code is not retryable: a router asking this question is deciding
    whether to spend another attempt, and a code this taxonomy does not know is not
    evidence that retrying would help. Recognized-but-unclassified cannot happen —
    the retryable set is derived from the full vocabulary.
    """
    return isinstance(value, str) and value in RETRYABLE_ATTEMPT_FAILURE_CODES


def unusable_evidence_reasons(document: Mapping[str, Any] | None) -> list[str]:
    """Return stable reason codes when delivery metadata cannot satisfy evidence."""
    if not isinstance(document, Mapping):
        return []
    reasons: list[str] = []
    source_status = document.get("source_status")
    if isinstance(source_status, str) and source_status in UNUSABLE_SOURCE_STATUSES:
        reasons.append(f"source_status:{source_status}")
    failure_code = document.get("delivery_failure_code")
    if is_delivery_failure_code(failure_code):
        reasons.append(f"delivery_failure_code:{failure_code}")
    return reasons


def evidence_is_usable(document: Mapping[str, Any] | None) -> bool:
    return not unusable_evidence_reasons(document)
