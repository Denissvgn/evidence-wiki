#!/usr/bin/env python3
"""Sign and verify workspace handoff correlation blocks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HANDOFF_FIELDS = ("task_id", "requested_by", "chain_run_id")
ENV_SECRET_NAME = "EVIDENCE_WIKI_HANDOFF_SECRET"  # noqa: S105 - environment variable name, not a secret
SIDECAR_SECRET_NAME = ".research-handoff-secret"  # noqa: S105 - sidecar filename, not a secret
SIGNATURE_PREFIX = "hmac-sha256:"
HANDOFF_SIGNATURE_INVALID = "HANDOFF_SIGNATURE_INVALID"

STATUS_VERIFIED = "verified"
STATUS_INVALID = "invalid"
STATUS_UNSIGNED = "unsigned"
STATUS_UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class HandoffSignatureVerification:
    status: str
    error_code: str | None = None
    message: str | None = None
    details: dict[str, Any] | None = None


def handoff_secret(project_root: Path) -> str | None:
    """Return the configured handoff secret, preferring the environment."""
    env_value = os.environ.get(ENV_SECRET_NAME)
    if env_value is not None:
        stripped = env_value.strip()
        return stripped or None

    sidecar = project_root / SIDECAR_SECRET_NAME
    if not sidecar.is_file():
        return None
    stripped = sidecar.read_text(encoding="utf-8").strip()
    return stripped or None


def canonical_handoff_payload(handoff: dict[str, Any]) -> bytes:
    """Return the deterministic byte payload covered by the HMAC."""
    values = {field: handoff.get(field, "") if isinstance(handoff.get(field), str) else "" for field in HANDOFF_FIELDS}
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=False).encode("utf-8")


def sign_handoff(handoff: dict[str, Any], secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), canonical_handoff_payload(handoff), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def signature_details(status: str, *, signature_present: bool, configured: bool) -> dict[str, Any]:
    return {
        "handoff_signature_status": status,
        "handoff_signature_configured": configured,
        "handoff_signature_present": signature_present,
    }


def verify_handoff_signature(
    handoff: dict[str, Any] | None,
    signature: str | None,
    secret: str | None,
) -> HandoffSignatureVerification:
    if not handoff:
        return HandoffSignatureVerification(
            STATUS_UNCONFIGURED,
            details=signature_details(STATUS_UNCONFIGURED, signature_present=bool(signature), configured=bool(secret)),
        )
    if secret is None:
        return HandoffSignatureVerification(
            STATUS_UNCONFIGURED,
            details=signature_details(STATUS_UNCONFIGURED, signature_present=bool(signature), configured=False),
        )
    if not signature:
        return HandoffSignatureVerification(
            STATUS_UNSIGNED,
            error_code=HANDOFF_SIGNATURE_INVALID,
            message="Handoff signature verification failed.",
            details=signature_details(STATUS_UNSIGNED, signature_present=False, configured=True),
        )

    expected = sign_handoff(handoff, secret)
    status = STATUS_VERIFIED if hmac.compare_digest(signature, expected) else STATUS_INVALID
    if status == STATUS_VERIFIED:
        return HandoffSignatureVerification(
            status,
            details=signature_details(status, signature_present=True, configured=True),
        )
    return HandoffSignatureVerification(
        status,
        error_code=HANDOFF_SIGNATURE_INVALID,
        message="Handoff signature verification failed.",
        details=signature_details(status, signature_present=True, configured=True),
    )


def project_handoff_verification(project_root: Path, project: dict[str, Any]) -> HandoffSignatureVerification:
    handoff = project.get("handoff") if isinstance(project.get("handoff"), dict) else None
    signature = project.get("handoff_signature") if isinstance(project.get("handoff_signature"), str) else None
    return verify_handoff_signature(handoff, signature, handoff_secret(project_root))
