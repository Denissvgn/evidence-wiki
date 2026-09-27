"""Keep reporting and package subprocesses inside explicit test destinations."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_ci_job_summary(monkeypatch):
    """Tests may opt in with a temporary file, never an inherited job summary."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


@pytest.fixture
def run_package_command(tmp_path):
    """Execute supplied argv from an unrelated CWD without checkout path injection."""
    cwd = tmp_path / "command cwd"
    assert not cwd.resolve().is_relative_to(Path(__file__).resolve().parents[1]), (
        "Package command CWD must be outside the checkout."
    )
    cwd.mkdir()
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    interpreter_directory = Path(sys.executable).parent.resolve()
    environment["PATH"] = os.pathsep.join(
        entry for entry in environment.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != interpreter_directory
    )

    def run(argv, *, timeout=30):
        return subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, encoding="utf-8",
                              timeout=timeout, check=False, shell=False)

    return run
