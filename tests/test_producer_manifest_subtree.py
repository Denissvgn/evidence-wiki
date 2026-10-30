"""Native manifest reuse preserves surrounding structural validation."""

from copy import deepcopy
from types import ModuleType

import pytest

from evidence_wiki.pack_discovery import owner


@pytest.fixture
def manifest_contract(monkeypatch):
    contract = owner("_review_dependency_contract")
    monkeypatch.setattr(contract, "_SCHEMA_CACHE_MIN_BYTES", 0)
    contract._SCHEMA_SUCCESSES.clear()
    schema = contract.schema_documents()["evidence-review-producer-manifest/v1"]
    value = {"schema_version": "evidence-review-producer-manifest/v1", "members": [
        {"path": "scripts/producer.py", "size_bytes": 1, "sha256": "sha256:" + "a" * 64}]}
    return contract, schema, value


def _parent(schema):
    return {"type": "object", "properties": {"manifest": schema, "sibling": True}}


def _member_walks(contract, monkeypatch):
    calls = []
    original = contract._shape

    def observed(value, schema, root, depth=0):
        if type(schema) is dict and schema.get("required") == ["path", "size_bytes", "sha256"]:
            calls.append(None)
        return original(value, schema, root, depth)

    monkeypatch.setattr(contract, "_shape", observed)
    return calls


def test_native_manifest_reuse_requires_success_and_exact_value(manifest_contract, monkeypatch):
    contract, schema, value = manifest_contract
    calls = _member_walks(contract, monkeypatch)
    parent = _parent(schema)
    contract.validate_schema({"manifest": value, "sibling": 1}, parent)
    assert len(calls) == 1
    contract.validate_schema({"manifest": deepcopy(value), "sibling": 2}, parent)
    assert len(calls) == 1
    invalid = deepcopy(value)
    invalid["members"][0]["size_bytes"] = True
    for sibling in (3, 4):
        with pytest.raises(ValueError):
            contract.validate_schema({"manifest": invalid, "sibling": sibling}, parent)
    assert len(calls) == 3


def test_native_manifest_reuse_tracks_document_generation(manifest_contract, monkeypatch):
    contract, schema, value = manifest_contract
    calls = _member_walks(contract, monkeypatch)
    parent = _parent(schema)
    contract.validate_schema(value, schema)
    native = ModuleType("replacement_manifest_documents")
    native.DOCUMENTS = contract.schema_documents()
    native.DOCUMENTS[contract.MATERIAL]["$id"] += ":changed"
    monkeypatch.setattr(contract, "_shapes", lambda: native)
    contract.validate_schema({"manifest": value, "sibling": 1}, parent)
    assert len(calls) == 2


def test_actual_material_inline_manifest_reuses_standalone_success(manifest_contract, monkeypatch):
    contract, schema, value = manifest_contract
    material = contract.schema_documents()[contract.MATERIAL]
    inline = material["$defs"]["worker_semantics"]["properties"]["producer"]["properties"]["manifest"]
    assert inline == {key: child for key, child in schema.items() if key not in {"$id", "$schema"}}
    calls = _member_walks(contract, monkeypatch)
    contract.validate_schema(value, schema)
    for sibling in (1, 2):
        contract.validate_schema({"manifest": value, "sibling": sibling}, _parent(inline))
    assert len(calls) == 1

    # Inline schemas retain their original outer depth accounting.
    parent, wrapped = _parent(inline), {"manifest": value}
    for _ in range(contract.MAX_DEPTH - 3):
        parent = {"properties": {"child": parent}}
        wrapped = {"child": wrapped}
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(wrapped, parent)


@pytest.mark.parametrize("change", ["lookalike", "reference", "nested_id"])
def test_manifest_shortcut_excludes_modified_or_scoped_schemas(manifest_contract, monkeypatch, change):
    contract, schema, value = manifest_contract
    native = ModuleType("scoped_manifest_documents")
    native.DOCUMENTS = contract.schema_documents()
    altered = deepcopy(schema)
    if change == "lookalike":
        altered["minProperties"] = 1
    elif change == "reference":
        altered["$defs"] = {"size": {"type": "integer"}}
        altered["properties"]["members"]["items"]["properties"]["size_bytes"] = {"$ref": "#/$defs/size"}
        native.DOCUMENTS["evidence-review-producer-manifest/v1"] = altered
    else:
        altered["properties"]["members"]["items"]["$id"] = "nested-member"
        native.DOCUMENTS["evidence-review-producer-manifest/v1"] = altered
    monkeypatch.setattr(contract, "_shapes", lambda: native)
    calls = _member_walks(contract, monkeypatch)
    contract.validate_schema(value, altered)
    for sibling in (1, 2):
        contract.validate_schema({"manifest": value, "sibling": sibling}, _parent(altered))
    assert len(calls) == 3


@pytest.mark.parametrize("bound", ["nodes", "bytes", "depth"])
def test_warm_manifest_cannot_relax_parent_bounds(manifest_contract, monkeypatch, bound):
    contract, schema, value = manifest_contract
    parent = _parent(schema)
    if bound == "nodes":
        monkeypatch.setattr(contract, "MAX_NODES", 20)
        sibling = [None] * 20
    elif bound == "bytes":
        sibling = ["x" * 65_536] * 16
    else:
        sibling = None
        for _ in range(contract.MAX_DEPTH):
            sibling = [sibling]
    contract.validate_schema(value, schema)
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema({"manifest": value, "sibling": sibling}, parent)


@pytest.mark.parametrize("valid", [False, True])
def test_manifest_delegation_restores_context_and_original_child_identity(manifest_contract, valid):
    contract, schema, value = manifest_contract
    if not valid:
        value["members"][0]["size_bytes"] = True
    parent = _parent(schema)
    typed, children = {123}, set()
    typed_token = contract._TYPED_ROOTS.set(typed)
    children_token = contract._CHILD_ROOTS.set(children)
    documents = contract._SCHEMA_DOCUMENTS.get()
    try:
        if valid:
            contract._shape({"manifest": value, "sibling": value}, parent, parent)
        else:
            with pytest.raises(ValueError):
                contract._shape({"manifest": value}, parent, parent)
        assert contract._TYPED_ROOTS.get() is typed
        assert typed == {123}
        assert contract._CHILD_ROOTS.get() is children
        assert children == {id(value)}
        assert contract._SCHEMA_DOCUMENTS.get() is documents
    finally:
        contract._TYPED_ROOTS.reset(typed_token)
        contract._CHILD_ROOTS.reset(children_token)
