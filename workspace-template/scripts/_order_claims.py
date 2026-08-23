#!/usr/bin/env python3
"""What an acquirer claims it did inside a pending order, before anything is committed.

Under delegated acquisition the acquirer runs ``source_requests.py fulfill`` and
``question_resolve.py reopen`` while a work order is pending. Writing the request store and
the question page there records bookkeeping the controller has not verified yet, so a
submission it later refuses leaves a fulfilment standing and a question already reopened.
This module holds the alternative: the acquirer files a **claim**, and the controller
commits it on acceptance or leaves it uncommitted.

A claim asserts only bookkeeping -- "request R is fulfilled by source S", "question Q
reopens with sources S..." -- and never the evidence. Every evidence check still runs
against the manifest, the normalized tree and the raw tree exactly as before.

Claims live **outside** the orchestration session tree::

    runs/order-claims/<orchestration_id>/<action_id>.json

which is deliberate and is the one place this differs from where the lifecycle argument
alone would put it. ``runs/orchestrations/<orchestration_id>/`` is covered by the managed
host's semantic tripwire: it snapshots that tree before dispatching a worker and verifies
it unchanged afterwards, excluding only the host-owned runtime subtrees. A claim is written
by the acquirer *during* exactly that window, so a claim stored under the session directory
would be read as control tampering by a host that is working correctly. The repair guard at
``runs/orchestration-guards/<orchestration_id>.json`` was moved out of that tree for the
neighbouring reason and is the precedent followed here.

Putting claims *inside* the work order was rejected outright: the order is a fingerprinted,
controller-authored artifact the acquirer must not write.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _workspace_locks import workspace_lock  # noqa: E402

ORDER_CLAIMS_DIR = "runs/order-claims"
LOCKS_DIR = ".locks"
SCHEMA_VERSION = 1
MAX_CLAIMS_BYTES = 8 * 1024 * 1024
CLAIM_LOCK_TIMEOUT_SECONDS = 10.0

# The orchestration controller's own rule for an identifier it will build a path from,
# restated here rather than imported: this module is loaded by workspace scripts that must
# not pull in the controller, exactly as ``_delegation_gate`` restates the layout constants.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")


class OrderClaimError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def require_safe_id(value: Any, label: str) -> str:
    """Reject any identifier this module would otherwise interpolate into a path."""
    normalized = str(value).strip() if value is not None else ""
    if not normalized or ".." in normalized or not SAFE_ID_RE.fullmatch(normalized):
        raise OrderClaimError(f"{label} is not a safe identifier: {normalized!r}")
    return normalized


def claims_dir(project_root: Path, orchestration_id: str) -> Path:
    return project_root / ORDER_CLAIMS_DIR / require_safe_id(orchestration_id, "orchestration_id")


def claims_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return claims_dir(project_root, orchestration_id) / f"{require_safe_id(action_id, 'action_id')}.json"


def claims_lock_path(project_root: Path, orchestration_id: str, action_id: str) -> Path:
    return (
        claims_dir(project_root, orchestration_id)
        / LOCKS_DIR
        / f"{require_safe_id(action_id, 'action_id')}.lock"
    )


def empty_claims(orchestration_id: str = "", action_id: str = "") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "orchestration_id": orchestration_id,
        "action_id": action_id,
        "fulfilments": {},
        "reopens": {},
    }


def _read_bounded_regular_text(path: Path) -> str | None:
    """Read the ledger as a bounded, singly linked regular file, following no link.

    The acquirer writes this file and the controller believes it, so it is read the way
    the raw-tree snapshot reads evidence rather than with ``read_text``: one ``lstat``, a
    real regular file, a link count of one, a size bound, and ``O_NOFOLLOW`` on the open.
    A symlink here would otherwise let a worker aim the controller's read somewhere else,
    and a dangling one would read as "nothing was claimed".
    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OrderClaimError(f"claims document could not be inspected: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1) or 1) != 1:
        raise OrderClaimError(f"claims document is not a singly linked regular file: {path.name}")
    if metadata.st_size > MAX_CLAIMS_BYTES:
        raise OrderClaimError(f"claims document is too large to read safely: {path.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError("claims document changed while it was opened")
            with os.fdopen(descriptor, "rb") as handle:
                payload = handle.read(MAX_CLAIMS_BYTES + 1)
        except BaseException:
            os.close(descriptor)
            raise
    except OSError as exc:
        raise OrderClaimError(f"claims document could not be read: {exc}") from exc
    if len(payload) > MAX_CLAIMS_BYTES:
        raise OrderClaimError(f"claims document is too large to read safely: {path.name}")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OrderClaimError(f"claims document is not valid UTF-8: {exc}") from exc


def load_claims(path: Path) -> dict[str, Any]:
    """Read one action's claims. A missing file is no claims; anything else is an error.

    Nothing here degrades to "no claims" on damage. A ledger the controller cannot read
    is a ledger whose contents it cannot commit, and answering with an empty document
    would let a submission be accepted having committed nothing -- the fail-open this
    whole mechanism exists to remove.
    """
    text = _read_bounded_regular_text(path)
    if text is None:
        return empty_claims()
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OrderClaimError(f"claims document could not be parsed: {exc}") from exc
    if not isinstance(document, dict):
        raise OrderClaimError(f"claims document is not a JSON object: {path.name}")
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise OrderClaimError(
            f"claims document declares schema_version {version!r}, not {SCHEMA_VERSION}: {path.name}"
        )
    for section in ("fulfilments", "reopens"):
        value = document.get(section)
        if value is None:
            document[section] = {}
            continue
        if not isinstance(value, dict):
            raise OrderClaimError(f"claims document has a non-object {section} section: {path.name}")
        for key, claim in value.items():
            _require_claim_shape(section, key, claim, path)
    return document


#: What each section's entries must carry, as (string fields, list-of-string fields).
_CLAIM_SHAPE = {
    "fulfilments": (("request_id", "source_id", "claimed_at"), ()),
    "reopens": (("question_slug", "claimed_at"), ("source_ids", "request_ids")),
}


def _require_claim_shape(section: str, key: Any, claim: Any, path: Path) -> None:
    """Refuse a claim the projection could not read, rather than failing later on its shape.

    The container being a mapping is not enough. A ``source_ids`` of ``null`` satisfies the
    section check and then makes the controller's projection iterate ``None``, which raises
    a bare ``TypeError`` out of verification instead of the fail-closed refusal every other
    unreadable-ledger path produces.
    """
    if not isinstance(key, str) or not key:
        raise OrderClaimError(f"claims document has a non-string {section} key: {path.name}")
    if not isinstance(claim, dict):
        raise OrderClaimError(f"claims document has a non-object {section} entry {key!r}: {path.name}")
    text_fields, list_fields = _CLAIM_SHAPE[section]
    for field in text_fields:
        if not isinstance(claim.get(field), str) or not claim[field]:
            raise OrderClaimError(
                f"claims document {section} entry {key!r} has no {field} string: {path.name}"
            )
    for field in list_fields:
        values = claim.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise OrderClaimError(
                f"claims document {section} entry {key!r} has a non-string-list {field}: {path.name}"
            )


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False
    )
    try:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
        raise
    handle.close()
    os.replace(handle.name, path)


def _record_claim(
    project_root: Path,
    orchestration_id: str,
    action_id: str,
    *,
    section: str,
    key: str,
    claim: dict[str, Any],
) -> dict[str, Any]:
    """Merge one claim into an action's document under its own lock.

    Locked because the two filing sites hold *different* locks -- ``fulfill`` serializes on
    the source-request store and ``reopen`` on the question page -- so neither excludes the
    other from this file, and an unlocked read-modify-write would drop one of two claims
    filed at once.
    """
    path = claims_path(project_root, orchestration_id, action_id)
    lock_path = claims_lock_path(project_root, orchestration_id, action_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with workspace_lock(
        lock_path,
        timeout_seconds=CLAIM_LOCK_TIMEOUT_SECONDS,
        purpose="order claim filing",
    ):
        document = load_claims(path)
        document["schema_version"] = SCHEMA_VERSION
        document["orchestration_id"] = str(orchestration_id)
        document["action_id"] = str(action_id)
        document[section][key] = claim
        _write_atomic(path, document)
    return claim


def record_fulfilment_claim(
    project_root: Path,
    orchestration_id: str,
    action_id: str,
    *,
    request_id: str,
    source_id: str,
    claimed_at: str,
) -> dict[str, Any]:
    claim = {"request_id": request_id, "source_id": source_id, "claimed_at": claimed_at}
    return _record_claim(
        project_root,
        orchestration_id,
        action_id,
        section="fulfilments",
        key=request_id,
        claim=claim,
    )


def record_reopen_claim(
    project_root: Path,
    orchestration_id: str,
    action_id: str,
    *,
    question_slug: str,
    source_ids: list[str],
    request_ids: list[str],
    claimed_at: str,
) -> dict[str, Any]:
    claim = {
        "question_slug": question_slug,
        "source_ids": list(source_ids),
        "request_ids": list(request_ids),
        "claimed_at": claimed_at,
    }
    return _record_claim(
        project_root,
        orchestration_id,
        action_id,
        section="reopens",
        key=question_slug,
        claim=claim,
    )


def _section(claims: dict[str, Any], name: str) -> dict[str, Any]:
    # Tolerant of a raw parsed document, because callers hand these accessors one.
    value = claims.get(name) if isinstance(claims, dict) else None
    return value if isinstance(value, dict) else {}


def fulfilment_claim(claims: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    claim = _section(claims, "fulfilments").get(request_id)
    return claim if isinstance(claim, dict) else None


def reopen_claim(claims: dict[str, Any], question_slug: str) -> dict[str, Any] | None:
    claim = _section(claims, "reopens").get(question_slug)
    return claim if isinstance(claim, dict) else None
