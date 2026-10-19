"""Marked process births cannot authorize signals after their leader is reaped."""

import os
import signal
import subprocess
import threading
import time

import pytest

from evidence_wiki import _process_groups as groups
from evidence_wiki.agent_execution import BoundedSubprocessExecutor, _Running
from tools import _integration_owner_pass as owners
from tools import _native_child_registry as registry


def forbidden(*args, **kwargs):
    pytest.fail("unexpected numeric process operation")


def process(*, pid=12345, returncode=0):
    actual = object.__new__(subprocess.Popen)
    actual._child_created = False
    actual.pid = pid
    actual.returncode = returncode
    actual._waitpid_lock = threading.Lock()
    actual.poll = forbidden
    actual.wait = lambda **kwargs: actual.returncode
    return actual


def running(actual):
    return _Running(None, actual, None, time.monotonic(), 10)


@pytest.mark.parametrize("present", [True, None, 0, "", PermissionError("unknown")])
def test_reaped_group_cannot_be_signalled_and_watch_reports_unknown(monkeypatch, present):
    actual = process()
    def probe(selected):
        assert selected is actual
        if isinstance(present, Exception):
            raise present
        return present
    groups.ProcessGroupOwnership(actual, probe)
    monkeypatch.setattr(groups.os, "getpgid", forbidden)
    monkeypatch.setattr(groups.os, "killpg", forbidden)
    executor = object.__new__(BoundedSubprocessExecutor)
    with pytest.raises(groups.ProcessGroupOwnershipError, match="process_group_ownership_unknown"):
        executor._kill(running(actual))
    observed = running(actual)
    executor._watch(observed)
    assert observed.finished.is_set()
    assert observed.snapshot["state"] == "unknown"
    assert observed.snapshot["reason"] == "executor_observation_unknown"
    assert observed.snapshot["final_accounting"] is False
    assert observed.snapshot["cancellation_confirmed"] is False


def test_original_absence_is_cached_across_core_and_owner_stop(monkeypatch):
    actual = process()
    probes = []
    guard = groups.ProcessGroupOwnership(actual, lambda selected: probes.append(selected) or False)
    executor = object.__new__(BoundedSubprocessExecutor)
    executor._kill(running(actual))
    assert probes == [actual]
    guard._group_alive = forbidden
    monkeypatch.setattr(groups.os, "getpgid", forbidden)
    monkeypatch.setattr(groups.os, "killpg", forbidden)
    executor._kill(running(actual))
    owners._stop_group(actual)
    assert guard.confirm_completed() is True


def test_live_signal_holds_wait_lock_and_never_polls(monkeypatch):
    actual = process(returncode=None)
    groups.ProcessGroupOwnership(actual, forbidden)
    signals = []
    def identity(pid):
        assert actual._waitpid_lock.locked()
        return pid
    def send(pid, selected_signal):
        assert actual._waitpid_lock.locked() and actual.returncode is None
        signals.append((pid, selected_signal))
    monkeypatch.setattr(groups.os, "getpgid", identity)
    monkeypatch.setattr(groups.os, "killpg", send)
    BoundedSubprocessExecutor._kill(None, running(actual))
    assert signals == [(actual.pid, signal.SIGKILL)]
    assert not actual._waitpid_lock.locked()


@pytest.mark.parametrize("change", ["pid", "marker", "other_process", "group"])
def test_identity_mismatch_refuses_core_and_owner_legacy_fallback(monkeypatch, change):
    actual = process(returncode=None)
    guard = groups.ProcessGroupOwnership(actual, forbidden)
    monkeypatch.setattr(groups.os, "getpgid", lambda pid: pid + (change == "group"))
    monkeypatch.setattr(groups.os, "killpg", forbidden)
    if change == "pid":
        actual.pid += 1
    elif change == "marker":
        setattr(actual, groups._MARKER, object())
    elif change == "other_process":
        other = process(returncode=None)
        setattr(other, groups._MARKER, guard)
        actual = other
    with pytest.raises(groups.ProcessGroupOwnershipError):
        BoundedSubprocessExecutor._kill(None, running(actual))
    with pytest.raises(groups.ProcessGroupOwnershipError):
        owners._stop_group(actual)


@pytest.mark.parametrize("after_term", [False, True])
def test_owner_stop_never_escalates_a_reaped_group(monkeypatch, after_term):
    actual = process(returncode=None if after_term else 0)
    groups.ProcessGroupOwnership(actual, lambda _: True)
    sent = []
    monkeypatch.setattr(groups.os, "getpgid", lambda pid: pid)
    def send(pid, selected_signal):
        assert actual.returncode is None and actual._waitpid_lock.locked()
        sent.append(selected_signal)
    monkeypatch.setattr(groups.os, "killpg", send)
    def wait(*, timeout):
        actual.returncode = 0
        raise subprocess.TimeoutExpired("owned-child", timeout)
    actual.wait = wait
    clock = iter((0, 6))
    monkeypatch.setattr(groups.time, "monotonic", lambda: next(clock))
    with pytest.raises(groups.ProcessGroupOwnershipError):
        owners._stop_group(actual)
    assert sent == ([signal.SIGTERM] if after_term else [])


def test_owner_stop_live_escalation_reaps_and_caches_absence(monkeypatch):
    actual = process(returncode=None)
    guard = groups.ProcessGroupOwnership(actual, lambda _: False)
    sent, waits = [], []
    monkeypatch.setattr(groups.os, "getpgid", lambda pid: pid)
    def send(pid, selected_signal):
        assert actual.returncode is None and actual._waitpid_lock.locked()
        sent.append(selected_signal)
    def wait(*, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            raise subprocess.TimeoutExpired("owned-child", timeout)
        actual.returncode = -9
        return -9
    monkeypatch.setattr(groups.os, "killpg", send)
    actual.wait = wait
    owners._stop_group(actual)
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert waits == [5, 5]
    guard._group_alive = forbidden
    owners._stop_group(actual)


def test_registry_marks_actual_object_before_exposure_and_reuses_cached_absence(tmp_path, monkeypatch):
    path = tmp_path / "children.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    tracked = registry.ChildTracker(descriptor, {})
    actual = process()
    tracked.real_popen = lambda *args, **kwargs: actual
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(registry, "_group_alive", lambda _: False)
    assert tracked.popen(["fixed"], start_new_session=True) is actual
    guard = groups.ownership_for(actual)
    assert type(guard) is groups.ProcessGroupOwnership
    BoundedSubprocessExecutor._kill(None, running(actual))
    guard._group_alive = forbidden
    monkeypatch.setattr(registry, "_group_alive", forbidden)
    monkeypatch.setattr(registry.os, "getpgid", forbidden)
    monkeypatch.setattr(registry.os, "killpg", forbidden)
    assert tracked.confirm_completed(actual)
    actual.wait = forbidden
    tracked.close(60)
    assert b'"group_absent":true' in path.read_bytes()
