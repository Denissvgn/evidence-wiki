"""Frozen native coordination clocks and per-birth admission remain explicit."""

import copy
from datetime import datetime, timedelta

import pytest

from evidence_wiki._native_owner_execution import execution_policy
from tests.test_integration_improvement import fixture
from tests.test_native_owner_feasibility import frozen
from tools import _integration_improvement as gate
from tools import run_native_owner_feasibility as runner
from tools._integration_improvement_owners import host_readers
from tools._release_decision import canonical, digest


def extended_plan():
    plan, *_ = fixture()
    selected = execution_policy("extended")
    plan["scope"].update(execution_policy=selected, execution_policy_sha256=digest(selected),
        native_run_seconds=14400, native_cleanup_recipe="owned-popen-groups/v2")
    plan["expires_at"] = (datetime.fromisoformat(plan["frozen_at"].replace("Z", "+00:00")) + timedelta(seconds=21600)).isoformat()
    for slot in plan["slots"]:
        slot["elapsed_cap_seconds"] = 1300
    return plan


def test_extended_study_freezes_exact_policy_and_independent_audit(tmp_path, monkeypatch):
    _args, _result, (_root, study, plan, configs) = frozen(
        tmp_path, monkeypatch, mode="full", execution_profile="extended")
    selected = execution_policy("extended")
    assert study["schema_version"] == "native-owner-study/v2"
    assert study["execution_policy"] == plan["scope"]["execution_policy"] == selected
    assert study["execution_policy_sha256"] == plan["scope"]["execution_policy_sha256"] == digest(selected)
    assert study["recipe"]["authority_seconds"] == 14400
    assert study["recipe"]["study_seconds"] == 21600
    assert study["recipe"]["owner_pass_seconds"] == 120
    assert study["recipe"]["witness_seconds"] == 30
    assert plan["scope"]["native_cleanup_recipe"] == "owned-popen-groups/v2"
    assert {slot["elapsed_cap_seconds"] for slot in plan["slots"]} == {1300}
    assert len(plan["slots"]) == 9 and set(configs["audit"]["jobs"]) == {"scoped_mcp"}
    assert datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00")) - datetime.fromisoformat(plan["frozen_at"].replace("Z", "+00:00")) == timedelta(hours=6)


@pytest.mark.parametrize("change", [
    lambda p: p["scope"].pop("execution_policy"),
    lambda p: p["scope"].pop("execution_policy_sha256"),
    lambda p: p["scope"].update(execution_policy_sha256="0" * 64),
    lambda p: p["scope"].update(native_run_seconds=14401),
    lambda p: p["scope"].update(native_cleanup_recipe="owned-popen-groups/v1"),
    lambda p: p["slots"][0].update(elapsed_cap_seconds=1300.0),
    lambda p: p["slots"][0].update(elapsed_cap_seconds=1301),
    lambda p: p.update(expires_at=(datetime.fromisoformat(p["expires_at"].replace("Z", "+00:00")) + timedelta(seconds=1)).isoformat()),
    lambda p: p["scope"]["execution_policy"].update(witness_seconds=3600),
])
def test_rehashed_policy_drift_is_rejected_by_plan_and_direct_readers(change):
    plan = extended_plan()
    assert gate.validate_plan(plan, digest(plan)) == plan
    assert not host_readers(plan, {})
    change(plan)
    with pytest.raises(ValueError):
        gate.validate_plan(plan, digest(plan))
    with pytest.raises(ValueError):
        host_readers(plan, {})


@pytest.mark.parametrize("change", [
    lambda study: study.update(schema_version="native-owner-study/v1"),
    lambda study: study.update(extra_authority=True),
    lambda study: study["recipe"].update(authority_seconds=21600),
    lambda study: study.pop("execution_policy_sha256"),
])
def test_rebound_study_does_not_reinterpret_selected_policy(tmp_path, monkeypatch, change):
    args, _result, (root, study, _plan, _configs) = frozen(
        tmp_path, monkeypatch, mode="full", execution_profile="extended")
    changed = copy.deepcopy(study)
    change(changed)
    (root / "control/study.json").write_bytes(canonical(changed))
    args.expected_study = digest(changed)
    with pytest.raises(ValueError):
        runner.load_study(args)
