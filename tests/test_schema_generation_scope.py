"""Validation-local native document snapshots retain exact identity boundaries."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from types import ModuleType

import pytest

from evidence_wiki.pack_discovery import owner


@pytest.fixture
def contract(monkeypatch):
    module = owner("_review_dependency_contract")
    monkeypatch.setattr(module, "_SCHEMA_CACHE_MIN_BYTES", 0)
    return module


def native_documents():
    module = ModuleType("local_native_shapes")
    module.DOCUMENTS = {"child": {"$id": "native-child", "type": "object",
                                "additionalProperties": True}}
    return module


@pytest.mark.parametrize("count", [1, 400])
def test_native_child_count_does_not_reload_generation(contract, monkeypatch, count):
    native = native_documents()
    calls = []
    monkeypatch.setattr(contract, "_shapes", lambda: (calls.append(None), native)[1])
    contract._SCHEMA_SUCCESSES.clear()
    schema = {"type": "array", "items": deepcopy(native.DOCUMENTS["child"])}
    value = [{} for _ in range(count)]
    contract.validate_schema(value, schema)
    assert len(calls) == 2  # Entry identity and fresh admission check.
    calls.clear()
    contract.validate_schema(value, schema)
    assert len(calls) == 1
    calls.clear()
    child = {}
    contract.validate_schema([child, child], schema)
    assert len(calls) == 1  # Aliased values never use the success memo.


def test_snapshot_uses_key_bytes_and_copied_context_expires(contract, monkeypatch):
    native = native_documents()
    monkeypatch.setattr(contract, "_shapes", lambda: native)
    contract._SCHEMA_SUCCESSES.clear()
    expected = deepcopy(native.DOCUMENTS)
    observed = []
    contexts = []
    shape = contract._shape

    def inspect(value, schema, root, depth=0):
        if depth == 0:
            native.DOCUMENTS["child"]["$id"] = "changed-generation"
            observed.append(deepcopy(contract._shape_documents()))
            contexts.append(copy_context())
        return shape(value, schema, root, depth)

    monkeypatch.setattr(contract, "_shape", inspect)
    contract.validate_schema({}, expected["child"])
    assert observed == [expected]
    assert not contract._SCHEMA_SUCCESSES
    assert contract._SCHEMA_DOCUMENTS.get() is None
    assert contexts[0].run(contract._shape_documents) is native.DOCUMENTS


def test_nested_and_thread_validation_install_independent_snapshots(contract, monkeypatch):
    native = native_documents()
    monkeypatch.setattr(contract, "_shapes", lambda: native)
    contract._SCHEMA_SUCCESSES.clear()
    shape = contract._shape
    seen = []

    def inspect(value, schema, root, depth=0):
        if depth == 0 and schema == {"const": "outer"}:
            outer_scope = contract._SCHEMA_DOCUMENTS.get()
            native.DOCUMENTS["child"]["$id"] = "inner-generation"
            contract.validate_schema("inner", {"const": "inner"})
            assert contract._SCHEMA_DOCUMENTS.get() is outer_scope
            assert contract._shape_documents()["child"]["$id"] == "native-child"
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(contract.validate_schema, "thread", {"const": "thread"}).result()
            assert contract._SCHEMA_DOCUMENTS.get() is outer_scope
        elif depth == 0 and schema in ({"const": "inner"}, {"const": "thread"}):
            seen.append(contract._shape_documents()["child"]["$id"])
        return shape(value, schema, root, depth)

    monkeypatch.setattr(contract, "_shape", inspect)
    contract.validate_schema("outer", {"const": "outer"})
    assert seen == ["inner-generation", "inner-generation"]
    assert contract._SCHEMA_DOCUMENTS.get() is None


def test_failed_validation_expires_copied_scope_and_restores_parent(contract, monkeypatch):
    native = native_documents()
    monkeypatch.setattr(contract, "_shapes", lambda: native)
    contexts = []
    shape = contract._shape

    def inspect(*args, **kwargs):
        contexts.append(copy_context())
        return shape(*args, **kwargs)

    monkeypatch.setattr(contract, "_shape", inspect)
    parent = ({"parent": {}}, [True])
    token = contract._SCHEMA_DOCUMENTS.set(parent)
    try:
        with pytest.raises(ValueError):
            contract.validate_schema(False, {"const": True})
        assert contract._SCHEMA_DOCUMENTS.get() is parent
        assert parent[1] == [True]
        assert contexts[0].run(contract._shape_documents) is native.DOCUMENTS
    finally:
        contract._SCHEMA_DOCUMENTS.reset(token)


def test_private_shape_without_validation_scope_uses_fresh_generation(contract, monkeypatch):
    native = native_documents()
    calls = []
    monkeypatch.setattr(contract, "_shapes", lambda: (calls.append(None), native)[1])
    children = set()
    token = contract._CHILD_ROOTS.set(children)
    value = {}
    try:
        contract._shape(value, deepcopy(native.DOCUMENTS["child"]), {})
        assert id(value) in children
        assert len(calls) == 1
    finally:
        contract._CHILD_ROOTS.reset(token)


def test_native_documents_helper_replacement_invalidates_success(contract, monkeypatch):
    native = native_documents()
    monkeypatch.setattr(contract, "_shapes", lambda: native)
    contract._SCHEMA_SUCCESSES.clear()
    schema = deepcopy(native.DOCUMENTS["child"])
    contract.validate_schema({}, schema)
    assert contract._SCHEMA_SUCCESSES

    def refuse():
        raise ValueError("replacement native classification")

    monkeypatch.setattr(contract, "_shape_documents", refuse)
    with pytest.raises(ValueError, match="replacement native classification"):
        contract.validate_schema({}, schema)
    assert contract._SCHEMA_DOCUMENTS.get() is None
