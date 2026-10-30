"""Prospective fixture duration is bound before native state is created."""

from pathlib import Path

import pytest

from tests._script_loader import load_module
from tools._repeated_integration_host import run_scenario


def support_module():
    path = Path(__file__).resolve().parents[1] / "examples/unattended-host/support.py"
    return load_module("reference_duration_support", path)


def test_new_duration_changes_only_policy_identity_and_time_budget(tmp_path):
    support = support_module()
    original = support.policy(tmp_path, "Retained reference observation").to_dict()
    explicit = support.policy(tmp_path, "Retained reference observation", run_seconds=1800).to_dict()
    extended = support.policy(tmp_path, "Retained reference observation", run_seconds=14400).to_dict()
    assert original == explicit
    assert original["policy_id"] != extended["policy_id"]
    assert extended["budgets"]["seconds"] == 14400
    extended["budgets"]["seconds"] = original["budgets"]["seconds"]
    extended["policy_id"] = original["policy_id"]
    assert extended == original


@pytest.mark.parametrize("duration", [True, False, 0, -1, 25201, 1800.0, "1800"])
def test_invalid_duration_cannot_create_native_output(tmp_path, duration):
    with pytest.raises(ValueError):
        support_module().policy(tmp_path, "Retained reference observation", run_seconds=duration)
    target = tmp_path / "scenario"
    plan = {"hosts": [{"host_id": "direct", "route": "direct"}], "scope": {"native_run_seconds": duration}}
    with pytest.raises(ValueError, match="integration_run_duration_invalid"):
        run_scenario(plan, "direct", target)
    assert not target.exists()


def test_native_duration_cannot_exceed_frozen_study_window(tmp_path):
    plan = {"hosts": [{"host_id": "direct", "route": "direct"}], "scope": {"native_run_seconds": 14400},
            "frozen_at": "2030-01-01T00:00:00+00:00", "expires_at": "2030-01-01T02:00:00+00:00"}
    target = tmp_path / "scenario"
    with pytest.raises(ValueError, match="integration_run_duration_exceeds_study"):
        run_scenario(plan, "direct", target)
    assert not target.exists()
