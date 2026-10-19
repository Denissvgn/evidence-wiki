"""Preserve canonical bytes while bounding recognized native review children."""

from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache

from . import mutation_contracts as legacy

_NATIVE = frozenset({"evidence-strict-policy/v2", "evidence-strict-authorship/v2", "evidence-strict-review/v3",
    "evidence-strict-result/v3", "evidence-strict-publication/v3", "evidence-strict-review-status/v2",
    "research-dependency-review-material/v1", "evidence-review-dependency-basis/v1", "evidence-review-delivery-binding/v1"})
_FAMILY = frozenset("research-" + name + "/v3" for name in ("review-delivery", "paged-review", "review-packet",
    "host-assembled-review", "complete-review-delivery", "review-sections", "blinded-review-input"))
_ATTEMPT = {"attempt_id", "nonce", "run_id", "binding_id", "profile_sha256", "config_ref", "job_sha256", "input_sha256", "job", "input"}
_PREPARED = {"result", "snapshot", "rubric", "review_schema", "registration", "dependency_basis", "semantic_material_id",
    "claim_schema_version", "claim_basis_id", "observed_workspace_basis_id", "review_producer_id"}


def _native():
    from .pack_discovery import owner
    return owner("_review_dependency_contract")


@lru_cache(maxsize=16)
def _family_schema(name):
    from .review_packet_contracts import schemas
    return schemas()[name]


def _child_schema(parent, key):
    return {"$defs": parent.get("$defs", {}), **parent["properties"][key]}


def _input(value):
    return type(value) is dict and type(value.get("packet")) is dict and value["packet"].get("schema_version") == "research-review-context/v4"


def _envelope(value):
    return (type(value) is dict and set(value) == {"payload", "authentication"}
        and type(value.get("payload")) is dict and value["payload"].get("schema_version") == "evidence-usage-command/v1"
        and value["payload"].get("action") in {"register-strict-review", "register-strict-human-review"}
        and type(value["payload"].get("body")) is dict and set(value["payload"]["body"]) == {"review"}
        and type(value["payload"]["body"]["review"]) is dict
        and value["payload"]["body"]["review"].get("schema_version") == "evidence-strict-review/v3")


def _project(value):
    """Only owner-defined paths may introduce an independently bounded child."""
    if type(value) is not dict:
        return value, False
    version = value.get("schema_version")
    if type(version) is not str:
        version = None
    if version in _NATIVE:
        native = _native()
        shape = native.schema_documents()[version]
        if version == "research-dependency-review-material/v1" and "dependency_basis_id" not in value:
            shape["required"].remove("dependency_basis_id")
        native.validate_schema(value, shape)
        return None, True
    if version in _FAMILY:
        _native().validate_schema(value, _family_schema(version))
        return None, True
    if version == "research-review-context/v4":
        from .review_wire import decode_dependency_material
        decode_dependency_material(value.get("dependency_material"))
        _native().validate_schema(value, _child_schema(_family_schema("research-blinded-review-input/v3"), "packet"))
        return None, True
    if _input(value):
        from .review_wire import decode_dependency_material
        decode_dependency_material(value["packet"].get("dependency_material"))
        _native().validate_schema(value, _family_schema("research-blinded-review-input/v3"))
        return None, True
    if _envelope(value):
        native = _native()
        native.validate("evidence-strict-review/v3", value["payload"]["body"]["review"])
        return {**value, "payload": {**value["payload"], "body": {"review": None}}}, True
    if value.get("review_schema") == "evidence-strict-review/v3":
        if set(value) != _PREPARED:
            raise ValueError("review_dependency_prepared_invalid")
        projected = dict(value)
        for key in ("result", "snapshot", "dependency_basis"):
            projected[key], found = _project(value[key])
            if not found:
                raise ValueError("review_dependency_prepared_invalid")
        return projected, True
    material = value.get("semantic_material")
    if type(material) is dict and material.get("schema_version") == "research-dependency-review-material/v1":
        shape = _native().schema_documents()["evidence-strict-review/v3"]
        _native().validate_schema(value, _child_schema(shape, "snapshot"))
        return None, True
    if (set(value) == _ATTEMPT or version == "research-agent-input/v1" and set(value) == {"schema_version", "job", "input"}) and _input(value.get("input")):
        projected, _ = _project(value["input"])
        return {**value, "input": projected}, True
    # These are the existing authenticated journal data coordinates. Unknown
    # siblings stay in the ordinary wrapper budget; they never gain an escape.
    if type(value.get("attempt")) is dict and set(value["attempt"]) == _ATTEMPT and _input(value["attempt"].get("input")):
        projected, _ = _project(value["attempt"])
        return {**value, "attempt": projected}, True
    if _envelope(value.get("envelope")):
        projected, _ = _project(value["envelope"])
        return {**value, "envelope": projected}, True
    if (value.get("operation") == "strict.register_review" and type(value.get("arguments")) is dict
            and set(value["arguments"]) == {"envelope"} and _envelope(value["arguments"]["envelope"])):
        projected, _ = _project(value["arguments"])
        return {**value, "arguments": projected}, True
    if (type(value.get("result")) is dict and type(value["result"].get("schema_version")) is str
            and value["result"]["schema_version"] in _FAMILY | _NATIVE):
        if value.get("state") == "observed" and set(value) == {"state", "result"} or "observation" in value and "error_code" in value:
            projected, _ = _project(value["result"])
            return {**value, "result": projected}, True
    if version in {"research-session-current/v1", "research-session-current/v2"}:
        release = value.get("canonical_release")
        if type(release) is dict and type(release.get("publication")) is dict and release["publication"].get("schema_version") == "evidence-strict-publication/v3":
            projected, _ = _project(release["publication"])
            return {**value, "canonical_release": {**release, "publication": projected}}, True
    if version in {"research-session-next/v1", "research-session-next/v2", "research-session-next/v3", "research-session-next/v4"}:
        projected, selected = _project(value.get("current"))
        if selected:
            return {**value, "current": projected}, True
    return value, False


def _aggregate(value, *, max_nodes, count_keys):
    pending, count = [(value, frozenset())], 0
    while pending:
        item, parents = pending.pop()
        count += 1
        if count > max_nodes:
            raise ValueError("review_dependency_aggregate_bound")
        if type(item) in {dict, list}:
            if id(item) in parents:
                raise ValueError("review_dependency_cycle")
            parents = parents | {id(item)}
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise ValueError("review_dependency_key_invalid")
                pending.extend((child, parents) for child in item.values())
                if count_keys:
                    pending.extend((key, parents) for key in item)
            else:
                pending.extend((child, parents) for child in item)
        elif type(item) is float and not math.isfinite(item):
            raise ValueError("review_dependency_value_invalid")
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("review_dependency_value_invalid")


def encoded(value, *, max_bytes=1_048_576, max_nodes=65_536, count_keys=True):
    """Return exact bytes for recognized formats, otherwise use the legacy path."""
    projected, selected = _project(value)
    if not selected:
        return None
    _aggregate(value, max_nodes=max_nodes, count_keys=count_keys)
    legacy.detached(projected)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError("review_dependency_bytes_bound")
    return raw


def detached(value):
    try:
        raw = encoded(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        legacy.refuse("INVALID")
    return legacy.detached(value) if raw is None else json.loads(raw)


def canonical(value):
    try:
        raw = encoded(value)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        legacy.refuse("INVALID")
    return legacy.canonical(value) if raw is None else raw


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()
