"""The grouped suite gate retains failures and its complete execution inventory."""

import json
import shutil
from pathlib import Path

import pytest

from tests._script_loader import load_module
from tests.test_coverage_report import REPORT as REPORTER

ROOT = Path(__file__).resolve().parents[1]
GROUPS = load_module("test_groups_tool", ROOT / "tools/run_test_groups.py")


@pytest.mark.parametrize("kind,units,accepted", [
    ("ascii", 1023, True), ("ascii", 1024, True), ("ascii", 1025, False),
    ("unicode", 1024, True), ("unicode", 1025, False),
    ("named", 120000, True), ("automatic", 40000, False), ("multiple", 1025, False),
], ids=["below", "at", "above", "unicode-at", "unicode-above", "named-payload", "automatic-payload", "many"])
def test_collection_id_guard(tmp_path, kind, units, accepted):
    root, output = tmp_path / "repo", tmp_path / "evidence"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n')
    prefix = "tests/test_case.py::test_case["
    identifier_units = units - len(prefix) - 1
    identifier = ("\U0001f642" * (identifier_units // 2) + "x" * (identifier_units % 2)
                  if kind == "unicode" else "x" * identifier_units)
    if kind in {"named", "automatic"}:
        parameter = "@pytest.mark.parametrize('data', ['x' * " + str(units) + "]"
        parameter += ", ids=['large'])" if kind == "named" else ")"
        signature = "data"
    else:
        identifiers = [identifier + str(i) for i in range(8)] if kind == "multiple" else [identifier]
        parameter = "@pytest.mark.parametrize('data', " + repr(list(range(len(identifiers)))) + ", ids=" + repr(identifiers) + ")"
        signature = "data"
    (root / "tests/test_case.py").write_text(
        "import pytest\nfrom pathlib import Path\n" + parameter + "\n"
        + f"def test_case({signature}):\n"
        + "    Path('executed').write_text('yes')\n", encoding="utf-8")
    code = GROUPS.main(["--root", str(root), "--output", str(output)])
    report = json.loads((output / "manifest.json").read_text())
    assert (code == 0) is accepted
    assert (root / "executed").exists() is accepted
    if accepted:
        observed = report["groups"][0]["result"]
        assert observed["collected"] == observed["executed"]
        assert observed["collected"] == report["collection"]["result"]["collected"]
        if kind == "named":
            assert observed["collected"] == [prefix + "large]"]
    else:
        assert report["status"] == "failed" and report["groups"] == []
        assert report["collection"]["result"]["executed"] == []
        log = (output / "collection.log").read_text()
        assert "Use short explicit parameter IDs" in log
        assert "1024 UTF-16 units" in log
        assert len(log) < 2000
        if kind == "multiple":
            assert "8 test IDs" in log and log.count("UTF-16 units") == 6


def test_groups_preserve_order_and_keep_large_modules_intact():
    nodes = ["a.py::a", "a.py::b", "b.py::c", "c.py::d", "c.py::e", "c.py::f"]
    result = GROUPS.groups(nodes, 2)
    assert result == [nodes[:2], nodes[2:3], nodes[3:]]
    assert [node for group in result for node in group] == nodes


def test_source_identity_includes_frozen_data_and_ignores_interpreter_caches(tmp_path):
    fixture = tmp_path / "tests/fixtures/cases.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"expected": false}')
    before = GROUPS.source_identity(tmp_path)
    fixture.write_text('{"expected": true}')
    after = GROUPS.source_identity(tmp_path)
    assert before != after and "tests/fixtures/cases.json" in after
    cache = tmp_path / "tests/__pycache__/generated.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    assert GROUPS.source_identity(tmp_path) == after


def test_failed_test_remains_failed_with_coverage_and_diagnostics(tmp_path, monkeypatch):
    root, output = tmp_path / "repo", tmp_path / "evidence"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    script = root / "workspace-template/scripts/example.py"
    script.parent.mkdir(parents=True)
    script.write_text("value = 1\n")
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "pyproject.toml").write_text('[tool.coverage.run]\nbranch = true\nparallel = true\n')
    (root / "tests/test_failure.py").write_text(
        'import runpy\nfrom pathlib import Path\n'
        'def test_measured_failure():\n'
        '    runpy.run_path(str(Path(__file__).parents[1] / "workspace-template/scripts/example.py"))\n'
        '    assert False, "intentional behavior failure"\n')
    (root / "tests/test_success.py").write_text('def test_success():\n    assert 1 + 1 == 2\n')
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    snapshot = REPORTER.source_snapshot(root)
    code = GROUPS.main(["--root", str(root), "--output", str(output), "--coverage", "--group-size", "1"])
    assert code == 1
    report = json.loads((output / "manifest.json").read_text())
    assert report["status"] == "failed" and report["sources_unchanged"]
    assert len(report["groups"]) == 2
    assert [row["exit_code"] for row in report["groups"]] == [1, 0]
    assert all(row["selection_matches"] and row["execution_complete"] for row in report["groups"])
    assert "intentional behavior failure" in Path(report["groups"][0]["log"]).read_text()
    assert all(row["result"]["memory"]["scope"] for row in report["groups"])
    coverage = REPORTER.generate_reports(root, root / ".coverage", tmp_path / "coverage", snapshot)
    assert coverage["status"] == "reported"
    assert code == 1 and report["status"] == "failed"


def test_timeout_retains_the_active_case_and_prior_failure(tmp_path):
    root, output = tmp_path / "repo", tmp_path / "evidence"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    output.mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "tests/test_interrupted.py").write_text(
        'import time\n'
        'def test_failure():\n'
        '    assert False, "retained failure before timeout"\n'
        'def test_waiting():\n'
        '    time.sleep(60)\n')
    result = GROUPS.run(root, output, "interrupted", ["tests"], timeout=10)
    assert result["exit_code"] == 124 and "missing_record" in result
    rows = [json.loads(line) for line in (output / "interrupted.progress.jsonl").read_text().splitlines()]
    failures = [row for row in rows if row.get("outcome") == "failed"]
    assert "retained failure before timeout" in failures[0]["failure"]
    assert [row for row in rows if row["event"] == "started"][-1]["nodeid"].endswith("::test_waiting")
    assert not any(row["phase"] == "call" for row in rows
                   if row["event"] == "report" and row["nodeid"].endswith("::test_waiting"))
