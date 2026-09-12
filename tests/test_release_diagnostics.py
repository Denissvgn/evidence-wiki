"""Budget refusal preserves real measurements without changing the gate result."""

import json
from dataclasses import replace

from tests._script_loader import load_module
from tests.test_release_workflow import REPO_ROOT

DIAGNOSTICS = load_module("release_diagnostics", REPO_ROOT / "tools/collect_release_diagnostics.py")
SCALE = load_module("diagnostics_scale", REPO_ROOT / "tools/scale_benchmark.py")


def test_real_failing_budget_is_preserved_with_missing_report_reason(tmp_path, monkeypatch):
    standard = SCALE.BENCHMARK_PROFILES["standard"]
    small = replace(standard, sources=1, wiki_pages=1,
                    thresholds=replace(standard.thresholds, total_seconds=0.0))
    monkeypatch.setitem(SCALE.BENCHMARK_PROFILES, "standard", small)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("RELEASE_GATE_STATUS", "failure")
    source = tmp_path / "scale-benchmark-standard.json"
    code = SCALE.main(["--profile", "standard", "--require-budget", "--output", str(source)])
    assert code == 3
    measurement = json.loads(source.read_text())
    assert measurement["release_budget"]["verdict"] == "no_ship"
    output = tmp_path / "diagnostics"
    manifest = DIAGNOSTICS.collect(tmp_path, output)
    assert source.read_bytes() == (output / source.name).read_bytes()
    assert manifest["context"]["GITHUB_SHA"] == "a" * 40
    assert manifest["context"]["RELEASE_GATE_STATUS"] == "failure"
    missing, available = manifest["reports"]
    assert missing["status"] == "missing" and "producing step" in missing["reason"]
    assert available["status"] == "available" and available["sha256"]
    assert code == SCALE.EXIT_BUDGET_VIOLATED


def test_partial_report_is_retained_without_claiming_validity(tmp_path):
    (tmp_path / "artifact-validation.json").write_bytes(b'{"partial":')
    result = DIAGNOSTICS.collect(tmp_path, tmp_path / "diagnostics")
    assert result["reports"][0]["status"] == "invalid-json"
    assert (tmp_path / "diagnostics/artifact-validation.json").read_bytes() == b'{"partial":'
