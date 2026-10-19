"""Closed reversible transport for explicitly selected native review children."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib

WIRE = "research-review-native-bytes/v1"
_KINDS = ("blinded_material", "native_snapshot", "native_envelope", "journal_data")
_MAX = 1048576


def schema():
    """Return the inert transport shape without loading native owners."""
    properties = {"schema_version": {"const": WIRE}, "kind": {"enum": list(_KINDS)},
        "encoding": {"const": "zlib-base64"}, "raw_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "raw_size_bytes": {"type": "integer", "minimum": 1, "maximum": _MAX},
        "encoded_chunks": {"type": "array", "minItems": 1, "maxItems": 22,
            "items": {"type": "string", "minLength": 1, "maxLength": 65536}}}
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}


def _validated(value, kind):
    from ._review_dependency_json import _aggregate, _child_schema, _envelope, _native, encoded

    if kind not in _KINDS:
        raise ValueError("review_native_wire_kind_invalid")
    _aggregate(value, max_nodes=65536, count_keys=True)
    native = _native()
    if kind == "blinded_material":
        shape = native.schema_documents()["research-dependency-review-material/v1"]
        native.validate_schema(value, shape)
    elif kind == "native_snapshot":
        shape = native.schema_documents()["evidence-strict-review/v3"]
        native.validate_schema(value, _child_schema(shape, "snapshot"))
        native.validate("research-dependency-review-material/v1", value["semantic_material"])
    elif kind == "native_envelope":
        if not _envelope(value):
            raise ValueError("review_native_wire_envelope_invalid")
        from .mutation_contracts import _selective_review_arguments
        _selective_review_arguments({"envelope": value})
    elif encoded(value) is None:
        raise ValueError("review_native_wire_journal_invalid")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def pack(value, kind):
    """Encode a fully validated native child; hashes provide no authority."""
    return _pack_validated_bytes(_validated(value, kind), kind)


def _pack_validated_bytes(raw, kind):
    """Pack private canonical bytes already validated within this codec call."""
    if not 0 < len(raw) <= _MAX:
        raise ValueError("review_native_wire_bound")
    encoded = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    return {"schema_version": WIRE, "kind": kind, "encoding": "zlib-base64",
        "raw_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "raw_size_bytes": len(raw),
        "encoded_chunks": [encoded[start:start + 65536] for start in range(0, len(encoded), 65536)]}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("review_native_wire_duplicate")
        result[key] = value
    return result


def unpack(wire, expected_kind):
    """Decode bounded canonical bytes before any native authentication."""
    if (type(wire) is not dict or set(wire) != set(schema()["properties"])
            or wire.get("schema_version") != WIRE or wire.get("kind") != expected_kind
            or expected_kind not in _KINDS or wire.get("encoding") != "zlib-base64"
            or type(wire.get("raw_size_bytes")) is not int or not 0 < wire["raw_size_bytes"] <= _MAX
            or type(wire.get("raw_sha256")) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", wire["raw_sha256"]) is None
            or type(wire.get("encoded_chunks")) is not list or not 1 <= len(wire["encoded_chunks"]) <= 22):
        raise ValueError("review_native_wire_invalid")
    chunks = wire["encoded_chunks"]
    if any(type(chunk) is not str or not 0 < len(chunk) <= 65536 or len(chunk) % 4
           or index < len(chunks) - 1 and len(chunk) != 65536 for index, chunk in enumerate(chunks)):
        raise ValueError("review_native_wire_chunks_invalid")
    encoded = "".join(chunks)
    compressed = base64.b64decode(encoded, validate=True)
    if base64.b64encode(compressed).decode("ascii") != encoded:
        raise ValueError("review_native_wire_base64_invalid")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, wire["raw_size_bytes"] + 1)
    if (len(raw) != wire["raw_size_bytes"] or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or "sha256:" + hashlib.sha256(raw).hexdigest() != wire["raw_sha256"]):
        raise ValueError("review_native_wire_bytes_invalid")
    def nonfinite(value):
        raise ValueError("review_native_wire_nonfinite")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=nonfinite)
    if (_validated(value, expected_kind) != raw
            or _pack_validated_bytes(raw, expected_kind) != wire):
        raise ValueError("review_native_wire_noncanonical")
    return value


_decode = unpack


def unpack(wire, expected_kind):
    """Reject malformed compressed input through the common codec failure type."""
    try:
        return _decode(wire, expected_kind)
    except (ValueError, TypeError, UnicodeError, RecursionError, zlib.error) as error:
        raise ValueError("review_native_wire_invalid") from error
