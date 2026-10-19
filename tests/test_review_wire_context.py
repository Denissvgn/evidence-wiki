"""Explicit blinded transport decoding preserves ordinary wrapper bounds."""

import pytest

from evidence_wiki._review_dependency_wire import WIRE
from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.review_packet_contracts import schemas
from evidence_wiki.review_wire import decode_dependency_material


def test_plain_child_is_detached_and_retains_execution_budget():
    child = {"label": "café", "values": [-0.0, True, None]}
    decoded = decode_dependency_material(child)
    assert decoded == child and decoded is not child
    decoded["values"].append("changed")
    assert len(child["values"]) == 3
    with pytest.raises(ExecutionError):
        decode_dependency_material({"values": [None] * 16384})


@pytest.mark.parametrize("kind", ["blinded_material", "native_snapshot", "native_envelope", "journal_data", "unknown"])
def test_incomplete_or_wrong_kind_wire_cannot_be_read_as_plain_material(kind):
    with pytest.raises(ValueError):
        decode_dependency_material({"schema_version": WIRE, "kind": kind})


def test_dependency_schema_selects_only_blinded_wire_without_changing_legacy_context():
    documents = schemas()
    current = documents["research-blinded-review-input/v3"]
    material = current["$defs"]["dependency_material"]
    native, wire = material["oneOf"]
    assert native["properties"]["schema_version"]["const"] == "research-dependency-review-material/v1"
    assert wire["properties"]["schema_version"] == {"const": WIRE}
    assert wire["properties"]["kind"] == {"const": "blinded_material"}
    for version in (1, 2):
        context = documents[f"research-blinded-review-input/v{version}"]["properties"]["packet"]
        assert "dependency_material" not in context["properties"]


def test_dependency_computation_schema_accepts_real_projected_decimal(tmp_path):
    from evidence_wiki.calculation_context import project
    from evidence_wiki.pack_discovery import owner
    from tests._computation_fixture import definition, graph, workspace

    declaration = definition()
    declaration["graphs"]["worksheet"] = graph()
    workspace(tmp_path, declaration)
    result = owner("_computation_runtime").evaluate(tmp_path)
    projected = project(result, {row["source_id"]: f"source-{index + 1}"
        for index, row in enumerate(result["sources"])})
    total = projected["aggregations"]["observations"]["groups"][0]["metrics"]["total"]
    assert total["type"] == "decimal" and total["value"] == "0.3"
    document = schemas()["research-blinded-review-input/v3"]
    computation = {"$defs": document["$defs"],
        **document["properties"]["packet"]["properties"]["computation"]}
    contract = owner("_review_dependency_contract")
    assert contract.validate_schema(projected, computation) == projected
    total["value"] = {"unexpected": "object"}
    with pytest.raises(ValueError):
        contract.validate_schema(projected, computation)


def test_computation_interning_preserves_keyword_named_properties_and_literal_data():
    from copy import deepcopy

    from evidence_wiki.pack_discovery import owner
    from evidence_wiki.review_packet_contracts import _shared_computation

    literal = {"type": "object", "properties": {"type": {"const": "retained semantic value"}}}
    child = {"type": "object", "properties": {"type": {"const": "decimal"}, "value": {"type": "string"}},
        "required": ["type", "value"], "additionalProperties": False}
    shape = {"type": "object", "properties": {f"output{index}": deepcopy(child) for index in range(8)}}
    shape["properties"]["literal"] = {"const": deepcopy(literal), "default": deepcopy(literal),
        "examples": [deepcopy(literal)], "enum": [deepcopy(literal)]}
    definitions = {}
    shared = _shared_computation(shape, definitions)
    assert definitions
    assert shared["properties"]["literal"] == shape["properties"]["literal"]
    value = {f"output{index}": {"type": "decimal", "value": "0.3"} for index in range(8)}
    value["literal"] = literal
    assert owner("_review_dependency_contract").validate_schema(value, {"$defs": definitions, **shared}) == value


@pytest.mark.parametrize("keyword", ["properties", "$defs", "patternProperties", "dependentSchemas"])
def test_computation_interning_preserves_schema_map_names(keyword):
    from copy import deepcopy

    from evidence_wiki.review_packet_contracts import _shared_computation

    child = {"type": "object", "properties": {"type": {"const": "decimal"}, "value": {"type": "string"}},
        "required": ["type", "value"], "additionalProperties": False}
    shape = {"type": "object", keyword: {name: deepcopy(child) for name in ("type", "enum", "const", "items", "other")}}
    definitions = {}
    shared = _shared_computation(shape, definitions)
    assert definitions
    assert set(shared[keyword]) == set(shape[keyword])
