"""Simulated validator outcomes stay separate from the enclosing CI job summary."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import validate_installed_artifacts as artifacts

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_CASES = [
    "tests/test_delivery_qualification.py::test_distribution_failure_keeps_incomplete_evidence",
    "tests/test_delivery_qualification.py::test_retained_distribution_evidence_does_not_move_execution_into_checkout",
    "tests/test_qualification_management.py::test_failed_retention_restores_validator_runtime",
]


@pytest.mark.parametrize("entrypoint", ["direct", "grouped"])
def test_validator_cases_do_not_append_to_inherited_job_summary(tmp_path, entrypoint):
    summary = tmp_path / "job-summary.md"
    summary.write_text("Existing job summary\n", encoding="utf-8")
    before = summary.read_bytes()
    environment = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))
    command = [sys.executable]
    if entrypoint == "direct":
        command += ["-m", "pytest", "-q", "--tb=short"]
    else:
        command += [str(ROOT / "tools/run_test_groups.py"), "--root", str(ROOT),
                    "--output", str(tmp_path / "suite"), "--group-size", "48"]
    result = subprocess.run([*command, *VALIDATOR_CASES], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary.read_bytes() == before
    if entrypoint == "grouped":
        report = json.loads((tmp_path / "suite/manifest.json").read_text())
        assert report["status"] == "passed" and report["sources_unchanged"]
        assert report["groups"][0]["execution_complete"]


def test_summary_writer_can_be_exercised_with_an_explicit_private_destination(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    for status in ("failed", "passed"):
        artifacts.write_job_summary({"artifact": "wheel", "status": status, "commands": [
            {"stage": "wheel/probe", "status": status, "seconds": 1.25}]})
    content = summary.read_text()
    assert "### Installed wheel validation: failed" in content
    assert "### Installed wheel validation: passed" in content
    assert "| wheel/probe | failed | 1.2 |" in content
    assert "| wheel/probe | passed | 1.2 |" in content


def test_standalone_validator_retains_failure_summary_and_exit_status(tmp_path):
    summary = tmp_path / "real-summary.md"
    result = subprocess.run([sys.executable, str(ROOT / "tools/validate_installed_artifacts.py"),
        "--dist-dir", str(tmp_path / "absent"), "--evidence-dir", str(tmp_path / "evidence")],
        cwd=ROOT, env=dict(os.environ, GITHUB_STEP_SUMMARY=str(summary)),
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "### Installed distribution validation: failed" in summary.read_text()
    assert json.loads((tmp_path / "evidence/summary.json").read_text())["status"] == "failed"
