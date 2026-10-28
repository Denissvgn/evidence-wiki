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


class _RecordCodecMemo:
    """Bound pure codec successes; no journal authentication is retained."""

    from collections import OrderedDict
    from threading import RLock

    ENTRY_LIMIT = 64
    BYTE_LIMIT = 16_777_216
    GENERATION_BYTE_LIMIT = 2_097_152
    ENTRY_OVERHEAD = 1024
    MIN_BYTES = 4096
    entries = OrderedDict()
    lock = RLock()
    generation = None
    schema_backend = None
    retained_bytes = 0

    @classmethod
    def _select(cls, generation, charge):
        bounds = (cls.ENTRY_LIMIT, cls.BYTE_LIMIT, cls.GENERATION_BYTE_LIMIT,
            cls.ENTRY_OVERHEAD, cls.MIN_BYTES)
        if (any(type(bound) is not int for bound in bounds)
                or min(bounds[:3]) < 1 or min(bounds[3:]) < 0
                or charge > cls.GENERATION_BYTE_LIMIT or charge > cls.BYTE_LIMIT):
            cls.entries.clear()
            cls.generation, cls.retained_bytes = None, 0
            return False
        if cls.generation != generation:
            cls.entries.clear()
            cls.generation, cls.retained_bytes = generation, charge
        return True

    @classmethod
    def get(cls, generation, charge, raw):
        with cls.lock:
            if not cls._select(generation, charge):
                return None
            saved = cls.entries.get(raw)
            if saved is not None:
                cls.entries.move_to_end(raw)
                return saved[:2]
        return None

    @classmethod
    def put(cls, generation, charge, raw, canonical, logical):
        size = len(raw) + len(canonical) + len(logical) + cls.ENTRY_OVERHEAD
        with cls.lock:
            if not cls._select(generation, charge) or size + charge > cls.BYTE_LIMIT:
                return
            previous = cls.entries.pop(raw, None)
            if previous is not None:
                cls.retained_bytes -= previous[2]
            cls.entries[raw] = canonical, logical, size
            cls.retained_bytes += size
            while (len(cls.entries) > cls.ENTRY_LIMIT
                    or cls.retained_bytes > cls.BYTE_LIMIT):
                _, removed = cls.entries.popitem(last=False)
                cls.retained_bytes -= removed[2]


def _codec_schema_bytes(value, native):
    """Encode exact inert schemas; shared containers retain expanded bounds."""
    maximum = native.MAX_BYTES

    def encoded():
        try:
            raw = native.canonical_bytes(value)
        except (TypeError, ValueError, RecursionError, OverflowError) as error:
            raise ValueError("review_dependency_value_invalid") from error
        if len(raw) > native.MAX_BYTES:
            raise ValueError("review_dependency_bytes_bound")
        return raw

    # Ordinary schema trees need no per-node frame or path-identity copies.
    # A repeated container conservatively selects the expanded DAG walk below.
    pending, seen, scheduled, string_bytes = [value], set(), 1, 0
    while pending:
        item = pending.pop()
        kind = type(item)
        if kind is dict or kind is list:
            identity = id(item)
            if identity in seen:
                break
            seen.add(identity)
            scheduled += len(item) * (2 if kind is dict else 1)
            if scheduled + string_bytes > maximum:
                raise ValueError("review_dependency_bytes_bound")
            if kind is dict:
                for key in item:
                    if type(key) is not str:
                        raise ValueError("review_dependency_key_invalid")
                    string_bytes += max(0, len(key) - 1)
                pending.extend(item.values())
            else:
                pending.extend(item)
        elif kind is str:
            string_bytes += max(0, len(item) - 1)
        elif kind is float:
            if not native.math.isfinite(item):
                raise ValueError("review_dependency_number_invalid")
        elif item is not None and kind is not bool and kind is not int:
            raise ValueError("review_dependency_value_invalid")
        if scheduled + string_bytes > maximum:
            raise ValueError("review_dependency_bytes_bound")
    else:
        return encoded()

    completed, active, stack = {}, set(), []
    pending = value
    while True:
        kind = type(pending)
        if kind is dict or kind is list:
            identity = id(pending)
            if identity in active:
                raise ValueError("review_dependency_cycle")
            result = completed.get(identity)
            if result is None:
                arity = len(pending) * (2 if kind is dict else 1)
                if arity + 1 > maximum:
                    raise ValueError("review_dependency_bytes_bound")
                if kind is dict:
                    if any(type(key) is not str for key in pending):
                        raise ValueError("review_dependency_key_invalid")
                    children = iter((*pending, *pending.values()))
                else:
                    children = iter(pending)
                active.add(identity)
                stack.append([identity, children, 1])
                try:
                    pending = next(children)
                    continue
                except StopIteration:
                    result = 1
                    completed[identity] = result
                    active.remove(identity)
                    stack.pop()
        elif kind is str:
            result = max(1, len(pending))
        elif kind is float:
            if not native.math.isfinite(pending):
                raise ValueError("review_dependency_number_invalid")
            result = 1
        elif pending is None or kind is bool or kind is int:
            result = 1
        else:
            raise ValueError("review_dependency_value_invalid")
        if result > maximum:
            raise ValueError("review_dependency_bytes_bound")
        while stack:
            frame = stack[-1]
            frame[2] += result
            if frame[2] > maximum:
                raise ValueError("review_dependency_bytes_bound")
            try:
                pending = next(frame[1])
                break
            except StopIteration:
                result = frame[2]
                completed[frame[0]] = result
                active.remove(frame[0])
                stack.pop()
        else:
            break
    return encoded()


def _codec_schema_backend(native):
    """Admit only the exact supported source, code, namespace and defaults."""
    import ast
    import types
    from pathlib import Path

    known_sources = (
        ("_schema_memo_key", "2070a625f2d71a655c57a501e50a2f5ef16a6641698cce8e65752a36647db23f"),
        ("_exact_json_bytes", "833fd37644db8026c3d838f1d48c9f05896e82b81abf2c7cdd47aeb97243f314"),
        ("_require", "bbb365f9c9144e2d5f92f243bb5403a53a49f6f5dcbb43af7486eb984a235ded"))
    try:
        selected = tuple(getattr(native, name) for name, _ in known_sources)
        expected_defaults = (None, None, ("review_dependency_contract_invalid",))
        for function, defaults in zip(selected, expected_defaults, strict=True):
            if (type(function) is not types.FunctionType
                    or function.__globals__ is not vars(native)
                    or function.__code__.co_filename != native.__file__
                    or (defaults is None and function.__defaults__ is not None)
                    or (defaults is not None and (type(function.__defaults__) is not tuple
                        or len(function.__defaults__) != len(defaults)
                        or any(type(actual) is not type(expected) or actual != expected
                            for actual, expected in zip(function.__defaults__, defaults, strict=True))))
                    or function.__kwdefaults__ is not None or function.__closure__ is not None):
                _RecordCodecMemo.schema_backend = None
                return None
        key = (native, native.__file__, tuple((function, function.__code__) for function in selected))
        previous = getattr(_RecordCodecMemo, "schema_backend", None)
        if previous is not None and previous[0] == key:
            return key if previous[1] else None
        supported = False
        if Path(native.__file__).name == "_review_dependency_contract.py":
            with Path(native.__file__).open("rb") as stream:
                raw = stream.read(_RecordCodecMemo.GENERATION_BYTE_LIMIT + 1)
            if len(raw) <= _RecordCodecMemo.GENERATION_BYTE_LIMIT:
                text = raw.decode("utf-8")
                tree = ast.parse(text)
                definitions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
                compiled = compile(raw, native.__file__, "exec", dont_inherit=True)
                codes = {item.co_name: item for item in compiled.co_consts if type(item) is types.CodeType}
                supported = all(
                    hashlib.sha256((ast.get_source_segment(text, definitions[name]) + "\n").encode()).hexdigest() == expected
                    and codes[name] == function.__code__
                    for (name, expected), function in zip(known_sources, selected, strict=True))
        # One compatibility entry, held as part of the ordinary generation key.
        _RecordCodecMemo.schema_backend = key, supported
        return key if supported else None
    except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError, OSError, KeyError, SyntaxError):
        _RecordCodecMemo.schema_backend = None
        return None


def _codec_native_schema_key(native, backend):
    """Preserve every supported native preimage field; unknown code falls back."""
    if backend is None:
        return native._schema_memo_key(b"", {})
    shapes = native._shapes()
    try:
        schema_raw = _codec_schema_bytes({}, native)
        documents_raw = _codec_schema_bytes(shapes.DOCUMENTS, native)
    except ValueError:
        return None
    return (b"", schema_raw, documents_raw, shapes, native._shape, native._matches, native._equal,
        native._bounded, native._node, native._require, native.canonical_bytes,
        native.MAX_BYTES, native.MAX_NODES, native.MAX_DEPTH, native._shape_documents,
        native._native_node_choices, native._NATIVE_NODE_FINGERPRINT)


def _record_codec_key(value):
    """Bind inert exact bytes to the complete live codec/schema generation."""
    import sys

    from . import _review_dependency_json as native_json
    from . import _review_dependency_wire as wire
    from . import agent_execution, mutation_contracts, review_packet_contracts, review_wire

    native = native_json._native()
    # Nested validators own their own schema snapshots and depth identities.
    if native._SCHEMA_DOCUMENTS.get() is not None:
        return None
    try:
        raw, unaliased = native._exact_json_bytes(value)
        if not unaliased or len(raw) < _RecordCodecMemo.MIN_BYTES:
            return None
        backend = _codec_schema_backend(native)
        schema = _codec_native_schema_key(native, backend)
        if schema is None:
            return None
        families = tuple((name, (_codec_schema_bytes(native_json._family_schema(name), native) if backend is not None else native._exact_json_bytes(native_json._family_schema(name))[0]))
            for name in sorted(native_json._FAMILY))

        def constant(item):
            kind = type(item)
            if item is None or kind in (str, bool, int, float):
                return kind, item
            if kind in (tuple, frozenset, set):
                values = tuple(constant(child) for child in item)
                return kind, frozenset(values) if kind in (frozenset, set) else values
            raise TypeError("uncacheable_codec_constant")

        modules = (sys.modules[__name__], mutation_contracts, native_json, wire,
            review_wire, review_packet_contracts, agent_execution, native)
        functions, constants = [], []
        for module in modules:
            functions.append((module, tuple((name, child, getattr(child, "__code__", None)) for name, child in sorted(vars(module).items())
                if callable(child))))
            selected = []
            for name, child in sorted(vars(module).items()):
                if name.upper() == name and type(child) in (str, bool, int, float, tuple, frozenset, set):
                    selected.append((name, constant(child)))
            constants.append((module, tuple(selected)))
        limits = (_RecordCodecMemo.ENTRY_LIMIT, _RecordCodecMemo.BYTE_LIMIT,
            _RecordCodecMemo.GENERATION_BYTE_LIMIT, _RecordCodecMemo.ENTRY_OVERHEAD,
            _RecordCodecMemo.MIN_BYTES)
        generation = schema[2:], families, tuple(functions), tuple(constants), limits, _RecordCodecMemo.schema_backend
        # Charge immutable generation bytes once, including nested fingerprints.
        # Opaque module/function objects are existing identities, not copies.
        pending, seen, charge = [generation], set(), 0
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            kind = type(item)
            charge += 128
            if kind is bytes:
                charge += len(item)
            elif kind is str:
                charge += len(item) * 4
            elif kind in (tuple, frozenset):
                charge += len(item) * 16
                pending.extend(item)
        return generation, charge, raw
    except (AttributeError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        # Unsupported keys retain the ordinary complete validation path.
        return None


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


def _canonical_owned_record(value: Any, *, expanded_limit: int | None = None, use_memo: bool = True) -> tuple[bytes, dict[str, Any]]:
    """Return physical bytes and a separately owned logical record for one read."""
    if expanded_limit is not None and (type(expanded_limit) is not int or not 0 <= expanded_limit <= 16_777_216):
        refuse("INVALID")
    if type(use_memo) is not bool:
        refuse("INVALID")
    selected = _record_codec_key(value) if use_memo else None
    if selected is not None:
        generation, charge, raw = selected
        saved = _RecordCodecMemo.get(generation, charge, raw)
        if saved is not None and _record_codec_key(value) == selected:
            canonical, logical = saved
            owned = json.loads(logical)
            if expanded_limit is not None and expanded_size(owned) > expanded_limit:
                refuse("LIMIT")
            return canonical, owned
    logical = detached_record(value)
    if expanded_limit is not None and expanded_size(logical) > expanded_limit:
        refuse("LIMIT")
    canonical = _serialized(_owned_record_transport(logical, physical=False))
    if selected is not None and _record_codec_key(value) == selected:
        _RecordCodecMemo.put(generation, charge, raw, canonical, _serialized(logical))
    return canonical, logical


def storage_record(value: Any) -> dict[str, Any]:
    """Select bounded physical storage without changing legacy codec capacity."""
    return _record_transport(value, physical=True)


def canonical_record(value: Any, *, use_memo: bool = True) -> bytes:
    """Encode the physical record, retaining legacy bytes below transport bounds."""
    return _canonical_owned_record(value, use_memo=use_memo)[0]


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
