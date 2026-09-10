"""Whole-payload authority checks against a separately configured host trust file.

Workspace records and bundled policy material are inert claims. Only the host's
external trust file supplies keys, principal controllers, roles, and revocations.
This module verifies receipts; it never signs them or runs their named tools.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _evidence_revision import canonical_bytes, content_id, observation
from _qualified_packet import strict_json

TRUST_SCHEMA = "evidence-trust-policy/v1"
AUTH_SCHEMA = "evidence-authentication/v1"
TRUST_ENV = "EVIDENCE_WIKI_AUTHORITY_FILE"
MAX_TRUST_BYTES = 1024 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
ROLES = frozenset({"generator", "evaluator", "usage", "scrubber", "assessment", "availability", "revocation"})


class EvidenceInvalid(ValueError):
    """A bounded, content-free reason that an evidence contract cannot be used."""


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise EvidenceInvalid("timestamp_requires_explicit_timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceInvalid("invalid_timestamp") from exc
    return parsed.astimezone(timezone.utc)


def name(value: Any) -> str:
    if not isinstance(value, str) or NAME_RE.fullmatch(value) is None:
        raise EvidenceInvalid("invalid_identity")
    return value


def digest(value: Any) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise EvidenceInvalid("invalid_digest")
    return value


def exact_object(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise EvidenceInvalid("invalid_object_fields")
    return value


def bounded_list(value: Any, *, maximum: int = 256, minimum: int = 0) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise EvidenceInvalid("invalid_list_bound")
    return value


def read_host_policy(project_root: Path) -> bytes:
    """Read one no-follow, private, bounded external trust-file generation."""
    raw = os.environ.get(TRUST_ENV)
    if not raw:
        raise EvidenceInvalid("host_trust_unavailable")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise EvidenceInvalid("unsafe_host_trust_path")
    # Resolution is only a rejection check; descriptors below perform the read.
    if path.resolve().is_relative_to(project_root.resolve()):
        raise EvidenceInvalid("workspace_cannot_supply_trust")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise EvidenceInvalid("host_trust_capture_unsupported")
    descriptors: list[int] = []
    try:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        for part in path.parts[1:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(fd)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_mode & 0o077 or before.st_size > MAX_TRUST_BYTES):
            raise EvidenceInvalid("unsafe_host_trust_file")
        chunks = bytearray()
        while True:
            block = os.read(fd, min(65536, MAX_TRUST_BYTES + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
            if len(chunks) > MAX_TRUST_BYTES:
                raise EvidenceInvalid("host_trust_bound_exceeded")
        after = os.fstat(fd)
        entry = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if observation(before) != observation(after) or observation(after) != observation(entry):
            raise EvidenceInvalid("host_trust_changed")
        return bytes(chunks)
    except OSError as exc:
        raise EvidenceInvalid("host_trust_unreadable") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def load_trust(project_root: Path, config: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Qualify current host authority once for a complete evaluation operation."""
    selection = exact_object(config.get("evidence_trust"), {"policy_id", "policy_revision"})
    name(selection["policy_id"])
    name(selection["policy_revision"])
    raw = read_host_policy(project_root)
    try:
        policy = exact_object(strict_json(raw), {
            "schema_version", "policy_id", "policy_revision", "not_before", "expires_at",
            "principals", "revoked_keys", "revoked_envelopes",
        })
    except ValueError as exc:
        raise EvidenceInvalid("invalid_host_trust_policy") from exc
    if (policy["schema_version"] != TRUST_SCHEMA
            or any(policy[key] != selection[key] for key in selection)):
        raise EvidenceInvalid("host_trust_revision_mismatch")
    if not timestamp(policy["not_before"]) <= now < timestamp(policy["expires_at"]):
        raise EvidenceInvalid("host_trust_outside_validity")
    principals = policy["principals"]
    if not isinstance(principals, dict) or not 1 <= len(principals) <= 128:
        raise EvidenceInvalid("invalid_host_principals")
    seen_keys: set[str] = set()
    secret_controllers: dict[str, str] = {}
    for principal, settings in principals.items():
        name(principal)
        exact_object(settings, {"controller", "roles", "keys"})
        name(settings["controller"])
        roles = bounded_list(settings["roles"], maximum=len(ROLES), minimum=1)
        if any(not isinstance(role, str) or role not in ROLES for role in roles) or len(set(roles)) != len(roles):
            raise EvidenceInvalid("invalid_host_role")
        keys = settings["keys"]
        if not isinstance(keys, dict) or not 1 <= len(keys) <= 32:
            raise EvidenceInvalid("invalid_host_keys")
        for key_id, secret in keys.items():
            name(key_id)
            if key_id in seen_keys or not isinstance(secret, str) or not re.fullmatch(r"[0-9a-f]{64,128}", secret) or len(secret) % 2:
                raise EvidenceInvalid("invalid_host_key")
            seen_keys.add(key_id)
            if secret in secret_controllers and secret_controllers[secret] != settings["controller"]:
                raise EvidenceInvalid("shared_credential_cannot_establish_independence")
            secret_controllers[secret] = settings["controller"]
    for key_id in bounded_list(policy["revoked_keys"], maximum=4096):
        name(key_id)
    for identifier in bounded_list(policy["revoked_envelopes"], maximum=4096):
        digest(identifier)
    return {"policy": policy, "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest()}


def authority_basis(trust: dict[str, Any]) -> dict[str, str]:
    """Expose policy identity without returning credentials or key material."""
    policy = trust["policy"]
    return {"policy_id": policy["policy_id"], "policy_revision": policy["policy_revision"],
            "content_hash": trust["content_hash"], "expires_at": policy["expires_at"]}


def verify_attestation(
    envelope: Any, role: str, trust: dict[str, Any] | None, now: datetime,
) -> dict[str, Any]:
    """Authenticate all payload and issuer metadata, preserving distinct failure reasons."""
    result: dict[str, Any] = {"authenticated": False, "reason": "authentication_missing"}
    try:
        exact_object(envelope, {"payload"}, {"authentication"})
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise EvidenceInvalid("invalid_authenticated_payload")
        result["payload_id"] = content_id("evidence-authenticated-payload/v1", payload)
        auth = envelope.get("authentication")
        if auth is None:
            return result
        exact_object(auth, {"schema_version", "scheme", "principal", "key_id", "role", "policy_id",
                            "policy_revision", "issued_at", "expires_at", "signature"})
        if auth["schema_version"] != AUTH_SCHEMA or auth["scheme"] != "hmac-sha256" or auth["role"] != role:
            raise EvidenceInvalid("authentication_contract_unsupported")
        for field in ("principal", "key_id", "policy_id", "policy_revision"):
            name(auth[field])
        signature = auth["signature"]
        if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise EvidenceInvalid("authentication_signature_invalid")
        issued = timestamp(auth["issued_at"])
        expiry = timestamp(auth["expires_at"])
        if not issued <= now < expiry:
            raise EvidenceInvalid("authentication_outside_validity")
        if trust is None:
            raise EvidenceInvalid("host_trust_unavailable")
        policy = trust["policy"]
        if any(auth[key] != policy[key] for key in ("policy_id", "policy_revision")):
            raise EvidenceInvalid("authentication_policy_mismatch")
        if not timestamp(policy["not_before"]) <= issued < expiry <= timestamp(policy["expires_at"]):
            raise EvidenceInvalid("authentication_exceeds_policy_validity")
        settings = policy["principals"].get(auth["principal"])
        if not settings or role not in settings["roles"] or auth["key_id"] not in settings["keys"]:
            raise EvidenceInvalid("authentication_principal_not_authorized")
        if auth["key_id"] in policy["revoked_keys"] or result["payload_id"] in policy["revoked_envelopes"]:
            raise EvidenceInvalid("authentication_revoked")
        metadata = {key: value for key, value in auth.items() if key != "signature"}
        message = b"evidence-attestation/v1\x00" + canonical_bytes({"payload": payload, "authentication": metadata})
        expected = hmac.new(bytes.fromhex(settings["keys"][auth["key_id"]]), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise EvidenceInvalid("authentication_signature_mismatch")
        result.update(authenticated=True, reason="authenticated_host_receipt", principal=auth["principal"],
                      controller=settings["controller"], role=role, issued_at=auth["issued_at"], expires_at=auth["expires_at"],
                      authority=authority_basis(trust))
    except (EvidenceInvalid, TypeError, ValueError, KeyError, AttributeError) as exc:
        result["reason"] = str(exc) if isinstance(exc, EvidenceInvalid) else "invalid_authentication"
    return result
