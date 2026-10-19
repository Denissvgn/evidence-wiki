"""Native material owns reversible JSON values, independent of Python aliases."""

from datetime import date, datetime, timezone

import pytest

from evidence_wiki.pack_discovery import owner


def test_material_detachment_preserves_exact_bytes_types_and_dependency_identities():
    contract = owner("_review_dependency_contract")
    questions = owner("_question_dependencies")
    source = {"id": "source", "metadata": questions.typed_node({
        "date": date(2026, 1, 2), "at": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "label": "café\n海", "flag": True, "count": 1, "value": -0.0}),
        "ratio": -0.0}
    value = {"global": {"sources": [source]}, "local": {"sources": [source]}}
    raw, unaliased = contract._exact_json_bytes(value)
    assert not unaliased
    result = contract.detached_json(value)
    assert contract.canonical_bytes(result) == raw
    assert contract.content_id(contract.MATERIAL, result) == contract.content_id(contract.MATERIAL, value)
    assert contract._exact_json_bytes(result)[1]
    assert result["global"]["sources"][0] is not result["local"]["sources"][0]
    assert type(questions.typed_value(result["local"]["sources"][0]["metadata"])["flag"]) is bool
    assert type(questions.typed_value(result["local"]["sources"][0]["metadata"])["count"]) is int
    result["global"]["sources"][0]["id"] = "changed"
    assert result["local"]["sources"][0]["id"] == source["id"] == "source"


@pytest.mark.parametrize("value", [(1,), {1: "coerced"}, float("nan"), float("inf"), b"bytes"])
def test_material_detachment_refuses_json_coercions(value):
    with pytest.raises(ValueError):
        owner("_review_dependency_contract").detached_json(value)


def test_material_detachment_rejects_cycles_and_excessive_bytes():
    contract = owner("_review_dependency_contract")
    value = []
    value.append(value)
    with pytest.raises(ValueError, match="cycle"):
        contract.detached_json(value)
    with pytest.raises(ValueError, match="bytes_bound"):
        contract.detached_json("x" * (contract.MAX_BYTES + 1))


def test_detached_material_cannot_borrow_typed_depth_from_a_shared_container():
    contract = owner("_review_dependency_contract")
    schema = {"type": "object", "properties": {
        "typed": {"$ref": "#/$defs/node"}, "other": True},
        "$defs": contract.schema_documents()[contract.MATERIAL]["$defs"]}
    node = {"t": "boolean", "v": True}
    other = node
    for _ in range(15):
        other = [other]
    value = {"typed": node, "other": other}
    assert contract.validate_schema(value, schema) == value
    with pytest.raises(ValueError, match="bound"):
        contract.validate_schema(contract.detached_json(value), schema)
