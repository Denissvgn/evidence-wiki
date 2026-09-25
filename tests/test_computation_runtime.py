"""Exercise actual record capture and deterministic computation boundaries."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from tests._computation_fixture import aggregation, definition, graph, metric, reference, workspace
from tests._script_loader import load_isolated_module

CORE = load_isolated_module("computation_runtime", Path(__file__).resolve().parents[1] / "workspace-template/scripts/_computation_runtime.py")


def test_lossless_record_aggregation_and_graph_trace(tmp_path):
    data = definition()
    data["graphs"]["worksheet"] = graph()
    workspace(tmp_path, data)
    result = CORE.evaluate(tmp_path)
    assert len(result["aggregations"]["observations"]["groups"]) == 1
    assert result["aggregations"]["observations"]["groups"][0]["metrics"]["total"]["value"] == "0.3"
    output = result["graphs"]["worksheet"]["outputs"]["total"]
    assert output["value"] == "0.375"
    assert {result["lineage"][key]["kind"] for key in output["lineage"]} == {"record", "field", "constant", "rule"}
    assert CORE.evaluate(tmp_path) == result


def test_explicit_group_reference_and_ratio_of_sums(tmp_path):
    data = definition()
    data["aggregations"]["observations"] = aggregation(groups=["record.category"], metrics={
        "total": metric(), "ratio": metric("ratio", "record.amount", "record.weight"), "count": metric("count", None)})
    data["graphs"]["worksheet"] = graph()
    data["graphs"]["worksheet"]["inputs"]["amount"] = reference(group=["x"])
    workspace(tmp_path, data, b'{"records":[{"id":"a","category":"x","amount":10,"weight":2},{"id":"b","category":"x","amount":20,"weight":4},{"id":"c","category":"y","amount":7,"weight":1}]}')
    result = CORE.evaluate(tmp_path)
    groups = result["aggregations"]["observations"]["groups"]
    assert [row["key"] for row in groups] == [["x"], ["y"]]
    assert groups[0]["metrics"]["ratio"]["value"] == "5"
    assert groups[0]["metrics"]["count"]["value"] == "2"
    assert result["graphs"]["worksheet"]["outputs"]["total"]["value"] == "37.5"


@pytest.mark.parametrize("change", ["sidecar", "orphan", "duplicate", "missing", "cycle", "group", "null", "float"])
def test_incomplete_or_changed_inputs_refuse(tmp_path, change):
    data = definition()
    data["graphs"]["worksheet"] = graph()
    raw = b'{"records":[{"id":"a","amount":1},{"id":"b","amount":2}]}'
    if change == "duplicate":
        raw = raw.replace(b'"b"', b'"a"')
    if change == "missing":
        raw = raw.replace(b'"amount":1', b'"other":1')
    if change == "null":
        raw = raw.replace(b'"amount":1', b'"amount":null')
    if change == "cycle":
        data["graphs"]["worksheet"]["nodes"]["scaled"]["expr"] = "scaled + 1"
    if change == "group":
        data["aggregations"]["observations"] = aggregation(groups=["record.id"])
    if change == "float":
        data["graphs"]["worksheet"]["constants"]["factor"]["value"] = 1.25
    workspace(tmp_path, data, raw)
    if change == "sidecar":
        (tmp_path / "sources/normalized/data--observations.structured.json").write_bytes(b'{}')
    if change == "orphan":
        (tmp_path / "sources/manifest.jsonl").unlink()
    with pytest.raises(ValueError):
        CORE.evaluate(tmp_path)


def test_invariants_return_findings_without_writing(tmp_path):
    data = definition()
    data["invariants"]["nonnegative"] = {"description": "Nonnegative observations", "target": "records",
        "selector": aggregation()["selector"], "inputs": {}, "filter": None, "assertion": "record.amount >= 0",
        "severity": "warning", "failure_message": "Negative observation {record.id}: {record.amount}"}
    workspace(tmp_path, data, b'{"records":[{"id":"a","amount":-1}]}')
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = CORE.evaluate(tmp_path)
    assert result["status"] == "passed"
    assert result["findings"][0]["message"] == "Negative observation a: -1"
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    config = yaml.safe_load((tmp_path / "research.yml").read_text())
    config["computation"]["invariants"]["nonnegative"]["severity"] = "error"
    (tmp_path / "research.yml").write_text(yaml.safe_dump(config))
    assert CORE.evaluate(tmp_path)["status"] == "failed"


def test_declaration_order_does_not_change_identity(tmp_path):
    data = definition()
    data["graphs"]["worksheet"] = graph()
    config = workspace(tmp_path, data)
    expected = CORE.evaluate(tmp_path)
    reordered = copy.deepcopy(config)
    reordered["computation"] = dict(reversed(list(reordered["computation"].items())))
    (tmp_path / "research.yml").write_text(yaml.safe_dump(reordered, sort_keys=False))
    assert CORE.evaluate(tmp_path) == expected


def test_matching_manifest_records_cannot_disappear_from_a_total(tmp_path):
    workspace(tmp_path)
    path = tmp_path / "sources/manifest.jsonl"
    missing = {"id": "data:missing", "kind": "structured_data", "status": "normalized", "raw_paths": ["raw/data/missing.json"]}
    path.write_text(path.read_text() + json.dumps(missing) + "\n")
    (tmp_path / "raw/data/missing.json").write_text('{"records": []}')
    with pytest.raises(ValueError, match="computation_required_record_missing"):
        CORE.evaluate(tmp_path)


def test_raw_revision_changes_invalidate_the_result_identity(tmp_path):
    workspace(tmp_path)
    before = CORE.evaluate(tmp_path)
    raw = tmp_path / "raw/data/observations.json"
    raw.write_bytes(raw.read_bytes() + b"\n")
    after = CORE.evaluate(tmp_path)
    assert after["result_id"] != before["result_id"] and after["input_id"] != before["input_id"]
