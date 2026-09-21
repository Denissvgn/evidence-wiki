"""Bounded JSON decoding for the package's fixed onboarding schemas.

This module validates data shape. It neither accepts arbitrary schemas nor
applies plans, checks live authority, or replaces a workspace owner's rules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from .errors import UsageError
from .onboarding_schemas import LIMITS, schema_document


def _refuse(code: str, field: str = "/") -> None:
    messages = {
        "INVALID": ("Onboarding data does not match its schema.", "Correct the named field using the selected schema."),
        "LIMIT": ("Onboarding data exceeds a contract limit.", "Split the input or reduce it to the published limits; never silently drop questions."),
        "VERSION_UNSUPPORTED": ("Unsupported onboarding schema version.", "Use the version required by the selected resource or a compatible installation; do not relabel incompatible data."),
    }
    message, remediation = messages[code]
    raise UsageError(f"ONBOARDING_{code}", message, recoverable=False,
                     remediation=remediation, details={"field": field}) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _refuse("INVALID")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    _refuse("INVALID")


def _bounded_nesting(text: str) -> None:
    """Reject deep containers before the interpreter-specific JSON parser."""
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > LIMITS["depth"]:
                _refuse("LIMIT")
        elif character in "]}":
            depth -= 1


def _bounded_tree(value: Any) -> None:
    pending = [(value, 1)]
    visited = 0
    string_bytes = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > LIMITS["depth"] or visited > LIMITS["nodes"]:
            _refuse("LIMIT")
        if isinstance(item, dict):
            if len(item) > LIMITS["object_properties"]:
                _refuse("LIMIT")
            if any(not isinstance(key, str) for key in item):
                _refuse("INVALID")
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif isinstance(item, list):
            if len(item) > LIMITS["array_items"]:
                _refuse("LIMIT")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > LIMITS["string_characters"]:
                _refuse("LIMIT")
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                _refuse("INVALID")
            if string_bytes > LIMITS["document_bytes"]:
                _refuse("LIMIT")
        elif type(item) is float and math.isfinite(item):
            # Opaque owned profiles can contain finite fractional settings.
            continue
        elif item is not None and type(item) not in (bool, int):
            _refuse("INVALID")


def _matches(value: Any, schema: dict[str, Any], field: str = "/") -> None:
    for condition in schema.get("allOf", []):
        _matches(value, condition, field)
    if "if" in schema:
        try:
            _matches(value, schema["if"], field)
        except UsageError:
            pass
        else:
            _matches(value, schema["then"], field)
    if "anyOf" in schema:
        limit_error = None
        for option in schema["anyOf"]:
            try:
                _matches(value, option, field)
                return
            except UsageError as exc:
                if exc.error_code == "ONBOARDING_LIMIT":
                    limit_error = exc
                continue
        if limit_error is not None:
            raise limit_error
        _refuse("INVALID", field)
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    if kind is not None and type(value) is not types[kind]:
        _refuse("INVALID", field)
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        _refuse("INVALID", field)
    if "enum" in schema and value not in schema["enum"]:
        _refuse("INVALID", field)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            _refuse("INVALID", field)
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            _refuse("INVALID", field)
        for key, child_schema in properties.items():
            if key in value:
                _matches(value[key], child_schema, field.rstrip("/") + "/" + key)
    elif isinstance(value, list):
        if len(value) > schema.get("maxItems", LIMITS["array_items"]):
            _refuse("LIMIT", field)
        if len(value) < schema.get("minItems", 0):
            _refuse("INVALID", field)
        for index, child in enumerate(value):
            _matches(child, schema.get("items", {}), field.rstrip("/") + f"/{index}")
    elif isinstance(value, str):
        if len(value) > schema.get("maxLength", LIMITS["string_characters"]):
            _refuse("LIMIT", field)
        if len(value) < schema.get("minLength", 0) or ("pattern" in schema and re.search(schema["pattern"], value) is None):
            _refuse("INVALID", field)
    elif type(value) is int:
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            _refuse("INVALID", field)


def decode_document(resource_id: str, raw: bytes) -> dict[str, Any]:
    """Decode exactly one UTF-8 JSON document under a closed structural schema.

Failures carry stable codes and schema-owned field paths, never input values,
unknown key names, JSON excerpts, environment values, or parser diagnostics.
"""
    schema = schema_document(resource_id)
    if not isinstance(raw, bytes):
        _refuse("INVALID")
    if len(raw) > LIMITS["document_bytes"]:
        _refuse("LIMIT")
    try:
        text = raw.decode("utf-8")
        _bounded_nesting(text)
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        _refuse("INVALID")
    _bounded_tree(value)
    if isinstance(value, dict) and "schema_version" in value and value["schema_version"] != schema["properties"]["schema_version"]["const"]:
        _refuse("VERSION_UNSUPPORTED", "/schema_version")
    _matches(value, schema)
    return value


def encode_document(resource_id: str, value: dict[str, Any]) -> bytes:
    """Validate and return canonical UTF-8 JSON; no newline is hashed."""
    schema_document(resource_id)
    _bounded_tree(value)
    chunks = []
    size = 0
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for chunk in encoder.iterencode(value):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > LIMITS["document_bytes"]:
                _refuse("LIMIT")
            chunks.append(encoded)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _refuse("INVALID")
    raw = b"".join(chunks)
    decode_document(resource_id, raw)
    return raw


def document_sha256(resource_id: str, value: dict[str, Any]) -> str:
    """Bind a complete validated artifact; a digest is not an authorization."""
    return hashlib.sha256(encode_document(resource_id, value)).hexdigest()
