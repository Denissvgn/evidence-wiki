"""Native node dispatch preserves structural validation and scope boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from threading import Event

import pytest

from tests.test_review_dependency_contract import contract as contract


@pytest.fixture
def native_schema(contract):
    return {"$defs": contract.schema_documents()[contract.MATERIAL]["$defs"],
            "$ref": "#/$defs/node"}


@pytest.mark.parametrize("node", [
    {"t": "null"}, {"t": "boolean", "v": True}, {"t": "integer", "v": "12"},
    {"t": "float", "v": "0x1.0000000000000p+0"}, {"t": "string", "v": "x"},
    {"t": "date", "v": "2026-10-16"},
    {"t": "timestamp", "v": "2026-10-16T12:00:00+00:00"},
    {"t": "list", "v": [{"t": "null"}]},
    {"t": "mapping", "v": [["x", {"t": "null"}]]},
    {"t": "unknown"}, {"t": 1}, {"t": "boolean", "v": 1},
    {"t": "null", "v": None}, {"t": "integer", "v": "01"},
    {"t": "date", "v": "2026-02-30"}, {"t": "timestamp", "v": "invalid"},
    {"t": "timestamp", "v": True}, {"t": []}, {"t": None},
])
def test_native_dispatch_matches_ordinary(contract, native_schema, monkeypatch, node):
    def result():
        contract._SCHEMA_SUCCESSES.clear()
        try:
            return (True, contract.validate_schema(node, native_schema))
        except ValueError as error:
            return (False, str(error))
    accelerated = result()
    monkeypatch.setattr(contract, "_native_node_choices", lambda schema: None)
    assert result() == accelerated


def test_overlapping_generic_union_still_rejects(contract):
    branch = {"type": "object", "properties": {"t": {"const": "x"}},
              "required": ["t"], "additionalProperties": False}
    with pytest.raises(ValueError):
        contract.validate_schema({"t": "x"}, {"oneOf": [branch, deepcopy(branch)]})


def test_owned_dispatch_scope_expires_in_copied_context(contract, native_schema, monkeypatch):
    original = contract._native_node_choices
    contexts = []
    def inspect(schema):
        result = original(schema)
        if result is not None:
            contexts.append((copy_context(), schema))
        return result
    monkeypatch.setattr(contract, "_native_node_choices", inspect)
    contract._SCHEMA_SUCCESSES.clear()
    contract.validate_schema({"t": "null"}, native_schema)
    assert contexts
    assert all(context.run(original, schema) is None for context, schema in contexts)
    assert contract._NODE_DISPATCH.get() is None
    assert contract._SCHEMA_DOCUMENTS.get() is None


def test_alias_input_never_enables_dispatch(contract, native_schema, monkeypatch):
    child = {"t": "null"}
    original = contract._native_node_choices
    def inspect(schema):
        assert contract._NODE_DISPATCH.get() is None
        return original(schema)
    monkeypatch.setattr(contract, "_native_node_choices", inspect)
    contract.validate_schema({"t": "list", "v": [child, child]}, native_schema)


def test_dispatch_identity_invalidates_success_key(contract, native_schema, monkeypatch):
    raw, _ = contract._exact_json_bytes({"t": "null"})
    before = contract._schema_memo_key(raw, native_schema)
    original = contract._native_node_choices
    monkeypatch.setattr(contract, "_native_node_choices", lambda schema: original(schema))
    assert contract._schema_memo_key(raw, native_schema) != before


def test_malformed_native_generation_preserves_sibling_type_error(contract, monkeypatch):
    documents = contract.schema_documents()
    node = documents[contract.MATERIAL]["$defs"]["node"]
    node["oneOf"][1]["required"] = ["t", []]
    monkeypatch.setattr(contract._shapes(), "DOCUMENTS", documents)
    schema = {"oneOf": deepcopy(node["oneOf"])}
    contract._SCHEMA_SUCCESSES.clear()
    with pytest.raises(TypeError):
        contract.validate_schema({"t": "null"}, schema)
    monkeypatch.setattr(contract, "_native_node_choices", lambda schema: None)
    with pytest.raises(TypeError):
        contract.validate_schema({"t": "null"}, schema)


@pytest.mark.parametrize("change", ["bounds", "extra_tag", "duplicate_tag"])
def test_changed_native_generation_uses_ordinary_union(contract, monkeypatch, change):
    documents = contract.schema_documents()
    node = documents[contract.MATERIAL]["$defs"]["node"]
    if change == "bounds":
        node["oneOf"][4]["properties"]["v"]["maxLength"] = 7
    elif change == "extra_tag":
        branch = deepcopy(node["oneOf"][0])
        branch["properties"]["t"]["const"] = "future"
        node["oneOf"].append(branch)
    else:
        node["oneOf"].append(deepcopy(node["oneOf"][0]))
    monkeypatch.setattr(contract._shapes(), "DOCUMENTS", documents)
    schema = deepcopy(node)
    original = contract._native_node_choices
    decisions = []
    def inspect(candidate):
        result = original(candidate)
        decisions.append(result)
        return result
    monkeypatch.setattr(contract, "_native_node_choices", inspect)
    contract._SCHEMA_SUCCESSES.clear()
    try:
        accelerated = (True, contract.validate_schema({"t": "null"}, schema))
    except ValueError as error:
        accelerated = (False, str(error))
    assert decisions and all(item is None for item in decisions)
    monkeypatch.setattr(contract, "_native_node_choices", lambda schema: None)
    contract._SCHEMA_SUCCESSES.clear()
    try:
        ordinary = (True, contract.validate_schema({"t": "null"}, schema))
    except ValueError as error:
        ordinary = (False, str(error))
    assert ordinary == accelerated


@pytest.mark.parametrize("depth", [16, 17])
def test_dispatch_preserves_typed_depth_bounds(contract, native_schema, monkeypatch, depth):
    node = {"t": "null"}
    for _ in range(depth - 1):
        node = {"t": "list", "v": [node]}
    def outcome():
        contract._SCHEMA_SUCCESSES.clear()
        try:
            return True, contract.validate_schema(node, native_schema)
        except ValueError as error:
            return False, str(error)
    dispatched = outcome()
    assert dispatched[0] is (depth == 16)
    monkeypatch.setattr(contract, "_native_node_choices", lambda schema: None)
    assert outcome() == dispatched


def test_dispatch_does_not_relax_parent_bound(contract, native_schema, monkeypatch):
    schema, value = {"$ref": "#/$defs/node"}, {"t": "null"}
    for _ in range(17):
        schema = {"type": "object", "properties": {"child": schema}}
        value = {"child": value}
    schema["$defs"] = native_schema["$defs"]
    for ordinary in [False, True]:
        if ordinary:
            monkeypatch.setattr(contract, "_native_node_choices", lambda schema: None)
        contract._SCHEMA_SUCCESSES.clear()
        with pytest.raises(ValueError, match="bound"):
            contract.validate_schema(value, schema)


def test_nested_validation_restores_outer_dispatch_after_failure(contract, native_schema, monkeypatch):
    original = contract._native_node_choices
    inspected = []
    def inspect(schema):
        if not inspected:
            inspected.append(True)
            outer = contract._NODE_DISPATCH.get()
            with pytest.raises(ValueError):
                contract.validate_schema(None, False)
            assert contract._NODE_DISPATCH.get() is outer
        return original(schema)
    monkeypatch.setattr(contract, "_native_node_choices", inspect)
    contract._SCHEMA_SUCCESSES.clear()
    assert contract.validate_schema({"t": "null"}, native_schema) == {"t": "null"}
    assert inspected and contract._NODE_DISPATCH.get() is None


def test_dispatch_classifies_detached_schema_during_caller_mutation(contract, native_schema, monkeypatch):
    entered, resume = Event(), Event()
    original = contract._native_node_choices
    def inspect(schema):
        entered.set()
        assert resume.wait(5)
        return original(schema)
    monkeypatch.setattr(contract, "_native_node_choices", inspect)
    contract._SCHEMA_SUCCESSES.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(contract.validate_schema, {"t": "null"}, native_schema)
        try:
            assert entered.wait(5)
            native_schema["$defs"]["node"]["oneOf"][1]["required"] = ["t", []]
        finally:
            resume.set()
        assert future.result(timeout=5) == {"t": "null"}


def test_audited_fingerprint_value_participates_in_success_key(contract, native_schema, monkeypatch):
    raw, _ = contract._exact_json_bytes({"t": "null"})
    before = contract._schema_memo_key(raw, native_schema)
    monkeypatch.setattr(contract, "_NATIVE_NODE_FINGERPRINT", "0" * 64)
    assert contract._schema_memo_key(raw, native_schema) != before


def test_non_json_schema_retains_ordinary_dispatch_path(contract, native_schema, monkeypatch):
    native_schema["ignored_extension"] = {"non_json"}
    original = contract._native_node_choices
    inspected = []
    def inspect(schema):
        inspected.append(True)
        assert contract._NODE_DISPATCH.get() is None
        return original(schema)
    monkeypatch.setattr(contract, "_native_node_choices", inspect)
    assert contract.validate_schema({"t": "null"}, native_schema) == {"t": "null"}
    assert inspected
