"""Canonical reporting stays usable after temporary executed sources disappear."""

import hashlib
import json
from pathlib import Path

import pytest
from coverage import CoverageData

from tests._script_loader import load_module

REPORT = load_module("canonical_coverage_report", Path(__file__).parents[1] / "tools/coverage_report.py")


def dataset(root):
    script = root / "workspace-template/scripts/worker.py"
    unused = root / "src/evidence_wiki/unused.py"
    helper = root / "temporary/scripts/helper.py"
    copy = root / "temporary/scripts/worker.py"
    for path in (script, unused, helper, copy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def choose(value):\n    if value:\n        return 1\n    return 0\n")
    data = CoverageData(basename=str(root / ".coverage.good"))
    data.add_arcs({str(script): [(-1, 1), (1, -1), (-1, 2), (2, 3), (3, -1)],
                   str(copy): [(-1, 2), (2, 4), (4, -1)], str(helper): [(-1, 1), (1, -1)]})
    data.write()
    helper.unlink()
    copy.unlink()
    (root / ".coverage.invalid").write_bytes(b"invalid SQLite data")
    return script, unused


def test_generates_text_xml_html_with_fixed_denominators_and_retains_original_shards(tmp_path):
    script, unused = dataset(tmp_path)
    snapshot = REPORT.source_snapshot(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.glob(".coverage.*")}
    output = tmp_path / "reports"
    manifest = REPORT.generate_reports(tmp_path, tmp_path / ".coverage", output, snapshot)
    assert manifest["status"] == "reported"
    assert manifest["excluded_shards"] == 1
    for entry in manifest["shards"]:
        assert hashlib.sha256((output / entry["preserved"]).read_bytes()).hexdigest() == entry["sha256"]
        if entry["status"] == "excluded":
            assert entry["reason"]
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob(".coverage.*")}
    for view in ("direct", "with-copy-aliases"):
        for relative in ("coverage.txt", "coverage.xml", "html/index.html"):
            assert (output / view / relative).stat().st_size > 0
        report = json.loads((output / view / "coverage.json").read_text())
        assert len(report["files"]) == 2
        missing = next(value for path, value in report["files"].items() if path.endswith("unused.py"))
        assert missing["summary"]["covered_lines"] == 0
        assert missing["summary"]["num_statements"] == 4
    direct, aliases = (manifest["reports"][view] for view in ("direct", "with-copy-aliases"))
    assert direct["num_statements"] == aliases["num_statements"] == 8
    assert direct["num_branches"] == aliases["num_branches"] == 4
    assert aliases["covered_branches"] > direct["covered_branches"]
    assert any("execution-time copy identity" in limit for limit in manifest["limits"])


def test_source_drift_fails_after_preserving_shards(tmp_path):
    script, _ = dataset(tmp_path)
    snapshot = REPORT.source_snapshot(tmp_path)
    script.write_text("changed = True\n")
    with pytest.raises(ValueError, match="sources changed"):
        REPORT.generate_reports(tmp_path, tmp_path / ".coverage", tmp_path / "reports", snapshot)
    manifest = json.loads((tmp_path / "reports/manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert len(manifest["shards"]) == 2


def test_no_valid_shards_does_not_manufacture_a_report(tmp_path):
    dataset(tmp_path)
    (tmp_path / ".coverage.good").unlink()
    with pytest.raises(ValueError, match="no readable branch shards"):
        REPORT.generate_reports(tmp_path, tmp_path / ".coverage", tmp_path / "reports", REPORT.source_snapshot(tmp_path))
    manifest = json.loads((tmp_path / "reports/manifest.json").read_text())
    assert manifest["reports"] == {}
    assert manifest["shards"][0]["status"] == "excluded"
