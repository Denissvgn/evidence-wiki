"""Bounded native journal wrappers preserve child data and canonical byte identity.

These codecs validate transport bounds, never journal signatures or authority.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ._review_dependency_json import detached as detached_native
from ._review_dependency_wire import WIRE, _pack_validated_bytes, unpack
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
    data = value["data"]
    if type(data) is dict and data.get("schema_version") == WIRE:
        try:
            data = unpack(data, "journal_data")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            refuse("INVALID")
    else:
        data = detached_native(data)
    result["data"] = data
    return _aggregate(result)


def _owned_record_transport(result: dict[str, Any], *, physical: bool) -> dict[str, Any]:
    from ._review_dependency_json import encoded

    result = {**result}
    raw = _serialized(result)
    if len(raw) > 262144:
        try:
            selected = encoded(result["data"])
            if selected is not None:
                result["data"] = _pack_validated_bytes(selected, "journal_data")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            refuse("INVALID")
    if physical and len(_serialized(result)) > 262144:
        refuse("LIMIT")
    return result


def _record_transport(value: Any, *, physical: bool) -> dict[str, Any]:
    return _owned_record_transport(detached_record(value), physical=physical)


def _canonical_owned_record(value: Any) -> tuple[bytes, dict[str, Any]]:
    """Return physical bytes and a separately owned logical record for one read."""
    logical = detached_record(value)
    return _serialized(_owned_record_transport(logical, physical=False)), logical


def storage_record(value: Any) -> dict[str, Any]:
    """Select bounded physical storage without changing legacy codec capacity."""
    return _record_transport(value, physical=True)


def canonical_record(value: Any) -> bytes:
    """Encode the physical record, retaining legacy bytes below transport bounds."""
    return _serialized(_record_transport(value, physical=False))


def digest_record(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_record(value)).hexdigest()


def expanded_size(value: Any) -> int:
    """Measure logical canonical expansion under the raw journal capacity."""
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for text in encoder.iterencode(value):
        size += len(text.encode("utf-8"))
        if size > 16777216:
            refuse("LIMIT")
    return size


def detached_records(value: Any) -> list[dict[str, Any]]:
    """Detach a native collection without charging list/header depth to data."""
    if type(value) is not list or len(value) > 4096:
        refuse("LIMIT")
    result = [detached_record(record) for record in value]
    expanded_size(result)
    _aggregate([_record_transport(record, physical=False) for record in result])
    return result


def canonical_records(value: Any) -> bytes:
    raw = _serialized([_record_transport(record, physical=False) for record in detached_records(value)])
    if len(raw) > 1048576:
        refuse("LIMIT")
    return raw


def digest_records(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_records(value)).hexdigest()
