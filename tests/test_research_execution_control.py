"""Finite host waits preserve uncertain effects and bounded callback capacity."""

import threading
import time

import pytest

from evidence_wiki import research_execution as execution


@pytest.mark.parametrize("timeout", [0, -1])
def test_expired_host_wait_does_not_start_callback(timeout):
    calls = []
    assert execution.bounded_host_call(lambda: calls.append(True), (), timeout) == (None, "host_wait_expired")
    assert not calls


def test_saturated_host_capacity_refuses_without_dispatch(monkeypatch):
    monkeypatch.setattr(execution, "_HOST_CALL_CAPACITY", threading.BoundedSemaphore(0))
    calls = []
    assert execution.bounded_host_call(lambda: calls.append(True), (), 1) == (None, "host_observation_capacity_exhausted")
    assert not calls


def test_timeout_keeps_capacity_occupied_until_actual_callback_exit(monkeypatch):
    capacity = threading.BoundedSemaphore(1)
    monkeypatch.setattr(execution, "_HOST_CALL_CAPACITY", capacity)
    release, finished = threading.Event(), threading.Event()
    effects = []

    def callback():
        release.wait(5)
        effects.append("late effect")
        finished.set()
        return "late result"

    start = time.monotonic()
    try:
        assert execution.bounded_host_call(callback, (), 0.02) == (None, "host_wait_expired")
        assert time.monotonic() - start < 1
        assert not effects
        assert execution.bounded_host_call(lambda: "new work", (), 1) == (None, "host_observation_capacity_exhausted")
    finally:
        release.set()
        assert finished.wait(2)
    assert effects == ["late effect"]
    assert capacity.acquire(timeout=2)
    capacity.release()
    assert execution.bounded_host_call(lambda: "fresh result", (), 1) == ("fresh result", None)


def test_callback_failure_releases_capacity(monkeypatch):
    monkeypatch.setattr(execution, "_HOST_CALL_CAPACITY", threading.BoundedSemaphore(1))

    def failure():
        raise ValueError("host failure")

    assert execution.bounded_host_call(failure, (), 1) == (None, "host_observation_failed")
    assert execution.bounded_host_call(lambda: "fresh result", (), 1) == ("fresh result", None)


def test_thread_start_failure_releases_capacity(monkeypatch):
    capacity = threading.BoundedSemaphore(1)
    monkeypatch.setattr(execution, "_HOST_CALL_CAPACITY", capacity)

    def failure(self):
        raise RuntimeError("unavailable host thread")

    monkeypatch.setattr(execution.threading.Thread, "start", failure)
    assert execution.bounded_host_call(lambda: "never called", (), 1) == (None, "host_observation_failed")
    assert capacity.acquire(blocking=False)
    capacity.release()
