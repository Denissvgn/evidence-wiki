#!/usr/bin/env python3
"""Bounded closed contracts for dependency-bound reviews."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from contextvars import ContextVar
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from threading import Lock

from _evidence_revision import canonical_bytes, content_id
from _workspace_module_loader import load_workspace_module

MAX_BYTES = 1_048_576
MAX_NODES = 65_536
MAX_DEPTH = 16
MATERIAL = "research-dependency-review-material/v1"
BASIS = "evidence-review-dependency-basis/v1"
MODE = "claim_dependencies_v1"
_TYPED_ROOTS = ContextVar("dependency_schema_typed_roots", default=None)
_CHILD_ROOTS = ContextVar("dependency_schema_child_roots", default=None)
_SCHEMA_DOCUMENTS = ContextVar("dependency_schema_documents", default=None)
_NODE_DISPATCH = ContextVar("dependency_schema_node_dispatch", default=None)
_NATIVE_NODE_FINGERPRINT = "3c6732f2d9e122d9b82c528e3fed89fcb56a5a89476a71c19a428355f5027b9e"
_SHAPE_CACHE = {}
_SCHEMA_SUCCESSES = OrderedDict()
_SCHEMA_CACHE_LIMIT = 64
_SCHEMA_CACHE_KEY_BYTES = 24 * MAX_BYTES
_SCHEMA_CACHE_ENTRY_CHARGE = 1024
_SCHEMA_CACHE_MIN_BYTES = 4096
_SCHEMA_CACHE_LOCK = Lock()
MECHANICAL_REASONS = frozenset({
    "strict_coverage_required", "strict_claim_unresolved", "strict_evidence_missing",
    "strict_source_revision_changed", "strict_source_time_unconfirmed", "strict_source_age_invalid",
    "strict_capture_insufficient", "strict_grounding_failed", "strict_computation_missing_or_stale",
    "strict_computation_value_mismatch", "strict_computation_source_missing",
})


def _require(condition, reason="review_dependency_contract_invalid"):
    if not condition:
        raise ValueError(reason)


def _shapes():
    return load_workspace_module(Path(__file__).resolve().parent, "_review_dependency_shapes", cache=_SHAPE_CACHE)


def schema_documents():
    """Return detached schemas; no external resolver or workspace declaration."""
    return deepcopy(_shapes().DOCUMENTS)


def _bounded(value, typed_roots=frozenset(), child_roots=frozenset()):
    pending = [(value, 1, frozenset())]
    nodes = 0
    while pending:
        item, depth, parents = pending.pop()
        if id(item) in typed_roots:
            count = [nodes]
            _node(item, _count=count)
            nodes = count[0]
            continue
        nodes += 1
        _require(nodes <= MAX_NODES and depth <= MAX_DEPTH, "review_dependency_tree_bound")
        if type(item) in {dict, list}:
            identity = id(item)
            _require(identity not in parents, "review_dependency_cycle")
            parents = parents | {identity}
            _require(len(item) <= 4096, "review_dependency_collection_bound")
            if type(item) is dict:
                _require(all(type(key) is str for key in item), "review_dependency_key_invalid")
                for key, child in item.items():
                    pending.append((key, depth + 1, parents))
                    # Versioned native children retain their own depth budget.
                    child_depth = 1 if id(child) in child_roots else depth + 1
                    pending.append((child, child_depth, parents))
            else:
                pending.extend((child, depth + 1, parents) for child in item)
        elif type(item) is str:
            _require(len(item) <= 65_536 and not any(0xD800 <= ord(c) <= 0xDFFF for c in item),
                     "review_dependency_string_invalid")
        elif type(item) is float:
            _require(math.isfinite(item), "review_dependency_number_invalid")
        else:
            _require(item is None or type(item) in {bool, int}, "review_dependency_value_invalid")
    try:
        raw = canonical_bytes(value)
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise ValueError("review_dependency_value_invalid") from error
    _require(len(raw) <= MAX_BYTES, "review_dependency_bytes_bound")


def _equal(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return left.keys() == right.keys() and all(_equal(value, right[key]) for key, value in left.items())
    if type(left) is list:
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _matches(value, schema, root, depth=0, *, collect=True):
    typed, children = set(), set()
    parent_typed, parent_children = _TYPED_ROOTS.get(), _CHILD_ROOTS.get()
    typed_token, child_token = _TYPED_ROOTS.set(typed), _CHILD_ROOTS.set(children)
    try:
        _shape(value, schema, root, depth)
        if collect:
            if parent_typed is not None:
                parent_typed.update(typed)
            if parent_children is not None:
                parent_children.update(children)
        return True
    except ValueError:
        return False
    finally:
        _TYPED_ROOTS.reset(typed_token)
        _CHILD_ROOTS.reset(child_token)


def _shape_documents():
    scope = _SCHEMA_DOCUMENTS.get()
    if scope is not None and scope[1][0]:
        return scope[0]
    return _shapes().DOCUMENTS


def _native_node_choices(schema):
    """Recognize only owned copies of the current closed native node union."""
    scope = _NODE_DISPATCH.get()
    documents_scope = _SCHEMA_DOCUMENTS.get()
    if (scope is None or not scope[1][0] or documents_scope is None
            or scope[1] is not documents_scope[1]):
        return None
    cache = scope[0]
    cached = cache.get(id(schema))
    if cached is not None and cached[0] is schema:
        return cached[1]
    material = documents_scope[0].get(MATERIAL)
    definitions = material.get("$defs") if type(material) is dict else None
    native = definitions.get("node") if type(definitions) is dict else None
    if type(native) is not dict or not _equal(schema, native):
        return None
    # Only this audited structural generation may skip sibling evaluation.
    # Other generations can contain malformed branches whose ordinary errors
    # must remain observable even when a different tag would match.
    try:
        schema_raw, _ = _exact_json_bytes(schema)
    except ValueError:
        return None
    if hashlib.sha256(schema_raw).hexdigest() != _NATIVE_NODE_FINGERPRINT:
        return None
    choices = schema.get("oneOf")
    if type(choices) is not list or not 0 < len(choices) <= 128:
        return None
    tagged = {}
    for branch in choices:
        if (type(branch) is not dict or branch.get("type") != "object"
                or branch.get("additionalProperties") is not False
                or type(branch.get("required")) is not list or "t" not in branch["required"]):
            return None
        properties = branch.get("properties")
        tag_schema = properties.get("t") if type(properties) is dict else None
        if type(tag_schema) is not dict or set(tag_schema) != {"const"}:
            return None
        tag = tag_schema["const"]
        if type(tag) is not str or tag in tagged:
            return None
        tagged[tag] = branch
    cache[id(schema)] = (schema, tagged)
    return tagged


def _shape(value, schema, root, depth=0):
    _require(depth <= 256, "review_dependency_schema_bound")
    if type(schema) is bool:
        _require(schema)
        return
    _require(type(schema) is dict, "review_dependency_schema_invalid")
    nested = schema is not root
    if "$id" in schema:
        root = schema
        children = _CHILD_ROOTS.get()
        if children is not None and any(_equal(schema, shape) for shape in _shape_documents().values()):
            children.add(id(value))
    properties = schema.get("properties")
    version = properties.get("schema_version") if type(properties) is dict else None
    if nested and type(version) is dict and version.get("const") == "evidence-review-producer-manifest/v1":
        manifest = _shape_documents().get("evidence-review-producer-manifest/v1")
        inline = ({key: child for key, child in manifest.items() if key not in {"$id", "$schema"}}
                  if type(manifest) is dict else None)
        if manifest is not None and (_equal(schema, manifest) or _equal(schema, inline)):
            # This closed native child has no typed references or nested root
            # allowances to propagate. Keep the outer aggregate bound walk.
            encoded_schema = json.dumps(manifest)
            if '"$ref"' not in encoded_schema and encoded_schema.count('"$id"') == 1:
                validate_schema(value, manifest)
                return
    if "$ref" in schema:
        pointer = schema["$ref"]
        _require(type(pointer) is str and pointer.startswith("#/"), "review_dependency_reference_invalid")
        target = root
        for part in pointer[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            _require(type(target) is dict and part in target, "review_dependency_reference_invalid")
            target = target[part]
        _shape(value, target, root, depth + 1)
        if pointer == "#/$defs/node":
            _node(value)
            roots = _TYPED_ROOTS.get()
            if roots is not None:
                roots.add(id(value))
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            choices = schema[key]
            _require(type(choices) is list and 0 < len(choices) <= 128, "review_dependency_schema_invalid")
            tagged = _native_node_choices(schema) if key == "oneOf" else None
            if tagged is None:
                matched = sum(_matches(value, choice, root, depth + 1) for choice in choices)
            else:
                tag = value.get("t") if type(value) is dict else None
                branch = tagged.get(tag) if type(tag) is str else None
                matched = int(branch is not None and _matches(value, branch, root, depth + 1))
            _require(matched == len(choices) if key == "allOf" else matched == 1 if key == "oneOf" else matched > 0)
    if "not" in schema:
        _require(not _matches(value, schema["not"], root, depth + 1, collect=False))
    if "if" in schema:
        branch = "then" if _matches(value, schema["if"], root, depth + 1, collect=False) else "else"
        if branch in schema:
            _shape(value, schema[branch], root, depth + 1)
    kinds = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None), "number": (int, float)}
    if "type" in schema:
        declared = schema["type"] if type(schema["type"]) is list else [schema["type"]]
        _require(all(kind in kinds for kind in declared), "review_dependency_schema_invalid")
        _require(any(type(value) in (kinds[kind] if type(kinds[kind]) is tuple else (kinds[kind],))
                     for kind in declared))
    if "const" in schema:
        _require(_equal(value, schema["const"]))
    if "enum" in schema:
        _require(any(_equal(value, choice) for choice in schema["enum"]))
    if type(value) is dict:
        _require(set(schema.get("required", ())) <= value.keys())
        _require(schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", 4096))
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if "propertyNames" in schema:
                _shape(key, schema["propertyNames"], root, depth + 1)
            if key in props:
                _shape(child, props[key], root, depth + 1)
            else:
                _shape(child, additional, root, depth + 1)
    elif type(value) is list:
        _require(schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 4096))
        prefix = schema.get("prefixItems", [])
        for index, child in enumerate(value):
            _shape(child, prefix[index] if index < len(prefix) else schema.get("items", True), root, depth + 1)
        if schema.get("uniqueItems"):
            keys = [canonical_bytes(child) for child in value]
            _require(len(keys) == len(set(keys)))
        if "contains" in schema:
            count = sum(_matches(child, schema["contains"], root, depth + 1) for child in value)
            _require(schema.get("minContains", 1) <= count <= schema.get("maxContains", len(value)))
    elif type(value) is str:
        _require(schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 65_536))
        if "pattern" in schema:
            _require(re.search(schema["pattern"], value) is not None)
        if schema.get("format") == "date":
            _require(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None)
            try:
                _require(date.fromisoformat(value).isoformat() == value)
            except ValueError as error:
                raise ValueError("review_dependency_date_invalid") from error
    elif type(value) in {int, float}:
        _require(schema.get("minimum", value) <= value <= schema.get("maximum", value))
        _require("exclusiveMinimum" not in schema or value > schema["exclusiveMinimum"])
        _require("exclusiveMaximum" not in schema or value < schema["exclusiveMaximum"])


def _exact_json_bytes(value):
    """Reject JSON coercions before constructing an immutable memo key."""
    pending = [(value, False)]
    active, seen = set(), set()
    unaliased = True
    nodes, scheduled, string_bytes = 0, 1, 0
    while pending:
        item, leaving = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        # Each JSON node occupies at least one byte in the bounded encoding.
        _require(nodes <= MAX_BYTES, "review_dependency_bytes_bound")
        kind = type(item)
        if kind in {dict, list}:
            identity = id(item)
            _require(identity not in active, "review_dependency_cycle")
            if identity in seen:
                unaliased = False
            seen.add(identity)
            active.add(identity)
            scheduled += len(item) * (2 if kind is dict else 1)
            _require(scheduled + string_bytes <= MAX_BYTES, "review_dependency_bytes_bound")
            pending.append((item, True))
            if kind is dict:
                _require(all(type(key) is str for key in item), "review_dependency_key_invalid")
                pending.extend((key, False) for key in item)
                pending.extend((child, False) for child in item.values())
            else:
                pending.extend((child, False) for child in item)
        elif kind is str:
            string_bytes += max(0, len(item) - 1)
            _require(scheduled + string_bytes <= MAX_BYTES, "review_dependency_bytes_bound")
        elif kind is float:
            _require(math.isfinite(item), "review_dependency_number_invalid")
        else:
            _require(item is None or kind in {bool, int}, "review_dependency_value_invalid")
    try:
        raw = canonical_bytes(value)
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise ValueError("review_dependency_value_invalid") from error
    _require(len(raw) <= MAX_BYTES, "review_dependency_bytes_bound")
    return raw, unaliased


def detached_json(value):
    """Detach exact bounded JSON bytes without preserving container aliases."""
    raw, _unaliased = _exact_json_bytes(value)
    return json.loads(raw)


def _schema_memo_key(raw, schema):
    shapes = _shapes()
    try:
        schema_raw, _ = _exact_json_bytes(schema)
        documents_raw, _ = _exact_json_bytes(shapes.DOCUMENTS)
    except ValueError:
        # Uncacheable schemas still receive the ordinary structural validation.
        return None
    return (raw, schema_raw, documents_raw, shapes, _shape, _matches, _equal,
            _bounded, _node, _require, canonical_bytes, MAX_BYTES, MAX_NODES, MAX_DEPTH,
            _shape_documents, _native_node_choices, _NATIVE_NODE_FINGERPRINT)


def validate_schema(value, schema):
    """Validate a bounded child with local references only."""
    raw, unaliased = _exact_json_bytes(value)
    # Structural depth allowances follow object identities. Shared containers
    # therefore retain the ordinary validation path, regardless of JSON bytes.
    original_schema = schema
    schema_owned = False
    key = (_schema_memo_key(raw, schema)
           if unaliased and len(raw) >= _SCHEMA_CACHE_MIN_BYTES else None)
    if unaliased:
        # Every unaliased validation owns its input snapshot, even when too
        # small to amortize the native-document memo key.
        value = json.loads(raw)
        if key is not None:
            schema = json.loads(key[1])
            schema_owned = True
        else:
            try:
                schema_raw, _ = _exact_json_bytes(schema)
            except ValueError:
                # Preserve ordinary structural validation for non-JSON schemas.
                schema = original_schema
            else:
                schema = json.loads(schema_raw)
                schema_owned = True
    hit = False
    if key is not None:
        with _SCHEMA_CACHE_LOCK:
            if (_SCHEMA_SUCCESSES
                    and next(iter(_SCHEMA_SUCCESSES))[2:] != key[2:]):
                _SCHEMA_SUCCESSES.clear()
            if key in _SCHEMA_SUCCESSES:
                _SCHEMA_SUCCESSES.move_to_end(key)
                hit = True
    if hit:
        return deepcopy(value)
    documents = json.loads(key[2]) if key is not None else deepcopy(_shapes().DOCUMENTS)
    # Copies of this context must stop using the snapshot after validation.
    lifetime = [True]
    documents_token = _SCHEMA_DOCUMENTS.set((documents, lifetime))
    dispatch_token = _NODE_DISPATCH.set(({}, lifetime) if schema_owned else None)
    roots, children = set(), set()
    token = _TYPED_ROOTS.set(roots)
    child_token = _CHILD_ROOTS.set(children)
    try:
        _shape(value, schema, schema)
        _bounded(value, roots, children)
    finally:
        lifetime[0] = False
        _NODE_DISPATCH.reset(dispatch_token)
        _SCHEMA_DOCUMENTS.reset(documents_token)
        _TYPED_ROOTS.reset(token)
        _CHILD_ROOTS.reset(child_token)
    if key is not None and _schema_memo_key(raw, original_schema) == key:
        with _SCHEMA_CACHE_LOCK:
            if (_SCHEMA_SUCCESSES
                    and next(iter(_SCHEMA_SUCCESSES))[2:] != key[2:]):
                _SCHEMA_SUCCESSES.clear()
            _SCHEMA_SUCCESSES[key] = None
            _SCHEMA_SUCCESSES.move_to_end(key)
            while (len(_SCHEMA_SUCCESSES) > _SCHEMA_CACHE_LIMIT
                   or sum(sum(len(part) for part in cached[:3])
                          + _SCHEMA_CACHE_ENTRY_CHARGE
                          for cached in _SCHEMA_SUCCESSES) > _SCHEMA_CACHE_KEY_BYTES):
                _SCHEMA_SUCCESSES.popitem(last=False)
    return deepcopy(value)


def _node(value, *, _depth=1, _count=None):
    count = [0] if _count is None else _count
    count[0] += 1
    _require(_depth <= MAX_DEPTH and count[0] <= MAX_NODES, "review_dependency_node_bound")
    _require(type(value) is dict and type(value.get("t")) is str, "review_dependency_node_invalid")
    kind = value["t"]
    _require(set(value) == ({"t"} if kind == "null" else {"t", "v"}), "review_dependency_node_invalid")
    raw = value.get("v")
    if kind == "null":
        return
    if kind == "boolean":
        _require(type(raw) is bool)
    elif kind == "integer":
        _require(type(raw) is str and re.fullmatch(r"0|-?[1-9][0-9]*", raw) is not None)
    elif kind == "float":
        try:
            number = float.fromhex(raw)
            _require(math.isfinite(number) and number.hex() == raw)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("review_dependency_float_invalid") from error
    elif kind == "string":
        _require(type(raw) is str)
    elif kind == "date":
        try:
            _require(type(raw) is str and date.fromisoformat(raw).isoformat() == raw)
        except (TypeError, ValueError) as error:
            raise ValueError("review_dependency_date_invalid") from error
    elif kind == "timestamp":
        try:
            parsed = datetime.fromisoformat(raw)
            _require(type(raw) is str and parsed.isoformat() == raw)
        except (TypeError, ValueError) as error:
            raise ValueError("review_dependency_timestamp_invalid") from error
    elif kind == "list":
        _require(type(raw) is list)
        for child in raw:
            _node(child, _depth=_depth + 1, _count=count)
    elif kind == "mapping":
        _require(type(raw) is list)
        keys = []
        for pair in raw:
            _require(type(pair) is list and len(pair) == 2 and type(pair[0]) is str)
            count[0] += 1
            _require(count[0] <= MAX_NODES, "review_dependency_node_bound")
            keys.append(pair[0].encode("utf-8"))
            _node(pair[1], _depth=_depth + 1, _count=count)
        _require(keys == sorted(set(keys)))
    else:
        _require(False, "review_dependency_node_invalid")


def validate_node(value):
    schema = {"$ref": "#/$defs/node", "$defs": _shapes().DOCUMENTS[MATERIAL]["$defs"]}
    validate_schema(value, schema)
    _node(value)
    return value


def validate_material(value, *, complete=True):
    schema = deepcopy(_shapes().DOCUMENTS[MATERIAL])
    if not complete:
        schema["required"].remove("dependency_basis_id")
        del schema["properties"]["dependency_basis_id"]
    validate_schema(value, schema)
    claims = value["claims"]
    keys = [row["id"] for row in claims]
    _require(len(keys) == len(set(keys)) and keys[0] == value["claim_id"], "review_dependency_closure_invalid")
    by_id = {row["id"]: row for row in claims}
    order, active, visited = [], set(), set()

    def visit(key):
        _require(key in by_id and key not in active, "review_dependency_closure_invalid")
        if key in visited:
            return
        visited.add(key)
        active.add(key)
        order.append(key)
        row = by_id[key]
        _require(("calculations" in row) == (value["claim_schema_version"] == "evidence-strict-claims/v2"),
                 "review_dependency_claim_schema_invalid")
        for premise in row["premises"]:
            visit(premise)
        active.remove(key)

    visit(value["claim_id"])
    _require(order == keys and visited == set(keys), "review_dependency_closure_invalid")
    questions = value["questions"]
    slugs = [row["manifest_question"]["slug"] for row in questions]
    _require(slugs == sorted(set(slugs), key=lambda x: x.encode("utf-8"))
             and set(slugs) == {row["question_slug"] for row in claims}, "review_dependency_questions_invalid")
    paths = [row["path"] for row in questions]
    _require(len(paths) == len(set(paths)), "review_dependency_questions_invalid")
    wrapper = value["global_material"]
    _require(wrapper["dependency_id"] == content_id("research-global-dependency-material/v1",
             {key: child for key, child in wrapper.items() if key != "dependency_id"}), "review_dependency_global_id_invalid")
    producer = wrapper["material"]["worker_semantics"]["producer"]
    validate("evidence-review-producer-manifest/v1", producer["manifest"])
    _require(producer["identity"]["manifest_id"] == content_id("evidence-review-producer-manifest/v1", producer["manifest"])
             and value["producer_id"] == content_id("evidence-review-producer/v1", producer["identity"]),
             "review_dependency_producer_invalid")
    return deepcopy(value)


def item_id(kind, coordinate, value):
    return content_id("evidence-review-dependency-item/v1", {"kind": kind, "coordinate": coordinate, "value": value})


def basis(preimage, entries, *, required_review_role, computation_mode):
    validate_material(preimage, complete=False)
    value = {"schema_version": BASIS, "review_basis_mode": MODE,
             "target_claim_id": preimage["claim_id"], "claim_schema_version": preimage["claim_schema_version"],
             "required_review_role": required_review_role, "computation_mode": computation_mode,
             "premise_closure": sorted((row["id"] for row in preimage["claims"][1:]), key=lambda x: x.encode("utf-8")),
             "question_slugs": [row["manifest_question"]["slug"] for row in preimage["questions"]],
             "semantic_material_id": content_id(MATERIAL, preimage), "dependencies": deepcopy(entries),
             "coverage": {"classification_complete": True, "reviewer_material_complete": True,
                          "truncated": False, "missing_coordinates": [], "unclassified_coordinates": []}}
    value["basis_id"] = content_id(BASIS, value)
    validate(BASIS, value)
    return value


def _observation(value, *, claim_id, basis_id, producer_id):
    _require(value is not None and value["basis_id"] == basis_id and value["claim_id"] == claim_id
             and value["producer_id"] == producer_id, "review_dependency_observation_invalid")
    reasons = value["reasons"]
    _require(reasons == sorted(set(reasons), key=lambda item: item.encode("utf-8"))
             and set(reasons) <= MECHANICAL_REASONS, "review_dependency_observation_invalid")


def _selection(row, producer_id, *, claim_id):
    basis_id = row["claim_basis_id"]
    selected = row["review"]
    if "basis_id" in selected:
        _require(selected["basis_id"] == basis_id, "review_dependency_selection_invalid")
    if row["accepted"]:
        _require(basis_id is not None and producer_id is not None
                 and row["observed_workspace_basis_id"] is not None and selected["passed"] is True
                 and "review_id" in selected, "review_dependency_acceptance_invalid")
    return basis_id


def validate(name, value):
    docs = _shapes().DOCUMENTS
    _require(name in docs, "review_dependency_schema_unsupported")
    if name == MATERIAL:
        return validate_material(value)
    validate_schema(value, docs[name])
    if name == BASIS:
        _require(value["basis_id"] == content_id(BASIS, {key: child for key, child in value.items() if key != "basis_id"}),
                 "review_dependency_basis_id_invalid")
        entries = value["dependencies"]
        coordinates = [(row["scope"], row["kind"], row["coordinate"]) for row in entries]
        _require(coordinates == sorted(set(coordinates), key=lambda row: tuple(x.encode("utf-8") for x in row)),
                 "review_dependency_coordinates_invalid")
        _require(value["target_claim_id"] not in value["premise_closure"])
        _require(value["premise_closure"] == sorted(set(value["premise_closure"]), key=lambda x: x.encode("utf-8")))
        _require(value["question_slugs"] == sorted(set(value["question_slugs"]), key=lambda x: x.encode("utf-8")))
    elif name in {"evidence-review-producer-manifest/v1", "evidence-review-producer-inventory/v1"}:
        rows = value["members"] if "members" in value else value["paths"]
        names = [row["path"] if type(row) is dict else row for row in rows]
        _require(names == sorted(set(names), key=lambda x: x.encode("utf-8")), "review_dependency_members_invalid")
        if "members" in value:
            _require(sum(row["size_bytes"] for row in rows) <= 33_554_432, "review_dependency_members_bound")
        for path in names:
            _require(len(path.encode("utf-8")) <= 1024 and not path.startswith("/")
                     and "\\" not in path and all(part not in {"", ".", ".."} for part in path.split("/")),
                     "review_dependency_member_path_invalid")
            _require(not any(ord(char) < 32 or ord(char) == 127 for char in path),
                     "review_dependency_member_path_invalid")
    elif name == "evidence-strict-review/v3":
        material = value["snapshot"]["semantic_material"]
        validate_material(material)
        manifest = value["dependency_basis"]
        validate(BASIS, manifest)
        _require(value["basis_id"] == manifest["basis_id"] == material["dependency_basis_id"]
                 and value["claim_id"] == manifest["target_claim_id"] == material["claim_id"]
                 and value["claim_schema_version"] == manifest["claim_schema_version"] == material["claim_schema_version"])
        semantic = content_id(MATERIAL, {key: child for key, child in material.items() if key != "dependency_basis_id"})
        _require(value["delivery"]["semantic_material_id"] == manifest["semantic_material_id"] == semantic)
        expected = {"schema_version": "evidence-strict-authorship/v2", "basis_id": value["basis_id"],
                    "claim_id": value["claim_id"], "claim_schema_version": value["claim_schema_version"],
                    "observed_workspace_basis_id": value["observed_workspace_basis_id"], "semantic_material_id": semantic}
        _require(value["generator"]["payload"] == expected and value["observation"]["basis_id"] == value["basis_id"]
                 and value["observation"]["claim_id"] == value["claim_id"]
                 and value["observation"]["producer_id"] == material["producer_id"])
        _observation(value["observation"], claim_id=value["claim_id"], basis_id=value["basis_id"],
                     producer_id=material["producer_id"])
        _require(value["snapshot"]["claim"] == material["claims"][0]
                 and value["snapshot"]["premises"] == sorted(material["claims"][1:], key=lambda row: row["id"].encode("utf-8")),
                 "review_dependency_snapshot_invalid")
    elif name == "evidence-review-delivery-binding/v1":
        seed = value["order_seed"]
        _require(len(seed) % 2 == 0 and value["order_seed_sha256"] == "sha256:" + hashlib.sha256(bytes.fromhex(seed)).hexdigest(),
                 "review_dependency_order_seed_invalid")
        for mapping in value["aliases"].values():
            _require(len(mapping) == len(set(mapping.values())), "review_dependency_alias_invalid")
        _require(set(value["source_permutation"]) <= set(value["aliases"]["sources"].values()),
                 "review_dependency_permutation_invalid")
    elif name == "evidence-strict-result/v3":
        claim_ids = [row["claim"]["id"] for row in value["claims"]]
        _require(len(claim_ids) == len(set(claim_ids)), "review_dependency_claim_duplicate")
        for row in value["claims"]:
            _require(("calculations" in row["claim"]) == (value["claim_schema_version"] == "evidence-strict-claims/v2"),
                     "review_dependency_claim_schema_invalid")
            claim_basis = _selection(row, value["review_producer_id"], claim_id=row["claim"]["id"])
            _require((claim_basis is None) == (row["observation"] is None), "review_dependency_observation_invalid")
            _require(row["accepted"] == (not row["reasons"]), "review_dependency_acceptance_invalid")
            if claim_basis is not None:
                _require(value["review_producer_id"] is not None)
                _observation(row["observation"], claim_id=row["claim"]["id"], basis_id=claim_basis,
                             producer_id=value["review_producer_id"])
    elif name == "evidence-strict-review-status/v2":
        for row in value["claims"]:
            _selection(row, value["review_producer_id"], claim_id=row["claim_id"])
            _require(row["accepted"] == (not row["reasons"]), "review_dependency_acceptance_invalid")
    elif name == "evidence-strict-publication/v3":
        if value["verdict"] == "ship":
            _require(value["review_producer_id"] is not None and value["selection_complete"] is True,
                     "review_dependency_publication_invalid")
        for question in value["questions"]:
            for row in question["claims"]:
                _require(("calculations" in row) == (value["claim_schema_version"] == "evidence-strict-claims/v2"),
                         "review_dependency_claim_schema_invalid")
                _require(value["review_producer_id"] is not None)
                _selection({**row, "accepted": True, "review": row["verification"]["review"]},
                           value["review_producer_id"], claim_id=row["id"])
                _observation(row["verification"]["observation"], claim_id=row["id"],
                             basis_id=row["claim_basis_id"], producer_id=value["review_producer_id"])
    return deepcopy(value)


def loads(raw):
    """Reject ambiguous JSON before bounded structural validation."""
    _require(type(raw) in {str, bytes} and len(raw) <= MAX_BYTES, "review_dependency_bytes_bound")

    def pairs(rows):
        value = {}
        for key, child in rows:
            _require(key not in value, "review_dependency_duplicate_key")
            value[key] = child
        return value

    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: _require(False, "review_dependency_number_invalid"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ValueError("review_dependency_json_invalid") from error
    _bounded(value)
    return value
