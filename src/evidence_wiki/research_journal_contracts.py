"""Bounded native journal wrappers preserve child data and canonical byte identity.

These codecs validate transport bounds, never journal signatures or authority.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .mutation_contracts import _serialized, detached, refuse

JOURNAL = "research-execution-journal/v1"
_FIELDS = {"schema_version", "kind", "binding_id", "request_id", "sequence", "previous_record_ref", "data", "authentication"}


def _aggregate(value: Any) -> Any:
    """Apply the unchanged total budget after checking every child separately."""
    pending, count = [value], 0
    while pending:
        item = pending.pop()
        count += 1
        if count > 65536:
            refuse("LIMIT")
        if type(item) is dict:
            pending.extend(child for pair in item.items() for child in pair)
        elif type(item) is list:
            pending.extend(item)
    return json.loads(_serialized(value))


def detached_record(value: Any) -> dict[str, Any]:
    """Detach one closed header around data checked at its original depth 16."""
    if (type(value) is not dict or any(type(key) is not str for key in value)
            or value.keys() not in (_FIELDS, _FIELDS | {"event_ref"})
            or value.get("schema_version") != JOURNAL):
        refuse("INVALID")
    result = detached({**value, "data": None})
    result["data"] = detached(value["data"])
    return _aggregate(result)


def canonical_record(value: Any) -> bytes:
    """Encode the complete record with the existing canonical JSON spelling."""
    return _serialized(detached_record(value))


def digest_record(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_record(value)).hexdigest()


def detached_records(value: Any) -> list[dict[str, Any]]:
    """Detach a native collection without charging list/header depth to data."""
    if type(value) is not list or len(value) > 4096:
        refuse("LIMIT")
    return _aggregate([detached_record(record) for record in value])


def canonical_records(value: Any) -> bytes:
    return _serialized(detached_records(value))


def digest_records(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_records(value)).hexdigest()
