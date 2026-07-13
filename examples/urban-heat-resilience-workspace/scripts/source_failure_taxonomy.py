#!/usr/bin/env python3
"""Shared source-delivery failure taxonomy.

The codes are intentionally domain-neutral: official web, product, standards,
government, academic publisher, and other manual-delivery paths can all use the
same vocabulary.
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


def is_delivery_failure_code(value: Any) -> bool:
    return isinstance(value, str) and value in DELIVERY_FAILURE_CODES


def is_source_status_value(value: Any) -> bool:
    return isinstance(value, str) and value in SOURCE_STATUS_VALUES


def delivery_failure_remediation(code: str) -> str | None:
    return DELIVERY_FAILURE_REMEDIATIONS.get(code)


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
