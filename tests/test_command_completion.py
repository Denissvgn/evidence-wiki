"""Authenticated completion binds the exact parent-owned producer birth."""

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools import _integration_owner_pass as groups
from tools import measure_integration_costs as observer
from tools._command_completion import CommandCompletion
from tools._integration_costs import METRICS, freeze_cost_plan
from tools._journey_cases import digest

KEY = b"k" * 32
SCOPE = {"plan_sha256": "a" * 64, "stage": "run-primary-direct"}


def observe(tmp_path, program="pass", **kwargs):
    now = datetime.now(timezone.utc)
    plan = freeze_cost_plan(scope_id="completion", binding={"selection": "own-command"}, sources=[{
        "source_id": "host", "provenance": "host_observed", "kind": "automated", "origin": "Owned command",
        "collection_started_at": (now - timedelta(seconds=1)).isoformat(),
        "collection_ended_at": (now + timedelta(minutes=2)).isoformat(), "coverage": "partial", "authentication_ref": None}],
        attempts=[{"attempt_id": "selected", "phase": "operation", "allocation": "repeated", "source_id": "host",
                   "required_metrics": list(METRICS)}])
    directory, work = tmp_path / "retained", tmp_path / "workers"
    directory.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    argv = [sys.executable, "-c", program]
    options = {"observer_key": KEY, "directory": directory, "work_root": work}
    result = observer.observe_command(plan, "selected", argv, **options, **kwargs)
    return plan, argv, options, result


def read(plan, argv, options, result, **kwargs):
    return observer.read_command_receipt(plan, "selected", argv, **options,
        expected_receipt_sha256=result["receipt_sha256"], **kwargs)


def forbidden(*args, **kwargs):
    raise AssertionError("late numeric process operation")


def test_short_actual_command_authenticates_parent_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "stop_process", forbidden)
    plan, argv, options, result = observe(tmp_path, native_child_scope=SCOPE)
    certificate = result["receipt"]["producer_completion"]
    assert certificate["returncode"] == 0
    assert certificate["leader_reaped"] is certificate["group_absent"] is True
    start = json.loads((options["directory"] / "selected.start.json").read_bytes())
    assert start["pid"] == certificate["worker_pid"] and start["birth_id"] == certificate["birth_id"]
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    monkeypatch.setattr(os, "getpgid", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    checked = read(plan, argv, options, result, native_child_scope=SCOPE)
    assert checked == result
    proof = observer.producer_cleanup_proof(checked, worker_pid=certificate["worker_pid"], **SCOPE)
    assert proof == {"worker_pid": certificate["worker_pid"], **SCOPE, "group_absent": True,
                     "returncode": 0, "command_receipt_sha256": result["receipt_sha256"]}
    for unverified in (result, dict(checked)):
        with pytest.raises(ValueError, match="authenticated_producer"):
            observer.producer_cleanup_proof(unverified, worker_pid=certificate["worker_pid"], **SCOPE)
    checked["receipt_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="authenticated_producer"):
        observer.producer_cleanup_proof(checked, worker_pid=certificate["worker_pid"], **SCOPE)


def test_actual_live_timeout_kills_owned_leader_once_before_wait(tmp_path, monkeypatch):
    original = os.killpg
    signals = []

    def signal_group(pid, value):
        if value:
            signals.append((pid, value))
        return original(pid, value)

    monkeypatch.setattr(os, "killpg", signal_group)
    monkeypatch.setattr(observer, "stop_process", forbidden)
    plan, argv, options, result = observe(tmp_path, "import time; time.sleep(20)",
                                        timeout_seconds=0.1, native_child_scope=SCOPE)
    certificate = result["receipt"]["producer_completion"]
    assert result["receipt"]["status"] == "failed" and "timeout" in result["receipt"]["failure"]
    assert certificate["returncode"] == -signal.SIGKILL
    assert signals == [(certificate["worker_pid"], signal.SIGKILL)]
    checked = read(plan, argv, options, result, native_child_scope=SCOPE)
    assert observer.producer_cleanup_proof(checked, worker_pid=certificate["worker_pid"], **SCOPE)["group_absent"]


def test_completed_birth_never_probes_or_signals_later_numeric_reuse(monkeypatch):
    actual = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    actual.wait(timeout=5)
    completed = CommandCompletion(actual, SCOPE, "b" * 64)
    certificate = completed.close()
    assert certificate is not None
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    monkeypatch.setattr(os, "getpgid", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(actual, "wait", forbidden)
    monkeypatch.setattr(actual, "poll", forbidden)
    assert completed.close() is certificate


@pytest.mark.parametrize("absence", [True, None, 0, PermissionError("denied")])
def test_reaped_present_or_unknown_group_has_no_signal_or_proof(monkeypatch, absence):
    child = SimpleNamespace(pid=12345, returncode=0, _waitpid_lock=threading.Lock(), wait=forbidden, poll=forbidden)

    def probe(actual):
        assert actual is child
        if isinstance(absence, Exception):
            raise absence
        return absence

    monkeypatch.setattr(groups, "_group_alive", probe)
    monkeypatch.setattr(os, "getpgid", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    completed = CommandCompletion(child, SCOPE, "b" * 64)
    assert completed.close() is None
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    assert completed.close() is None


@pytest.mark.parametrize("mutation", ["birth", "scope", "returncode", "missing", "downgrade", "start_version", "extra", "bool", "early", "late", "naive", "start_scope"])
def test_resigned_lifecycle_forgery_is_refused(tmp_path, mutation):
    plan, argv, options, result = observe(tmp_path, native_child_scope=SCOPE)
    final_path = options["directory"] / "selected.receipt.json"
    receipt = result["receipt"]
    if mutation == "birth":
        receipt["producer_completion"]["birth_id"] = "b" * 64
    elif mutation == "scope":
        receipt["producer_completion"]["stage"] = "replay"
    elif mutation == "returncode":
        receipt["producer_completion"]["returncode"] = 17
    elif mutation == "missing":
        del receipt["producer_completion"]
    elif mutation == "downgrade":
        receipt["schema_version"] = "integration-cost-receipt/v1"
        del receipt["producer_completion"]
    elif mutation == "start_version":
        path = options["directory"] / "selected.start.json"
        start = json.loads(path.read_bytes())
        start["schema_version"] = "integration-cost-start/v1"
        path.write_text(json.dumps(observer._signed(start, KEY)))
    elif mutation == "start_scope":
        path = options["directory"] / "selected.start.json"
        start = json.loads(path.read_bytes())
        start["native_child_scope"] = {**SCOPE, "stage": "replay"}
        path.write_text(json.dumps(observer._signed(start, KEY)))
    elif mutation in {"early", "late", "naive"}:
        receipt["producer_completion"]["observed_at"] = {
            "early": "2000-01-01T00:00:00+00:00", "late": "2099-01-01T00:00:00+00:00",
            "naive": "2026-01-01T00:00:00"}[mutation]
    elif mutation == "extra":
        receipt["producer_completion"]["extra"] = True
    else:
        receipt["producer_completion"]["returncode"] = False
    receipt = observer._signed(receipt, KEY)
    final_path.write_text(json.dumps(receipt))
    result["receipt_sha256"] = digest(receipt)
    with pytest.raises(ValueError, match="cost_(producer_completion_invalid|receipt_lifecycle_version)"):
        read(plan, argv, options, result, native_child_scope=SCOPE)


def test_spawn_failure_has_no_completion_and_legacy_shapes_remain_unchanged(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    plan, argv, options, result = observe(legacy)
    assert result["receipt"]["schema_version"] == "integration-cost-receipt/v1"
    assert "producer_completion" not in result["receipt"]
    intent = json.loads((options["directory"] / "selected.intent.json").read_bytes())
    start = json.loads((options["directory"] / "selected.start.json").read_bytes())
    assert "native_child_scope" not in intent and set(start) == {"intent_sha256", "started_at", "pid", "authentication"}
    assert read(plan, argv, options, result) == result
    with pytest.raises(ValueError, match="lifecycle_version"):
        read(plan, argv, options, result, native_child_scope=SCOPE)

    def fail(*args, **kwargs):
        raise OSError("spawn refused")

    monkeypatch.setattr(subprocess, "Popen", fail)
    failed = tmp_path / "failed"
    failed.mkdir()
    plan, argv, options, result = observe(failed, native_child_scope=SCOPE)
    assert result["receipt"]["producer_completion"] is None and result["receipt"]["started_at"] is None
    assert read(plan, argv, options, result, native_child_scope=SCOPE) == result
    checked = read(plan, argv, options, result, native_child_scope=SCOPE)
    with pytest.raises(ValueError, match="producer_completion_invalid"):
        observer.producer_cleanup_proof(checked, worker_pid=12345, **SCOPE)


@pytest.mark.parametrize("scope", [{}, {"plan_sha256": "a" * 64}, {**SCOPE, "extra": True},
                                  {**SCOPE, "stage": "unknown"}, {**SCOPE, "plan_sha256": "A" * 64}])
def test_partial_or_unknown_scope_never_dispatches(tmp_path, monkeypatch, scope):
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ValueError, match="producer_completion_invalid"):
        observe(tmp_path, native_child_scope=scope)


def test_actual_reaped_group_unknown_retains_failure_without_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(groups, "_group_alive", lambda process: None)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(observer, "stop_process", forbidden)
    plan, argv, options, result = observe(tmp_path, native_child_scope=SCOPE)
    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["failure"] == "cost_producer_cleanup_unconfirmed"
    assert result["receipt"]["producer_completion"] is None
    checked = read(plan, argv, options, result, native_child_scope=SCOPE)
    with pytest.raises(ValueError, match="producer_completion_invalid"):
        observer.producer_cleanup_proof(checked, worker_pid=12345, **SCOPE)
