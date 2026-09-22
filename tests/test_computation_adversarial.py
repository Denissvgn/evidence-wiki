"""Bounded hostile declarations, replay tampering and concurrent-input changes."""

import base64
import json
import shutil
from pathlib import Path

import pytest
import yaml

from evidence_wiki.computation import evaluate, execute
from evidence_wiki.errors import EvidenceWikiError
from tests._computation_fixture import aggregation, definition, graph, workspace
from tests._script_loader import load_isolated_module
from tests.test_computation_effects import EFFECTS, prepared

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mutation", ["cycle", "table_gap", "table_reverse", "shorthand", "traversal", "overlap", "template", "unknown", "large_precision"])
def test_invalid_declarations_refuse_before_effects(tmp_path, mutation):
    data = definition()
    data["graphs"]["worksheet"] = graph()
    if mutation == "cycle":
        data["graphs"]["worksheet"]["inputs"]["amount"] = {"kind": "graph", "id": "worksheet", "output": "total"}
    elif mutation.startswith("table"):
        data["tables"]["rates"] = [{"lower": "0", "upper": "10", "value": "1"},
                                   {"lower": "11" if mutation == "table_gap" else "10", "upper": "9" if mutation == "table_reverse" else None, "value": "2"}]
    elif mutation == "shorthand":
        data["graphs"]["worksheet"]["nodes"]["scaled"] = "amount * 2"
    elif mutation == "traversal":
        data["graphs"]["worksheet"]["output_page"] = "wiki/outputs/../../raw/data/observations.json"
    elif mutation == "overlap":
        data["graphs"]["worksheet"]["output_page"] = "sources/normalized/data--observations.md"
    elif mutation == "template":
        data["invariants"]["rule"] = {"description": "Rule", "target": "computed", "selector": None, "inputs": {},
            "filter": None, "assertion": "False", "severity": "warning", "failure_message": "{anything.__class__.__mro__}"}
    elif mutation == "unknown":
        data["graphs"]["worksheet"]["nodes"]["scaled"]["unexpected"] = True
    else:
        data["arithmetic"]["precision"] = 10_000_000
    workspace(tmp_path, data)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(EvidenceWikiError):
        execute(tmp_path, "write", expected_result_id="sha256:" + "0" * 64, request_id="invalid")
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_escaped_duplicate_keys_and_yaml_aliases_are_not_a_downgrade(tmp_path):
    workspace(tmp_path)
    path = tmp_path / "research.yml"
    original = path.read_text()
    duplicated = original.replace("computation:", '"\\u0063omputation":') + '\n"\\u0063omputation": null\n'
    path.write_text(duplicated)
    with pytest.raises(EvidenceWikiError) as caught:
        evaluate(tmp_path)
    assert caught.value.details["reason"] == "computation_yaml_duplicate_key"
    aliased = original.replace("group_by: []", "group_by: &same []").replace("tables: {}", "tables: {a: *same}")
    path.write_text(aliased)
    with pytest.raises(EvidenceWikiError):
        evaluate(tmp_path)


@pytest.mark.parametrize("kind", ["records", "groups"])
def test_excess_records_or_groups_never_return_a_truncated_total(tmp_path, kind):
    data = definition()
    count = 2049 if kind == "records" else 129
    if kind == "groups":
        data["aggregations"]["observations"] = aggregation(groups=["record.id"])
    raw = json.dumps({"records": [{"id": str(index), "amount": 1} for index in range(count)]}).encode()
    workspace(tmp_path, data, raw)
    with pytest.raises(EvidenceWikiError):
        evaluate(tmp_path)


def test_retained_runtime_refuses_replaced_checker_bytes(tmp_path):
    workspace(tmp_path)
    scripts = tmp_path / "scripts"
    shutil.copytree(ROOT / "workspace-template/scripts", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    module = load_isolated_module("retained_computation", scripts / "_computation_runtime.py")
    before = module.evaluate(tmp_path)
    path = scripts / "_computation_expression.py"
    path.write_text(path.read_text() + "\n# Updated implementation identity.\n")
    with pytest.raises(ValueError, match="computation_engine_changed"):
        module.evaluate(tmp_path)
    updated = load_isolated_module("updated_computation", scripts / "_computation_runtime.py").evaluate(tmp_path)
    assert updated["engine_id"] != before["engine_id"]


def test_changed_configuration_invalidates_an_action_even_when_values_agree(tmp_path):
    result, _destination = prepared(tmp_path)
    path = tmp_path / "research.yml"
    config = yaml.safe_load(path.read_text())
    config["project"]["owner_goal"] = "A changed scope."
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(EvidenceWikiError) as caught:
        execute(tmp_path, "write", expected_result_id=result["result_id"], request_id="stale")
    assert caught.value.details["reason"] == "computation_result_changed"


def test_input_change_at_publication_boundary_preserves_output_and_pending_state(tmp_path, monkeypatch):
    result, destination = prepared(tmp_path)
    original_bytes = destination.read_bytes()
    publisher = EFFECTS.sibling("_usage_materialization")
    original = publisher.publish_file
    def race(root, relative, *args, **kwargs):
        if relative == "wiki/outputs/totals.md":
            raw = root / "raw/data/observations.json"
            raw.write_bytes(raw.read_bytes() + b"\n")
        return original(root, relative, *args, **kwargs)
    monkeypatch.setattr(publisher, "publish_file", race)
    with pytest.raises(ValueError, match="computation_inputs_changed"):
        EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="race")
    assert destination.read_bytes() == original_bytes
    assert EFFECTS.pending(tmp_path) == ["race"]


def test_linked_destination_cannot_overwrite_an_outside_file(tmp_path):
    result, destination = prepared(tmp_path / "workspace")
    outside = tmp_path / "private.txt"
    outside.write_text("preserve")
    destination.unlink()
    destination.symlink_to(outside)
    with pytest.raises(EvidenceWikiError):
        execute(tmp_path / "workspace", "write", expected_result_id=result["result_id"], request_id="linked")
    assert outside.read_text() == "preserve"


def warning_workspace(root):
    data = definition()
    data["invariants"]["nonnegative"] = {"description": "Nonnegative", "target": "records", "selector": aggregation()["selector"],
        "inputs": {}, "filter": None, "assertion": "record.amount >= 0", "severity": "warning", "failure_message": "Negative {record.id}"}
    workspace(root, data, b'{"records":[{"id":"a","amount":-1}]}')
    return EFFECTS.evaluate(root)


@pytest.mark.parametrize("tamper", [False, True])
def test_warning_replay_reproduces_intake_and_refuses_forged_content(tmp_path, monkeypatch, tamper):
    result = warning_workspace(tmp_path)
    publisher = EFFECTS.sibling("_usage_materialization")
    original = publisher.publish_file
    def interrupted(root, relative, *args, **kwargs):
        if relative.startswith("wiki/questions/"):
            raise OSError("interrupted intake")
        return original(root, relative, *args, **kwargs)
    monkeypatch.setattr(publisher, "publish_file", interrupted)
    with pytest.raises(OSError):
        EFFECTS.apply(tmp_path, "apply-warnings", expected_result_id=result["result_id"], request_id="warning")
    monkeypatch.setattr(publisher, "publish_file", original)
    if tamper:
        path = tmp_path / EFFECTS.STATE_PATH
        state = json.loads(path.read_text())
        for write in state["requests"]["warning"]["writes"]:
            if write["path"].startswith("wiki/questions/"):
                raw = base64.b64decode(write["content"]) + b"\nForged instruction outside the finding.\n"
                write.update(content=base64.b64encode(raw).decode(), sha256=EFFECTS.digest(raw))
        path.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="computation_intake_not_reproduced"):
            EFFECTS.apply(tmp_path, "apply-warnings", expected_result_id=result["result_id"], request_id="warning")
    else:
        EFFECTS.apply(tmp_path, "apply-warnings", expected_result_id=result["result_id"], request_id="warning")
        assert len(list((tmp_path / "wiki/questions").glob('*.md'))) == 1
        assert EFFECTS.pending(tmp_path) == []


def test_completed_write_replay_does_not_certify_an_edited_output(tmp_path):
    result, destination = prepared(tmp_path)
    EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output")
    destination.write_text(destination.read_text().replace("0.3", "999"))
    with pytest.raises(ValueError):
        EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output")


def test_new_markdown_uses_configured_page_metadata(tmp_path):
    result, destination = prepared(tmp_path)
    destination.unlink()
    result = EFFECTS.evaluate(tmp_path)
    EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="new")
    metadata = yaml.safe_load(destination.read_text().split("---", 2)[1])
    assert metadata["type"] == "output" and metadata["source_ids"] == ["data:observations"]
    assert "Source: data:observations" in destination.read_text()


def test_warning_recovery_cannot_restore_forged_control_files(tmp_path, monkeypatch):
    result = warning_workspace(tmp_path)
    publisher = EFFECTS.sibling("_usage_materialization")
    original = publisher.publish_file
    def interrupted(root, relative, *args, **kwargs):
        if relative.startswith("wiki/questions/"):
            raise OSError("interrupted")
        return original(root, relative, *args, **kwargs)
    monkeypatch.setattr(publisher, "publish_file", interrupted)
    with pytest.raises(OSError):
        EFFECTS.apply(tmp_path, "apply-warnings", expected_result_id=result["result_id"], request_id="warning")
    monkeypatch.setattr(publisher, "publish_file", original)
    actual = (tmp_path / "research.yml").read_bytes()
    fake = actual + b"\nproject: {name: forged}\n"
    state_path = tmp_path / EFFECTS.STATE_PATH
    state = json.loads(state_path.read_text())
    state["requests"]["warning"]["writes"].append({"path": "research.yml", "expected": EFFECTS.digest(fake),
        "before": base64.b64encode(fake).decode(), "content": base64.b64encode(actual).decode(), "sha256": EFFECTS.digest(actual)})
    state_path.write_text(json.dumps(state))
    def no_intake(*args, **kwargs):
        raise AssertionError("unsafe restored configuration reached intake")
    monkeypatch.setattr(EFFECTS.sibling("intake_questions"), "run_intake_document", no_intake)
    with pytest.raises(ValueError, match="computation_intake_path_invalid"):
        EFFECTS.apply(tmp_path, "apply-warnings", expected_result_id=result["result_id"], request_id="warning")
    assert (tmp_path / "research.yml").read_bytes() == actual
