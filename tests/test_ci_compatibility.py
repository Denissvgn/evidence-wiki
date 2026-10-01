"""Compatibility evidence cannot substitute for complete test execution."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from tools import check_ci_compatibility as CHECK

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("damage", [None, "failed-case", "oversized-id", "missing-target", "changed-source", "wrong-python"])
def test_compatibility_gate_retains_evidence_and_refuses_incomplete_checks(tmp_path, monkeypatch, damage):
    root, output = tmp_path / "repo", tmp_path / "evidence"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    for target in CHECK.TARGETS:
        module, _, function = target.partition("::")
        path = root / module
        if damage == "missing-target" and function:
            continue
        code = 'from pathlib import Path\ndef ' + (function or "test_html") + '():\n'
        code += '    Path("executed").write_text("yes")\n'
        if damage == "failed-case":
            code += '    assert False, "retained compatibility failure"\n'
        if damage == "changed-source":
            code += '    Path(__file__).write_text("changed")\n'
        if damage == "oversized-id":
            code = "import pytest\n@pytest.mark.parametrize('data', ['x' * 40000])\n" + code.replace("():", "(data):")
        path.write_text(code, encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setenv("EVIDENCE_WIKI_EXPECTED_PYTHON", "0.0.0" if damage == "wrong-python" else sys.version.split()[0])
    result = CHECK.main(["--output", str(output)])
    summary = json.loads((output / "summary.json").read_text())
    assert summary["kind"] == "compatibility" and not (output / "manifest.json").exists()
    assert summary["python_version"] == sys.version.split()[0]
    if damage is None:
        assert result == 0 and summary["status"] == "passed"
        assert summary["selected"] == summary["collected"] == 3
        assert (output / "compatibility.xml").is_file()
    else:
        assert result == 1 and summary["status"] == "failed"
        assert summary["error"]
        if damage in {"wrong-python", "oversized-id", "missing-target"}:
            assert not (root / "executed").exists()
        if damage == "failed-case":
            assert "retained compatibility failure" in (output / "compatibility.log").read_text()


@pytest.mark.parametrize("damage", ["missing-record", "failed-record", "partial-execution", "extra-selection", "skipped"])
def test_compatibility_gate_refuses_incomplete_execution_records(damage):
    row = {"label": "compatibility", "exit_code": 0,
           "result": {"exit_code": 0, "collected": ["case"], "executed": ["case"], "outcomes": {"passed": 1}}}
    if damage == "missing-record":
        del row["result"]
    elif damage == "failed-record":
        row["result"]["exit_code"] = 1
    elif damage == "partial-execution":
        row["result"]["executed"] = []
    elif damage == "skipped":
        row["result"]["outcomes"] = {"skipped": 1}
    else:
        row["result"]["collected"].append("unexpected")
    with pytest.raises(ValueError):
        CHECK.require_complete(row, ["case"])
