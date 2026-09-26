"""Keep simulated reporting effects inside each test's explicit destinations."""

import pytest


@pytest.fixture(autouse=True)
def isolate_ci_job_summary(monkeypatch):
    """Tests may opt in with a temporary file, never an inherited job summary."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
