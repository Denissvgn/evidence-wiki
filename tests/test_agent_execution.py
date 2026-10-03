"""Real host execution, authenticated receipts and bounded interruption."""

from __future__ import annotations

import copy
import json
import os
import sys
import time
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki.agent_execution import (
    BoundedSubprocessExecutor,
    ExecutionAttempt,
    ExecutionError,
    ExecutionProfile,
    ExecutionRole,
    validate_observation,
)
from evidence_wiki.workflow_contracts import validate_result
from tests.test_workflow_contracts import job


def profile(**changes):
    fields = {
        "profile_ref": "local-host", "total_steps": 5, "currency": "EUR", "pricing_ref": "local-process/v1",
        "roles": (ExecutionRole("researcher", "host_worker", "registered-controller", "local-process", "prompt/v1"),),
        "model_calls": 0, "tool_calls": 5, "tokens": 0, "cost_limit": "0", "cleanup_timeout_seconds": 0.2,
    }
    return ExecutionProfile(**(fields | changes))


def attempt(selected=None, *, attempt_id="attempt-1", nonce="nonce-1", timeout=5, **changes):
    selected = selected or profile()
    now = datetime.now(timezone.utc)
    basis = job()
    basis.update(issued_utc=(now - timedelta(seconds=1)).isoformat(),
                 deadline_utc=(now + timedelta(seconds=10)).isoformat(), timeout_seconds=timeout)
    basis.update(changes.pop("job_changes", {}))
    return ExecutionAttempt.create(attempt_id=attempt_id, nonce=nonce, run_id="run-1", binding_id="binding-1",
                                   profile=selected, config_ref="researcher", job=basis,
                                   input_document={"packet": "bounded-evidence"}, **changes)


def executor(tmp_path, script, selected=None, **changes):
    if os.name != "posix":
        pytest.skip("The reference executor requires POSIX process groups.")
    return BoundedSubprocessExecutor(selected or profile(),
                                    commands={"researcher": (sys.executable, "-c", script)}, cwd=tmp_path, **changes)


def final(host, value, timeout=4):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        observed = host.poll(value)
        if observed["state"] != "running":
            return observed
        time.sleep(0.01)
    pytest.fail("The actual bounded process did not settle.")


class LocalMeter:
    def finalize(self, value, summary):
        return {"seconds": summary["seconds"], "model_calls": 0, "tool_calls": 1, "tokens": 0, "cost": "0"}


def test_process_dispatch_input_correlation_and_host_measured_receipt(tmp_path):
    script = "import json,sys; v=json.load(sys.stdin); print(json.dumps({'packet':v['input']['packet']}))"
    host, value = executor(tmp_path, script, meter=LocalMeter()), attempt()
    first = host.invoke(value)
    assert first["state"] in {"running", "succeeded"}
    observed = final(host, value)
    assert observed["state"] == "succeeded" and observed["output"] == {"packet": "bounded-evidence"}
    assert observed["final_accounting"] and observed["observed_usage"]["cost"] == "0"
    assert host.authenticate(observed, value) == observed
    assert observed["completed_utc"] and observed["output_sha256"]
    assert observed["observed_usage"]["seconds"] >= 0
    assert not observed["cancellation_confirmed"]


def test_default_worker_cost_and_token_claims_cannot_become_measured_usage(tmp_path):
    host = executor(tmp_path, "print('{\"cost\":\"0\",\"tokens\":0,\"model_calls\":0}')")
    value = attempt()
    host.invoke(value)
    observed = final(host, value)
    assert observed["output"]["cost"] == "0"
    assert observed["observed_usage"]["cost"] is None and observed["observed_usage"]["tokens"] is None
    assert observed["observed_usage"]["model_calls"] is None and not observed["final_accounting"]


def test_deadline_enforced_without_polling_or_cooperative_worker(tmp_path):
    marker = tmp_path / "after-timeout"
    script = f"import time,pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('unexpected'); print('{{}}')"
    host, value = executor(tmp_path, script), attempt(timeout=1)
    host.invoke(value)
    time.sleep(1.25)
    observed = host.poll(value)
    assert observed["state"] == "expired" and observed["reason"] == "deadline_expired"
    assert not marker.exists() and not observed["cancellation_confirmed"]
    assert host.authenticate(observed, value) == observed


def test_real_cancellation_confirms_exit_and_preserves_unknown_accounting(tmp_path):
    host, value = executor(tmp_path, "import time; time.sleep(20)"), attempt()
    host.invoke(value)
    observed = host.cancel(value)
    assert observed["state"] == "cancelled" and observed["cancellation_confirmed"]
    assert observed["completed_utc"] and not observed["final_accounting"]
    assert host.cancel(value)["state"] == "cancelled"


def test_completion_racing_cancel_retains_observed_completion(tmp_path):
    host, value = executor(tmp_path, "print('{}')"), attempt()
    host.invoke(value)
    final(host, value)
    observed = host.cancel(value)
    assert observed["state"] == "succeeded" and not observed["cancellation_confirmed"]


def test_background_descendant_is_stopped_after_leader_exit(tmp_path):
    marker = tmp_path / "descendant-write"
    child = f"import time,pathlib; time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('unexpected')"
    script = f"import subprocess; subprocess.Popen([{sys.executable!r},'-c',{child!r}]); print('{{}}')"
    host, value = executor(tmp_path, script), attempt()
    host.invoke(value)
    assert final(host, value)["state"] == "succeeded"
    time.sleep(0.6)
    assert not marker.exists()


def test_duplicate_dispatch_is_idempotent_and_altered_attempt_conflicts(tmp_path):
    marker = tmp_path / "dispatches"
    script = f"import pathlib; p=pathlib.Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('{{}}')"
    host, value = executor(tmp_path, script), attempt()
    host.invoke(value)
    final(host, value)
    host.invoke(value)
    assert marker.read_text() == "x"
    with pytest.raises(ExecutionError, match="attempt_conflict"):
        host.invoke(replace(value, nonce="changed-nonce"))


def test_serial_execution_and_total_attempt_bound(tmp_path):
    selected = profile(total_steps=1)
    host = executor(tmp_path, "import time;time.sleep(0.1);print('{}')", selected)
    value = attempt(selected)
    host.invoke(value)
    with pytest.raises(ExecutionError, match="step_limit"):
        host.invoke(attempt(selected, attempt_id="another"))
    final(host, value)
    with pytest.raises(ExecutionError, match="step_limit"):
        host.invoke(attempt(selected, attempt_id="another"))


def test_no_environment_or_receipt_key_inheritance(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_SECRET", "host-sensitive-value")
    script = "import json,os,sys;v=json.load(sys.stdin);print(json.dumps({'secret':os.getenv('EXAMPLE_SECRET'),'fields':sorted(v)}))"
    host, value = executor(tmp_path, script), attempt()
    host.invoke(value)
    output = final(host, value)["output"]
    assert output["secret"] is None
    assert not any("key" in field for field in output["fields"])
    with pytest.raises(ExecutionError, match="environment_refused"):
        executor(tmp_path, "print('{}')", environment={"API_TOKEN": "explicit-sensitive-value"})


@pytest.mark.parametrize("mutation", ["output", "run_id", "attempt_id", "nonce", "observed_usage", "authentication"])
def test_tamper_replay_and_cross_host_authentication_refuse(tmp_path, mutation):
    host, value = executor(tmp_path, "print('{}')"), attempt()
    host.invoke(value)
    observed = final(host, value)
    altered = copy.deepcopy(observed)
    if mutation == "output":
        altered[mutation] = {"invented": True}
    elif mutation == "observed_usage":
        altered[mutation]["cost"] = "0"
    elif mutation == "authentication":
        altered[mutation] = "0" * 64
    else:
        altered[mutation] = "another"
    with pytest.raises(ExecutionError):
        host.authenticate(altered, value)
    with pytest.raises(ExecutionError):
        host.authenticate(observed, replace(value, nonce="different-nonce"))
    different = executor(tmp_path, "print('{}')")
    with pytest.raises(ExecutionError):
        different.authenticate(observed, value)


def test_historical_receipt_authentication_does_not_relax_live_result_gate(tmp_path):
    host, value = executor(tmp_path, "print('{}')"), attempt()
    host.invoke(value)
    observed = final(host, value)
    assert host.authenticate(observed, value)["state"] == "succeeded"
    from tests.test_workflow_contracts import result

    after_expiry = (datetime.fromisoformat(value.job["deadline_utc"]) + timedelta(seconds=1)).isoformat()
    with pytest.raises(Exception, match="Workflow contract"):
        validate_result(result(value.job), job=value.job, now_utc=after_expiry)
    assert host.authenticate(observed, value) == observed


@pytest.mark.parametrize("script,reason", [
    ("print('not-json')", "output_invalid"), ("print('{\"a\":1,\"a\":2}')", "output_invalid"),
    ("print('{\"n\":NaN}')", "output_invalid"), ("print('[]')", "output_invalid"),
    ("print('x'*1000000)", "output_limit"), ("raise RuntimeError('private worker failure')", "worker_failed"),
])
def test_invalid_or_unbounded_worker_output_fails_finitely(tmp_path, script, reason):
    host, value = executor(tmp_path, script, max_output_bytes=1024), attempt()
    host.invoke(value)
    observed = final(host, value)
    assert observed["state"] == "failed" and observed["reason"] == reason
    assert observed["output"] is None
    assert "private worker failure" not in json.dumps(observed)


@pytest.mark.skipif(os.name != "posix", reason="The reference executor requires POSIX process groups.")
def test_missing_host_after_restart_never_redispatches_on_poll_or_cancel(tmp_path):
    selected, value = profile(), attempt()
    host = BoundedSubprocessExecutor(selected, commands={}, cwd=tmp_path)
    assert host.poll(value)["reason"] == "dispatch_not_observed"
    assert host.cancel(value)["reason"] == "dispatch_not_observed"
    assert host.invoke(value)["reason"] == "executor_unavailable"


def test_stalled_meter_is_bounded_and_late_measurement_is_not_accepted(tmp_path):
    class StalledMeter:
        def finalize(self, value, summary):
            time.sleep(0.5)
            return LocalMeter().finalize(value, summary)

    host, value = executor(tmp_path, "print('{}')", meter=StalledMeter()), attempt()
    started = time.monotonic()
    host.invoke(value)
    observed = final(host, value)
    assert time.monotonic() - started < 0.5 and not observed["final_accounting"]
    time.sleep(0.5)
    assert not host.poll(value)["final_accounting"]


def test_profiles_attempts_and_observations_are_detached_and_immutable(tmp_path):
    selected, value = profile(), attempt()
    with pytest.raises(FrozenInstanceError):
        selected.total_steps = 0
    detached = value.job
    detached["payload"]["question"] = "changed"
    assert value.job != detached
    host = executor(tmp_path, "print('{}')")
    host.invoke(value)
    observed = final(host, value)
    detached = validate_observation(observed)
    detached["observed_usage"]["cost"] = "changed"
    assert observed["observed_usage"]["cost"] is None
    with pytest.raises(ExecutionError):
        validate_observation(observed | {"new_field": True})


@pytest.mark.parametrize("changes", [
    {"total_steps": 0}, {"total_steps": True}, {"currency": "eur"}, {"cost_limit": "-1"},
    {"cleanup_timeout_seconds": float("nan")}, {"model_calls": 51},
    {"roles": (ExecutionRole("same", "host_worker", "c", "m", "p"),) * 2},
])
def test_invalid_frozen_profile_refuses(changes):
    with pytest.raises(ExecutionError):
        profile(**changes)


def test_role_prompt_controller_and_job_budget_bind_before_dispatch(tmp_path):
    host = executor(tmp_path, "print('{}')")
    for changes in ({"controller_ref": "forged"}, {"prompt_version": "changed"},
                    {"budgets": {"model_calls": 1, "tool_calls": 5, "tokens": 0, "cost": "0"}}):
        with pytest.raises(ExecutionError):
            host.invoke(attempt(job_changes=changes))
    with pytest.raises(ExecutionError, match="profile_mismatch"):
        host.invoke(attempt(profile(total_steps=3)))


def test_reference_executor_refuses_unsupported_platform_before_dispatch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from evidence_wiki import agent_execution as execution

    monkeypatch.setattr(execution, "os", SimpleNamespace(name="nt"))
    with pytest.raises(ExecutionError, match="execution_process_boundary_unsupported"):
        BoundedSubprocessExecutor(profile(), commands={}, cwd=tmp_path)
