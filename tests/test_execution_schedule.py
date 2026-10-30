"""Frozen continuation configuration and deterministic offline host receipts."""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import DeterministicOfflineExecutor, ExecutionRoleLimit, ExecutionSchedule
from evidence_wiki.agent_execution import PROFILE, PROFILE_V2, ExecutionError, ExecutionRole
from evidence_wiki.workflow_contracts import MAX_BYTES, WorkflowContractError
from tests.test_agent_execution import attempt, profile


def limit(**changes):
    fields = dict(role="host_worker", controller_ref="registered-controller", model_calls=10,
                  tool_calls=10, tokens=1000, cost_limit="1", seconds=60)
    return ExecutionRoleLimit(**(fields | changes))


def offline_profile(**changes):
    role = ExecutionRole("researcher", "host_worker", "registered-controller", "offline/fixture", "prompt/v1")
    return profile(roles=(role,), **changes)


def test_legacy_profile_document_and_hash_are_unchanged():
    selected = profile()
    expected = {"schema_version": PROFILE, "profile_ref": "local-host", "total_steps": 5,
        "currency": "EUR", "pricing_ref": "local-process/v1", "roles": [{"config_ref": "researcher",
        "role": "host_worker", "controller_ref": "registered-controller", "model_ref": "local-process",
        "prompt_version": "prompt/v1"}], "job_budgets": {"model_calls": 0, "tool_calls": 5,
        "tokens": 0, "cost": "0"}, "cleanup_timeout_seconds": 0.2}
    from evidence_wiki.agent_execution import _digest

    assert selected.document() == expected
    assert selected.sha256 == _digest(expected)
    assert replace(selected, schedule=None).sha256 == selected.sha256


def test_schedule_is_frozen_versioned_and_defensively_copied():
    limits, reasons, chains = [limit()], ["executor_unavailable"], [["researcher", "backup"]]
    schedule = ExecutionSchedule(limits, reasons, 2, chains)
    backup = ExecutionRole("backup", "host_worker", "registered-controller", "other-model", "other-prompt")
    selected = profile(roles=(*profile().roles, backup), schedule=schedule)
    limits.clear()
    reasons.clear()
    chains[0].append("unconfigured")
    document = selected.document()
    assert document["schema_version"] == PROFILE_V2
    assert document["scheduling"] == {"role_limits": [limit().document()],
        "retry_reasons": ["executor_unavailable"], "backoff_seconds": 2,
        "failover": [["researcher", "backup"]]}
    assert selected.sha256 != replace(selected, schedule=None).sha256
    document["scheduling"]["role_limits"][0]["budgets"]["seconds"] = 0
    assert schedule.limit("host_worker", "registered-controller").seconds == 60
    with pytest.raises(FrozenInstanceError):
        schedule.backoff_seconds = 0
    with pytest.raises(ExecutionError, match="role_limit_unavailable"):
        schedule.limit("evaluator", "registered-controller")


@pytest.mark.parametrize("changes", [{"model_calls": True}, {"tool_calls": -1}, {"tokens": 100000001},
    {"seconds": 86401}, {"seconds": 1.2}, {"cost_limit": "NaN"}, {"role": []},
    {"controller_ref": "bad reference"}])
def test_role_limit_rejects_invalid_types_and_unbounded_values(changes):
    with pytest.raises(ExecutionError):
        limit(**changes)


@pytest.mark.parametrize("changes", [{"role_limits": []}, {"role_limits": [limit(), limit()]},
    {"role_limits": [object()]}, {"retry_reasons": ["x", "x"]}, {"retry_reasons": "x"},
    {"backoff_seconds": True}, {"backoff_seconds": 3601}, {"failover": [["a"]]},
    {"failover": [["a", "a"]]}, {"failover": [["a", "b"], ["b", "c"]]}])
def test_schedule_rejects_ambiguous_or_unbounded_configuration(changes):
    with pytest.raises(ExecutionError):
        ExecutionSchedule(**(dict(role_limits=[limit()]) | changes))


def test_profile_requires_all_role_buckets_and_refuses_unqualified_fallback():
    evaluator = ExecutionRole("judge", "evaluator", "judge-controller", "judge-model", "review/v1")
    with pytest.raises(ExecutionError, match="role_limits_incomplete"):
        profile(roles=(*profile().roles, evaluator), schedule=ExecutionSchedule((limit(),)))
    with pytest.raises(ExecutionError, match="role_unavailable"):
        profile(schedule=ExecutionSchedule((limit(),), failover=(("researcher", "missing"),)))
    for other in (evaluator, replace(profile().roles[0], config_ref="backup", controller_ref="different")):
        limits = (limit(), limit(role=other.role, controller_ref=other.controller_ref))
        with pytest.raises(ExecutionError, match="failover_identity_invalid"):
            profile(roles=(*profile().roles, other), schedule=ExecutionSchedule(limits,
                failover=(("researcher", other.config_ref),)))
    judge_backup = replace(evaluator, config_ref="judge-backup")
    with pytest.raises(ExecutionError, match="failover_identity_invalid"):
        profile(roles=(evaluator, judge_backup), schedule=ExecutionSchedule((limit(role="evaluator",
            controller_ref="judge-controller"),), failover=(("judge", "judge-backup"),)))


def test_offline_output_is_inert_deterministic_and_usage_is_local(tmp_path, monkeypatch):
    import evidence_wiki.agent_execution as execution

    def refuse_dispatch(*args, **kwargs):
        pytest.fail("Offline lookup attempted external process dispatch.")
    monkeypatch.setattr(execution.subprocess, "Popen", refuse_dispatch)
    selected, outputs = offline_profile(), {"researcher": {"proposal": ["fixed"]}}
    host = DeterministicOfflineExecutor(selected, outputs=outputs, receipt_key=b"a" * 32)
    value = attempt(selected)
    outputs["researcher"]["proposal"].append("changed")
    observed = host.invoke(value)
    assert observed["state"] == "succeeded" and observed["reason"] == "offline_fixture"
    assert observed["output"] == {"proposal": ["fixed"]}
    assert observed["final_accounting"] and observed["observed_usage"]["seconds"] >= 0
    assert {key: item for key, item in observed["observed_usage"].items() if key != "seconds"} == {
        "model_calls": 0, "tool_calls": 0, "tokens": 0, "cost": "0"}
    assert observed["started_utc"] and observed["completed_utc"]
    assert host.authenticate(observed, value) == observed
    assert host.poll(value) == observed == host.cancel(value) == host.invoke(value)
    observed["output"]["proposal"].append("caller-change")
    assert host.poll(value)["output"] == {"proposal": ["fixed"]}
    second = host.invoke(attempt(selected, attempt_id="another"))
    assert second["output"] == host.poll(value)["output"]


def test_offline_unknown_restart_retains_unobserved_usage_and_historical_authentication():
    selected = offline_profile()
    host = DeterministicOfflineExecutor(selected, outputs={"researcher": {}}, receipt_key=b"a" * 32)
    value = attempt(selected)
    observed = host.invoke(value)
    restarted = DeterministicOfflineExecutor(selected, outputs={"researcher": {}}, receipt_key=b"a" * 32)
    lost = restarted.poll(value)
    assert lost["reason"] == "dispatch_not_observed" and not lost["final_accounting"]
    assert all(item is None for item in lost["observed_usage"].values())
    assert restarted.cancel(value)["reason"] == "dispatch_not_observed"
    assert restarted.authenticate(observed, value) == observed
    assert restarted._attempts == {}


def test_known_offline_unavailable_is_zero_and_immutable():
    selected = offline_profile(total_steps=1)
    host = DeterministicOfflineExecutor(selected, outputs={}, receipt_key=b"a" * 32)
    value = attempt(selected)
    observed = host.invoke(value)
    assert observed["state"] == "unavailable" and observed["reason"] == "executor_unavailable"
    assert observed["final_accounting"] and observed["started_utc"] is None
    assert observed["observed_usage"] == {"seconds": 0, "model_calls": 0, "tool_calls": 0,
                                         "tokens": 0, "cost": "0"}
    assert host.invoke(value) == observed
    with pytest.raises(ExecutionError, match="step_limit"):
        host.invoke(attempt(selected, attempt_id="another"))
    with pytest.raises(ExecutionError, match="attempt_conflict"):
        host.invoke(replace(value, nonce="other"))


@pytest.mark.parametrize("changes", [{"outputs": {"unknown": {}}}, {"outputs": {"researcher": lambda: {}}},
    {"outputs": {"researcher": {"number": float("nan")}}}, {"receipt_key": b"short"},
    {"profile": profile()}])
def test_offline_configuration_requires_explicit_reserved_models_and_closed_output(changes):
    with pytest.raises(ExecutionError):
        DeterministicOfflineExecutor(**(dict(profile=offline_profile(), outputs={}, receipt_key=b"a" * 32) | changes))


def test_offline_receipts_refuse_tampering_cross_key_and_role_budget_overrun():
    selected = offline_profile()
    host = DeterministicOfflineExecutor(selected, outputs={"researcher": {}}, receipt_key=b"a" * 32)
    value = attempt(selected)
    observed = host.invoke(value)
    altered = copy.deepcopy(observed)
    altered["reason"] = "live_model"
    with pytest.raises(ExecutionError, match="authentication_refused"):
        host.authenticate(altered, value)
    other = DeterministicOfflineExecutor(selected, outputs={}, receipt_key=b"b" * 32)
    with pytest.raises(ExecutionError, match="authentication_refused"):
        other.authenticate(observed, value)
    with pytest.raises(ExecutionError, match="correlation_refused"):
        host.authenticate(observed, replace(value, nonce="other"))
    oversized = attempt(selected, attempt_id="oversized", job_changes={"budgets": {
        "model_calls": 1, "tool_calls": 5, "tokens": 0, "cost": "0"}})
    with pytest.raises(ExecutionError, match="job_budget_exceeded"):
        host.invoke(oversized)


def test_offline_dispatch_rejects_expired_jobs_but_reconciles_historical_completion(monkeypatch):
    import evidence_wiki.agent_execution as execution

    selected = offline_profile()
    host = DeterministicOfflineExecutor(selected, outputs={"researcher": {}}, receipt_key=b"a" * 32)
    now = datetime.now(timezone.utc)
    expired = attempt(selected, job_changes={"issued_utc": (now - timedelta(seconds=3)).isoformat(),
        "deadline_utc": (now - timedelta(seconds=1)).isoformat(), "timeout_seconds": 1})
    with pytest.raises(WorkflowContractError):
        host.invoke(expired)
    assert host._attempts == {}
    value = attempt(selected)
    observed = host.invoke(value)
    monkeypatch.setattr(execution, "_now", lambda: (now + timedelta(hours=1)).isoformat())
    assert host.poll(value) == observed == host.invoke(value)
    assert host.authenticate(observed, value) == observed


def test_offline_output_bound_is_checked_before_dispatch():
    with pytest.raises(ExecutionError, match="output_limit_invalid"):
        DeterministicOfflineExecutor(offline_profile(), outputs={"researcher": {"large": "x" * (MAX_BYTES // 2)}},
                                     receipt_key=b"a" * 32)
