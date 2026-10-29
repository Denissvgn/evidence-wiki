"""Original child completion proofs survive later reuse of numeric PIDs."""

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki import _process_groups as ownership
from evidence_wiki.agent_execution import BoundedSubprocessExecutor, _Running
from tools import _integration_owner_pass as groups
from tools import _review_dependency_workload as native

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Owned child receipts require POSIX process-group identity.")


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", subprocess.Popen)
    result = native.OwnedChildren(native.OriginalResponses(tmp_path / "originals"), "a" * 64)
    result.open()
    return result


def forbidden(*args, **kwargs):
    raise AssertionError("late numeric process operation")


def fake_child(pid=12345, returncode=0):
    return SimpleNamespace(pid=pid, returncode=returncode, _waitpid_lock=threading.Lock(),
        wait=lambda **kwargs: returncode, poll=forbidden)


def launch_fake(tracker, monkeypatch, child):
    monkeypatch.setattr(tracker, "real_popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(native, "os_getpgid", lambda pid: pid)
    return tracker.launch(["unused"], start_new_session=True)


def test_reaped_actual_child_receipt_prevents_all_late_process_operations(tracker, monkeypatch):
    process = subprocess.Popen([str(native.ROOT / ".venv/bin/python"), "-c", "pass"], start_new_session=True)
    assert process.wait(timeout=5) == 0
    tracker.confirm_completed()
    certificate = tracker._completed[id(process)]
    receipt = json.loads(Path(json.loads(certificate.completion_pin)["path"]).read_bytes())
    assert receipt["ordinal"] == 1 and receipt["plan_sha256"] == "a" * 64
    assert receipt["spawn_pin"] == {key: value for key, value in tracker.originals.pins[0].items() if key != "label"}
    assert receipt["pid"] == process.pid and receipt["returncode"] == 0
    assert receipt["leader_reaped"] is receipt["group_absent"] is True
    with pytest.raises(FrozenInstanceError):
        certificate.pid = 67890
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    monkeypatch.setattr(os, "getpgid", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(process, "wait", forbidden)
    monkeypatch.setattr(process, "poll", forbidden)
    tracker.confirm_completed()
    assert len(tracker.originals.pins) == 2
    result = tracker.close()
    assert result["children"][0]["completion_pin"] == json.loads(certificate.completion_pin)
    assert result["status"] == "confirmed"
    with pytest.raises(ValueError, match="launch_refused"):
        tracker.launch(["unused"], start_new_session=True)


@pytest.mark.parametrize("certify", [False, True])
def test_embedded_executor_watch_caches_actual_absence_for_tracker(tracker, monkeypatch, certify):
    probes = []
    original_probe = groups._group_alive
    def probe(process):
        assert type(process.returncode) is int
        probes.append(process)
        return original_probe(process)
    process = subprocess.Popen([str(native.ROOT / ".venv/bin/python"), "-c", "print('{}')"],
        stdout=subprocess.PIPE, start_new_session=True)
    guard = ownership.ownership_for(process)
    assert type(process) is native._POPEN_TYPE
    assert type(guard) is ownership.ProcessGroupOwnership
    monkeypatch.setattr(guard, "_group_alive", probe)
    running = _Running(None, process, None, time.monotonic(), 2)
    running.data.extend(process.stdout.read())
    process.stdout.close()
    executor = object.__new__(BoundedSubprocessExecutor)
    executor.profile = SimpleNamespace(cleanup_timeout_seconds=1)
    executor._meter = None
    executor._watch(running)
    assert running.finished.is_set() and running.snapshot["state"] == "succeeded"
    assert process.returncode == 0 and probes == [process]
    # A later occupant of this numeric PID cannot invalidate proven birth absence.
    monkeypatch.setattr(guard, "_group_alive", forbidden)
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    monkeypatch.setattr(os, "getpgid", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(process, "wait", forbidden)
    monkeypatch.setattr(process, "poll", forbidden)
    executor._kill(running)
    if certify:
        tracker.confirm_completed()
        certificate = tracker._completed[id(process)]
    else:
        assert not tracker._completed
    result = tracker.close()
    assert result["status"] == "confirmed"
    if certify:
        assert result["children"][0]["completion_pin"] == json.loads(certificate.completion_pin)
    else:
        assert result["children"][0]["group_absent"] is True
    executor._kill(running)


def test_marked_live_native_cleanup_signals_under_wait_lock(tracker, monkeypatch):
    process = subprocess.Popen([str(native.ROOT / ".venv/bin/python"), "-c",
        "import time; time.sleep(1.5)"], start_new_session=True)
    guard = ownership.ownership_for(process)
    original_getpgid, original_killpg = os.getpgid, os.killpg
    signals = []
    def identity(pid):
        assert process._waitpid_lock.locked() and process.returncode is None
        return original_getpgid(pid)
    def send(pid, selected_signal):
        if selected_signal:
            assert process._waitpid_lock.locked() and process.returncode is None
            signals.append(selected_signal)
        return original_killpg(pid, selected_signal)
    monkeypatch.setattr(os, "getpgid", identity)
    monkeypatch.setattr(os, "killpg", send)
    monkeypatch.setattr(process, "poll", forbidden)
    assert tracker.close()["status"] == "confirmed"
    assert signals == [signal.SIGTERM] and type(process.returncode) is int
    monkeypatch.setattr(guard, "_group_alive", forbidden)
    monkeypatch.setattr(os, "getpgid", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    assert guard.confirm_completed() is True
    BoundedSubprocessExecutor._kill(None, SimpleNamespace(process=process))


@pytest.mark.parametrize("marker", ["malformed", "transplanted", "missing"])
def test_native_tracker_refuses_changed_ownership_without_legacy_fallback(tracker, monkeypatch, marker):
    process = subprocess.Popen([str(native.ROOT / ".venv/bin/python"), "-c", "pass"], start_new_session=True)
    assert process.wait(timeout=2) == 0
    if marker == "missing":
        delattr(process, ownership._MARKER)
    elif marker == "malformed":
        setattr(process, ownership._MARKER, object())
    else:
        other = object.__new__(native._POPEN_TYPE)
        other._child_created = False
        other.pid = process.pid + 1
        setattr(process, ownership._MARKER, ownership.ProcessGroupOwnership(other, forbidden))
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    monkeypatch.setattr(os, "getpgid", forbidden)
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(process, "wait", forbidden)
    monkeypatch.setattr(process, "poll", forbidden)
    if marker != "missing":
        with pytest.raises(ownership.ProcessGroupOwnershipError):
            BoundedSubprocessExecutor._kill(None, SimpleNamespace(process=process))
    tracker.confirm_completed()
    assert not tracker._completed
    with pytest.raises(ValueError, match="cleanup_unconfirmed"):
        tracker.close()


def test_distinct_objects_with_same_pid_never_share_completion(tracker, monkeypatch):
    first = launch_fake(tracker, monkeypatch, fake_child())
    monkeypatch.setattr(groups, "_group_alive", lambda process: False)
    tracker.confirm_completed()
    second = launch_fake(tracker, monkeypatch, fake_child(pid=first.pid))
    probed = []
    monkeypatch.setattr(groups, "_group_alive", lambda process: probed.append(process) or True)
    tracker.confirm_completed()
    assert id(first) in tracker._completed and id(second) not in tracker._completed
    with pytest.raises(ValueError, match="cleanup_unconfirmed"):
        tracker.close()
    assert len(probed) == 2 and all(process is second for process in probed)


@pytest.mark.parametrize("proof", [True, None, "unknown", RuntimeError("unavailable")])
def test_present_or_unknown_group_never_certified_or_accepted(tracker, monkeypatch, proof):
    process = launch_fake(tracker, monkeypatch, fake_child(returncode=9))
    def observe(actual):
        assert actual is process
        if isinstance(proof, Exception):
            raise proof
        return proof
    monkeypatch.setattr(groups, "_group_alive", observe)
    tracker.confirm_completed()
    assert not tracker._completed
    with pytest.raises(ValueError, match="cleanup_unconfirmed"):
        tracker.close()
    assert not any(pin["label"].endswith("-completed") for pin in tracker.originals.pins)


def test_confirmation_never_reaps_and_can_retry_pending_absence(tracker, monkeypatch):
    process = launch_fake(tracker, monkeypatch, fake_child(returncode=None))
    monkeypatch.setattr(process, "wait", forbidden)
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    tracker.confirm_completed()
    assert not tracker._completed
    process.returncode = 0
    monkeypatch.setattr(groups, "_group_alive", lambda actual: True)
    tracker.confirm_completed()
    assert not tracker._completed
    monkeypatch.setattr(groups, "_group_alive", lambda actual: False)
    tracker.confirm_completed()
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    assert tracker.close()["status"] == "confirmed"


def test_missing_pid_cannot_establish_a_completion_or_cleanup(tracker, monkeypatch):
    process = launch_fake(tracker, monkeypatch, fake_child(pid=None))
    monkeypatch.setattr(groups, "_group_alive", forbidden)
    tracker.confirm_completed()
    assert not tracker._completed
    with pytest.raises(ValueError, match="cleanup_unconfirmed"):
        tracker.close()
    assert process.returncode == 0


def test_retained_children_remain_bounded_after_completion(tracker, monkeypatch):
    process = launch_fake(tracker, monkeypatch, fake_child())
    monkeypatch.setattr(groups, "_group_alive", lambda actual: False)
    tracker.confirm_completed()
    with tracker.lock:
        tracker.owned.extend(fake_child(pid=20000 + index) for index in range(4095))
    monkeypatch.setattr(tracker, "real_popen", forbidden)
    with pytest.raises(ValueError, match="launch_refused"):
        tracker.launch(["unused"], start_new_session=True)
    assert len(tracker.owned) == 4096 and tracker.owned[0] is process
