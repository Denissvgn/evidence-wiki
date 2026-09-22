"""Real output ownership, intake and interrupted-effect recovery checks."""

import base64
import json
from pathlib import Path

import pytest

from tests._computation_fixture import aggregation, definition, workspace
from tests._script_loader import load_isolated_module

EFFECTS = load_isolated_module("computation_effects", Path(__file__).resolve().parents[1] / "workspace-template/scripts/_computation_effects.py")


def prepared(tmp_path):
    data = definition()
    data["aggregations"]["observations"] = aggregation()
    data["aggregations"]["observations"]["output_target"] = "wiki/outputs/totals.md"
    workspace(tmp_path, data)
    destination = tmp_path / "wiki/outputs/totals.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("# My notes\n\nPreserve this prose.  \n")
    return EFFECTS.evaluate(tmp_path), destination


def test_dry_run_writes_nothing_and_apply_preserves_prose(tmp_path):
    result, destination = prepared(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    planned = EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output-1", dry_run=True)
    assert planned["dry_run"] is True
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    first = EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output-1")
    assert first["replayed"] is False
    assert destination.read_bytes().startswith(before[Path("wiki/outputs/totals.md")])
    assert "| total | 0.3 | units | False |" in destination.read_text()
    once = destination.read_bytes()
    assert EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output-1")["replayed"] is True
    assert destination.read_bytes() == once


def test_interrupted_output_write_resumes_without_duplicate_blocks(tmp_path, monkeypatch):
    result, destination = prepared(tmp_path)
    publisher = EFFECTS.sibling("_usage_materialization")
    original = publisher.publish_file
    def fail_output(root, relative, *args, **kwargs):
        if relative == "wiki/outputs/totals.md":
            raise OSError("simulated interruption")
        return original(root, relative, *args, **kwargs)
    monkeypatch.setattr(publisher, "publish_file", fail_output)
    with pytest.raises(OSError, match="simulated interruption"):
        EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output-1")
    assert EFFECTS.pending(tmp_path) == ["output-1"]
    monkeypatch.setattr(publisher, "publish_file", original)
    EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output-1")
    assert EFFECTS.pending(tmp_path) == []
    assert destination.read_text().count("### Computed values") == 1


def test_pending_write_bytes_are_recomputed_instead_of_trusting_their_hash(tmp_path, monkeypatch):
    result, _destination = prepared(tmp_path)
    publisher = EFFECTS.sibling("_usage_materialization")
    original = publisher.publish_file
    def interrupt(root, relative, *args, **kwargs):
        if relative.startswith("wiki/"):
            raise OSError("interrupted")
        return original(root, relative, *args, **kwargs)
    monkeypatch.setattr(publisher, "publish_file", interrupt)
    with pytest.raises(OSError):
        EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output-1")
    state_path = tmp_path / EFFECTS.STATE_PATH
    state = json.loads(state_path.read_text())
    for write in state["requests"]["output-1"]["writes"]:
        if write["path"].startswith("wiki/"):
            data = base64.b64decode(write["content"]).replace(b"0.3", b"9000")
            write.update(content=base64.b64encode(data).decode(), sha256=EFFECTS.digest(data))
    state_path.write_text(json.dumps(state))
    monkeypatch.setattr(publisher, "publish_file", original)
    with pytest.raises(ValueError, match="computation_output_not_reproduced"):
        EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="output-1")


def test_edited_generated_block_is_not_overwritten(tmp_path):
    result, destination = prepared(tmp_path)
    EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="first")
    destination.write_text(destination.read_text().replace("0.3", "my edit"))
    before = destination.read_bytes()
    with pytest.raises(ValueError, match="computation_output_block_edited"):
        EFFECTS.apply(tmp_path, "write", expected_result_id=result["result_id"], request_id="second")
    assert destination.read_bytes() == before


def test_due_dispatch_records_only_a_local_action_once(tmp_path):
    data = definition()
    data["cadence"]["scheduled"] = {"description": "Local status", "trigger": {"type": "fixed_date", "at": "2026-09-20"},
        "lead_alerts": [], "action": {"kind": "status_flag", "target": "ready"}}
    workspace(tmp_path, data)
    result = EFFECTS.evaluate(tmp_path)
    first = EFFECTS.apply(tmp_path, "dispatch", expected_result_id=result["result_id"], request_id="dispatch-1", cadence_id="scheduled")
    assert first["occurrence"]["outcome"] == {"flag": "ready", "value": True}
    second = EFFECTS.apply(tmp_path, "dispatch", expected_result_id=result["result_id"], request_id="dispatch-2", cadence_id="scheduled")
    assert second["replayed"] is True
    assert second["occurrence"] == first["occurrence"]
    state = json.loads((tmp_path / EFFECTS.STATE_PATH).read_text())
    assert len(state["occurrences"]) == 1


def test_warning_intake_is_explicit_and_deduplicated(tmp_path):
    data = definition()
    data["invariants"]["nonnegative"] = {"description": "Nonnegative observations", "target": "records", "selector": aggregation()["selector"],
        "inputs": {}, "filter": None, "assertion": "record.amount >= 0", "severity": "warning", "failure_message": "Negative {record.id}"}
    workspace(tmp_path, data, b'{"records":[{"id":"a","amount":-1}]}')
    result = EFFECTS.evaluate(tmp_path)
    assert not (tmp_path / "wiki/questions").exists()
    first = EFFECTS.apply(tmp_path, "apply-warnings", expected_result_id=result["result_id"], request_id="warning-1")
    assert len(first["created"]) == 1
    EFFECTS.apply(tmp_path, "apply-warnings", expected_result_id=result["result_id"], request_id="warning-2")
    pages = list((tmp_path / "wiki/questions").glob("*.md"))
    assert len(pages) == 1
    assert "BEGIN UNTRUSTED EVIDENCE: Context" in pages[0].read_text()
