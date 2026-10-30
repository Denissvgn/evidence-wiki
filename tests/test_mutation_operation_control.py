"""Shared deadlines at public normalization and staged mutation boundaries."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import Workspace
from evidence_wiki import mutation_operations as operations
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.host_context import HostContext
from evidence_wiki.operation_control import OperationControl, native_control
from tests.test_mutation_operations import host, owner, prepared
from tests.test_mutation_operations import roots as root_fixture


@pytest.fixture
def roots(tmp_path, monkeypatch):
    return root_fixture.__wrapped__(tmp_path, monkeypatch)


def control(*, seconds=10, cancelled=None):
    return OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
                            timeout_seconds=seconds, cancel_requested=cancelled)


def test_public_native_control_preserves_one_original_allowance_across_module_views():
    normalized, helper = owner("normalize_sources"), owner("_operation_control")
    value = control(seconds=0.05)
    first = native_control(value, normalized)
    second = value._for_owner(helper)
    assert first.deadline_utc == value.deadline_utc == second.deadline_utc
    time.sleep(0.06)
    for operation in (lambda: value.checkpoint("later"), lambda: first.checkpoint("later"),
                      lambda: second.remaining_seconds()):
        with pytest.raises(Exception) as error:
            operation()
        assert error.value.error_code == "OPERATION_DEADLINE_EXCEEDED"


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_public_normalization_uses_actual_bounded_native_worker(roots):
    root, _ = roots
    context = HostContext([root], {"workspace.open", "normalize_sources.run_normalization_report"},
                          "caller", [root])
    with context.activate(), Workspace.open(root) as workspace:
        result = workspace.normalize.sources(all_sources=True, operation_control=control())
    assert result["summary"]["created"] == 1
    assert next((root / "sources/normalized").glob("*.md")).is_file()


def test_public_normalization_cancel_before_capture_preserves_source_bytes(roots):
    root, _ = roots
    before = (root / "raw/web/source.html").read_bytes()
    context = HostContext([root], {"workspace.open", "normalize_sources.run_normalization_report"}, "caller", [root])
    with context.activate(), Workspace.open(root) as workspace, pytest.raises(EvidenceWikiError) as error:
        workspace.normalize.sources(all_sources=True, operation_control=control(cancelled=lambda: True))
    assert error.value.error_code == "OPERATION_CANCELLED"
    assert before == (root / "raw/web/source.html").read_bytes()
    assert not (root / "sources/normalized").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_canonical_normalization_forwards_control_and_excludes_callbacks_from_intent(roots):
    root, _ = roots
    with host(roots).activate(), Workspace.open(root) as workspace:
        request = prepared(root, "normalize.sources", {"all_sources": True})
        result = workspace.mutations.submit(request, operation_control=control())
        stored = operations.inspect(root, "request")
    assert result["status"] == "committed" and result["result"]["summary"]["created"] == 1
    raw = json.dumps(stored["record"]["intent"])
    assert "operation_control" not in raw and "cancel_requested" not in raw and "_native" not in raw


def test_canonical_cancellation_before_capture_never_dispatches(roots, monkeypatch):
    root, _ = roots
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True})
        monkeypatch.setattr(operations, "_capture", lambda *a, **k: pytest.fail("Capture after cancellation"))
        with pytest.raises(EvidenceWikiError) as error:
            operations.submit(root, request, operation_control=control(cancelled=lambda: True))
    assert error.value.error_code == "OPERATION_CANCELLED"
    assert not (root / "sources/normalized").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_staged_publisher_cancellation_preserves_partial_effects_without_rollback(roots, monkeypatch):
    root, _ = roots
    stopped = False
    publisher = owner("_usage_materialization")
    original = publisher.publish_file

    def publish(*args, **kwargs):
        nonlocal stopped
        result = original(*args, **kwargs)
        stopped = True
        return result

    monkeypatch.setattr(publisher, "publish_file", publish)
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True, "append_log": True})
        receipt = operations.submit(root, request, operation_control=control(cancelled=lambda: stopped))
        assert receipt["status"] == "commit_unknown" and receipt["error"]["error_code"] == "OPERATION_CANCELLED"
        assert next((root / "sources/normalized").glob("*.md")).is_file()
        assert not (root / "log.md").exists()
        proof = operations.reconcile(root, "request", operation_control=control())
        assert proof["status"] == "partial" and not proof["desired_state_proved"]
        assert "log.md" in proof["owner_observation"]["missing_or_changed_outputs"]
    assert not (root / "log.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_last_moment_publication_callback_prevents_atomic_replace(roots, monkeypatch):
    root, _ = roots
    stopped = False
    publisher = owner("_usage_materialization")
    original = publisher.publish_file

    def publish(*args, **kwargs):
        nonlocal stopped
        stopped = True
        assert "before_publish" in kwargs
        return original(*args, **kwargs)

    monkeypatch.setattr(publisher, "publish_file", publish)
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True})
        receipt = operations.submit(root, request, operation_control=control(cancelled=lambda: stopped))
    assert receipt["status"] == "commit_unknown" and receipt["error"]["error_code"] == "OPERATION_CANCELLED"
    assert not list((root / "sources/normalized").glob("*.md"))


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_completed_write_lost_return_reconciliation_does_not_repeat_extraction(roots, monkeypatch):
    root, _ = roots
    original = operations._invoke

    def lost_return(*args, **kwargs):
        value = original(*args, **kwargs)
        assert value["summary"]["created"] == 1
        raise KeyboardInterrupt

    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True})
        monkeypatch.setattr(operations, "_invoke", lost_return)
        with pytest.raises(KeyboardInterrupt):
            operations.submit(root, request, operation_control=control())
        monkeypatch.setattr(owner("normalize_sources"), "_controlled_normalize_source",
                            lambda *a, **k: pytest.fail("Extraction repeated during proof"))
        proof = operations.reconcile(root, "request", operation_control=control())
    assert proof["status"] == "reconciled" and proof["desired_state_proved"]
    assert proof["owner_observation"]["original_execution_timing"] == "unknown"


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_historical_completed_replay_precedes_new_control_deadline(roots):
    root, _ = roots
    with host(roots).activate(), Workspace.open(root) as workspace:
        request = prepared(root, "normalize.sources", {"all_sources": True})
        result = workspace.mutations.submit(request, operation_control=control())
        expired = OperationControl(deadline_utc="2020-01-01T00:00:00Z", timeout_seconds=1)
        assert workspace.mutations.submit(request, operation_control=expired) == result
        assert workspace.mutations.reconcile("request", operation_control=expired) == result


def test_generic_direct_owner_cancelled_before_any_effect(roots):
    root, _ = roots
    with host(roots).activate():
        request = prepared(root)
        with pytest.raises(EvidenceWikiError) as error:
            operations.submit(root, request, operation_control=control(cancelled=lambda: True))
    assert error.value.error_code == "OPERATION_CANCELLED"
    assert not (root / "sources/coverage/sample.yml").exists()


def test_unknown_public_control_refused_before_native_work(roots):
    root, _ = roots
    with host(roots).activate():
        request = prepared(root, "normalize.sources", {"all_sources": True})
        with pytest.raises(EvidenceWikiError) as error:
            operations.submit(root, request, operation_control={"deadline_utc": "claimed"})
    assert error.value.error_code == "OPERATION_CONTROL_INVALID"
    assert not (root / "sources/normalized").exists()


def test_stalled_cancel_probe_is_bounded_unknown_and_never_restarted(roots):
    root, _ = roots
    release = threading.Event()
    calls = 0

    def stalled():
        nonlocal calls
        calls += 1
        release.wait(timeout=3)
        return False

    value = control(cancelled=stalled)
    started = time.monotonic()
    try:
        with host(roots).activate():
            request = prepared(root, "normalize.sources", {"all_sources": True})
            with pytest.raises(EvidenceWikiError) as error:
                operations.submit(root, request, operation_control=value)
            assert error.value.error_code == "OPERATION_CANCEL_UNKNOWN"
            for _ in range(3):
                with pytest.raises(EvidenceWikiError) as error:
                    value.checkpoint("later")
                assert error.value.error_code == "OPERATION_CANCEL_UNKNOWN"
        assert time.monotonic() - started < 2
        assert calls == 1 and not (root / "sources/normalized").exists()
    finally:
        release.set()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_cancel_observer_stall_stops_an_already_running_native_worker(tmp_path):
    helper = owner("_operation_control")
    pid = tmp_path / "worker-pid"
    release = threading.Event()

    def stalled_after_start():
        if pid.exists():
            release.wait(timeout=3)
        return False

    value = control(seconds=2, cancelled=stalled_after_start)
    adapter = native_control(value, helper)
    command = [sys.executable, "-c",
               f"import os,pathlib,time;pathlib.Path({str(pid)!r}).write_text(str(os.getpid()));time.sleep(30)"]
    started = time.monotonic()
    try:
        with pytest.raises(Exception) as error:
            adapter.run(command)
        assert error.value.error_code == "OPERATION_CANCEL_UNKNOWN"
        assert time.monotonic() - started < 2 and pid.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid.read_text()), 0)
    finally:
        release.set()
