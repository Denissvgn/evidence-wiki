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
from tests.test_normalization_control import patient_cancel_probe, stepped_clock


@pytest.fixture
def roots(tmp_path, monkeypatch):
    return root_fixture.__wrapped__(tmp_path, monkeypatch)


def control(*, seconds=10, cancelled=None):
    return OperationControl(deadline_utc=(datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
                            timeout_seconds=seconds, cancel_requested=cancelled)


def test_public_native_control_preserves_one_original_allowance_across_module_views(monkeypatch):
    normalized, helper = owner("normalize_sources"), owner("_operation_control")
    # A real 0.05 s budget raced the scheduler: native_control() checkpoints, and on a loaded
    # host that checkpoint could already fall past the budget. The clock now moves only here.
    clock = stepped_clock(helper, monkeypatch)
    budget, far = 0.05, 3600
    # Each case puts the other limit an hour away, so only the bound under test can stop the work.
    for bound in ("timeout", "deadline"):
        deadline = clock.utc + timedelta(seconds=budget if bound == "deadline" else far)
        value = OperationControl(deadline_utc=deadline.isoformat(),
                                 timeout_seconds=far if bound == "deadline" else budget)
        # Spend part of the allowance before the views exist, so a view that started a fresh
        # allowance would still be inside it when the original one has run out.
        clock.advance(0.03)
        first = native_control(value, normalized)
        second = value._for_owner(helper)
        assert first.deadline_utc == value.deadline_utc == second.deadline_utc, bound
        assert second.remaining_seconds() == pytest.approx(budget - 0.03), bound
        clock.advance(0.03, monotonic=0.0 if bound == "deadline" else None)
        for operation, arguments in ((value.checkpoint, ("later",)), (first.checkpoint, ("later",)),
                                     (second.remaining_seconds, ())):
            with pytest.raises(Exception) as error:
                operation(*arguments)
            assert error.value.error_code == "OPERATION_DEADLINE_EXCEEDED", bound


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_public_normalization_uses_actual_bounded_native_worker(roots):
    root, _ = roots
    context = HostContext([root], {"workspace.open", "normalize_sources.run_normalization_report"},
                          "caller", [root])
    with context.activate(), Workspace.open(root) as workspace:
        result = workspace.normalize.sources(all_sources=True, operation_control=control())
    assert result["summary"]["created"] == 1
    assert next((root / "sources/normalized").glob("*.md")).is_file()


def test_public_normalization_cancel_before_capture_preserves_source_bytes(roots, monkeypatch):
    root, _ = roots
    # The probe thread had 0.05 s to answer; see patient_cancel_probe.
    patient_cancel_probe(owner("_operation_control"), monkeypatch)
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
    # The probe thread had 0.05 s to answer; see patient_cancel_probe.
    patient_cancel_probe(owner("_operation_control"), monkeypatch)
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
    # Every checkpoint's probe thread had 0.05 s to answer (see patient_cancel_probe), and the
    # real native worker run had to fit 10 s; under load it took longer, so the budget is 60 s.
    patient_cancel_probe(owner("_operation_control"), monkeypatch)
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
        receipt = operations.submit(root, request, operation_control=control(seconds=60, cancelled=lambda: stopped))
        assert receipt["status"] == "commit_unknown" and receipt["error"]["error_code"] == "OPERATION_CANCELLED"
        assert next((root / "sources/normalized").glob("*.md")).is_file()
        assert not (root / "log.md").exists()
        # Reconciliation re-observes the workspace and the owner's outputs inside its budget; under load 10 s ran out.
        proof = operations.reconcile(root, "request", operation_control=control(seconds=60))
        assert proof["status"] == "partial" and not proof["desired_state_proved"]
        assert "log.md" in proof["owner_observation"]["missing_or_changed_outputs"]
    assert not (root / "log.md").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_last_moment_publication_callback_prevents_atomic_replace(roots, monkeypatch):
    root, _ = roots
    # Every checkpoint's probe thread had 0.05 s to answer (see patient_cancel_probe), and the
    # real native worker run had to fit 10 s; under load it took longer, so the budget is 60 s.
    patient_cancel_probe(owner("_operation_control"), monkeypatch)
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
        receipt = operations.submit(root, request, operation_control=control(seconds=60, cancelled=lambda: stopped))
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


def test_generic_direct_owner_cancelled_before_any_effect(roots, monkeypatch):
    root, _ = roots
    # The probe thread had 0.05 s to answer; see patient_cancel_probe.
    patient_cancel_probe(owner("_operation_control"), monkeypatch)
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
    release, entered, returned = threading.Event(), threading.Event(), threading.Event()
    calls = 0

    def stalled():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=60)
        returned.set()
        return False

    # The cutoff itself is the product's 0.05 s. What raced was the rest: the probe thread can
    # be scheduled after that cutoff on a loaded host, so it is awaited before the later
    # checkpoints, and "did not wait for the callback" is checked by `returned`, not by a 2 s
    # wall-clock bound on the whole submit.
    value = control(seconds=30, cancelled=stalled)
    try:
        with host(roots).activate():
            request = prepared(root, "normalize.sources", {"all_sources": True})
            started = time.monotonic()
            with pytest.raises(EvidenceWikiError) as error:
                operations.submit(root, request, operation_control=value)
            elapsed = time.monotonic() - started
            assert error.value.error_code == "OPERATION_CANCEL_UNKNOWN"
            assert entered.wait(timeout=10)
            for _ in range(3):
                with pytest.raises(EvidenceWikiError) as error:
                    value.checkpoint("later")
                assert error.value.error_code == "OPERATION_CANCEL_UNKNOWN"
        assert not returned.is_set() and elapsed < 10
        assert calls == 1 and not (root / "sources/normalized").exists()
    finally:
        release.set()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_cancel_observer_stall_stops_an_already_running_native_worker(tmp_path, monkeypatch):
    helper = owner("_operation_control")
    pid = tmp_path / "worker-pid"
    release, entered, returned = threading.Event(), threading.Event(), threading.Event()

    def stalled_after_start():
        if pid.exists():
            entered.set()
            release.wait(timeout=60)
            returned.set()
        return False

    # Every checkpoint before the worker wrote its pid had to be answered within 0.05 s, the
    # first one outside the run, and on a loaded host the probe thread answered later. Those
    # waits are now patient until the callback starts to stall; from then on the product's own
    # timeout cuts the stalled probe off. The 2 s budget and bound also had to cover starting a
    # real worker; "did not wait for the callback" is checked by `returned` instead.
    patient_cancel_probe(helper, monkeypatch, until=entered.is_set)
    value = control(seconds=30, cancelled=stalled_after_start)
    adapter = native_control(value, helper)
    # The callback stalls as soon as the pid file exists, and the stop can land between creating a
    # file and writing it, so the worker renames a complete file into place.
    command = [sys.executable, "-c",
               f"import os,pathlib,time;t=pathlib.Path({str(pid) + '.tmp'!r});t.write_text(str(os.getpid()));"
               f"os.replace(t,{str(pid)!r});time.sleep(60)"]
    started = time.monotonic()
    try:
        with pytest.raises(Exception) as error:
            adapter.run(command)
        assert error.value.error_code == "OPERATION_CANCEL_UNKNOWN"
        assert not returned.is_set() and time.monotonic() - started < 10 and pid.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid.read_text()), 0)
    finally:
        release.set()
