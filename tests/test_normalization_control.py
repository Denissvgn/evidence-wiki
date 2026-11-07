"""Native deadlines stop real workers and preserve canonical publication ownership."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests._script_loader import load_module
from tests.test_source_owner_api import SCRIPTS, workspace


@pytest.fixture
def owners():
    from tests._usage_fixture import require_bounded_inventory
    require_bounded_inventory()
    return (load_module("inventory_control", SCRIPTS / "source_inventory.py"),
            load_module("normalization_control", SCRIPTS / "normalize_sources.py"))


def control(normalizer, *, seconds=10, cancel=None):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    return normalizer.OperationControl(deadline_utc=deadline, timeout_seconds=seconds,
                                       cancel_requested=cancel)


def patient_cancel_probe(normalizer, monkeypatch, seconds=10.0, idle_seconds=None, until=None):
    """Give the real cancellation probe ``seconds`` to answer instead of the product's 0.05 s.

    ``OperationControl._observe_cancellation`` allows 0.05 s, including the time to start
    its probe thread. On a loaded host that thread can be scheduled later, so a host that
    did answer is reported as OPERATION_CANCEL_UNKNOWN. The probe stays a real thread that
    calls the real callback and answers through a real queue; only the queue's timed waits
    are widened. Returns the timeouts the product asked for, so a test can still check the
    0.05 s itself. The stalled-probe tests in test_mutation_operation_control.py check that a
    blocked probe is cut off.

    The probe thread itself waits for the next request for the control's remaining budget.
    Under ``stepped_clock`` that budget stops moving, so real work between two checkpoints
    can outlast the widened wait and the idle probe exits. ``idle_seconds`` (default
    ``seconds``) widens only that wait; the checkpoint's wait for an answer stays ``seconds``.

    ``until`` is for a test whose callback stalls on purpose. A checkpoint first waits the
    product's own timeout; after that it keeps waiting only while ``until()`` is false, so
    once the callback has started to block, the product's refusal path cuts it off again.
    """
    requested = []
    idle = seconds if idle_seconds is None else idle_seconds

    class PatientQueue(queue.Queue):
        def get(self, block=True, timeout=None):
            requested.append(timeout)
            if timeout is None:
                return super().get(block, None)
            if threading.current_thread().name == "evidence-operation-cancellation":
                return super().get(block, max(timeout, idle))
            if until is None:
                return super().get(block, max(timeout, seconds))
            end = time.monotonic() + max(timeout, seconds)
            try:
                return super().get(block, timeout)
            except queue.Empty:
                pass
            while not until():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    return super().get(block, min(0.01, remaining))
                except queue.Empty:
                    pass
            raise queue.Empty

    names = normalizer.OperationControl.checkpoint.__globals__
    monkeypatch.setitem(names, "queue", SimpleNamespace(Queue=PatientQueue, Empty=queue.Empty, Full=queue.Full))
    return requested


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_controlled_native_extraction_publishes_only_through_parent(tmp_path, owners):
    inventory, normalizer = owners
    root = workspace(tmp_path / "workspace")
    inventory.run_inventory_report(root)
    result = normalizer.run_normalization_report(root, all_sources=True,
        operation_control=control(normalizer))
    assert result["summary"]["created"] == 1
    assert result["actions"][0]["status"] == "content_extracted"
    output = root / result["actions"][0]["output"]
    assert output.is_file() and "Observed content." in output.read_text()


def test_cancel_before_capture_creates_no_normalized_outputs(tmp_path, owners, monkeypatch):
    inventory, normalizer = owners
    # The clock stands still, so the wait the product asks for is its whole allowance.
    clock = stepped_clock(normalizer, monkeypatch)
    requested = patient_cancel_probe(normalizer, monkeypatch)
    root = workspace(tmp_path / "workspace")
    inventory.run_inventory_report(root)
    deadline = (clock.utc + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    with pytest.raises(Exception) as error:
        normalizer.run_normalization_report(root, all_sources=True,
            operation_control=normalizer.OperationControl(deadline_utc=deadline, timeout_seconds=10,
                                                          cancel_requested=lambda: True))
    assert error.value.error_code == "OPERATION_CANCELLED"
    assert not (root / "sources" / "normalized").exists()
    # The host still had only the product's 0.05 s to answer; the test widened the wait, not the allowance.
    assert min(requested) == pytest.approx(0.05)


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_cancel_after_extraction_is_not_swallowed_as_failed_source(tmp_path, owners, monkeypatch):
    inventory, normalizer = owners
    patient_cancel_probe(normalizer, monkeypatch)
    root = workspace(tmp_path / "workspace")
    inventory.run_inventory_report(root)
    cancelled = False

    class CancelAfterWorker(normalizer.OperationControl):
        def run(self, command):
            nonlocal cancelled
            result = super().run(command)
            cancelled = True
            return result

    deadline = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    operation = CancelAfterWorker(deadline_utc=deadline, timeout_seconds=10,
                                 cancel_requested=lambda: cancelled)
    with pytest.raises(Exception) as error:
        normalizer.run_normalization_report(root, all_sources=True, operation_control=operation)
    assert error.value.error_code == "OPERATION_CANCELLED"
    assert not (root / "sources" / "normalized").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_deadline_stops_real_noncooperative_worker(tmp_path, owners):
    _, normalizer = owners
    pid = tmp_path / "pid"
    worker = tmp_path / "worker.py"
    worker.write_text("import os, pathlib, time\npathlib.Path(" + repr(str(pid))
                      + ").write_text(str(os.getpid()))\ntime.sleep(30)\n")
    # The deadline has to fall after the child Python has started and written its pid. Under
    # 36 busy loops with pytest clamped to utility QoS, that took 0.19-0.24 s at the median,
    # 0.6-0.7 s at p99 and once 2.99 s in 1,100 starts, so 0.25 s lost about half the runs.
    budget = 5
    operation = control(normalizer, seconds=budget)
    started = time.monotonic()
    with pytest.raises(Exception) as error:
        operation.run([sys.executable, str(worker)])
    assert error.value.error_code == "OPERATION_DEADLINE_EXCEEDED"
    # The same 1.75 s past the deadline as before: stopping the group is bounded, not the 30 s sleep.
    assert time.monotonic() - started < budget + 1.75
    assert pid.is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_pending_local_cancel_stops_real_worker(tmp_path, owners, monkeypatch):
    _, normalizer = owners
    patient_cancel_probe(normalizer, monkeypatch)
    started = time.monotonic()
    operation = control(normalizer, cancel=lambda: time.monotonic() - started >= 0.1)
    worker = tmp_path / "worker.py"
    worker.write_text("import time\ntime.sleep(30)\n")
    with pytest.raises(Exception) as error:
        operation.run([sys.executable, str(worker)])
    assert error.value.error_code == "OPERATION_CANCELLED"
    assert time.monotonic() - started < 2


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_staged_controlled_plan_does_not_publish(tmp_path, owners):
    inventory, normalizer = owners
    root = workspace(tmp_path / "workspace")
    inventory.run_inventory_report(root)
    arguments = dict(source_ids=None, all_sources=True, dry_run=False, force=False,
        append_log=False, pdf_extractor=None, max_sources=128, allow_external=False,
        max_manifest_bytes=1_048_576, max_manifest_records=1000,
        max_entries=10_000, max_input_bytes=16_777_216)
    prepared = normalizer.prepare_normalization_effects(root, arguments,
        observed_utc=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        operation_control=control(normalizer))
    assert prepared["report"]["summary"]["created"] == 1
    assert any(effect["data"] and b"Observed content." in effect["data"] for effect in prepared["effects"])
    assert not (root / "sources" / "normalized").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_cancel_after_one_publication_preserves_observable_effect(tmp_path, owners):
    inventory, normalizer = owners
    root = workspace(tmp_path / "workspace", count=2)
    inventory.run_inventory_report(root)

    class CancelSecondExtraction(normalizer.OperationControl):
        count = 0

        def checkpoint(self, name):
            if name == "before_extraction":
                self.count += 1
                if self.count == 2:
                    self._refuse("OPERATION_CANCELLED", name)
            super().checkpoint(name)

    deadline = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    with pytest.raises(Exception) as error:
        normalizer.run_normalization_report(root, all_sources=True,
            operation_control=CancelSecondExtraction(deadline_utc=deadline, timeout_seconds=10))
    assert error.value.error_code == "OPERATION_CANCELLED"
    outputs = list((root / "sources" / "normalized").glob("*.md"))
    assert len(outputs) == 1 and "Observed content." in outputs[0].read_text()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_worker_environment_excludes_host_credentials(tmp_path, owners, monkeypatch):
    _, normalizer = owners
    monkeypatch.setenv("EVIDENCE_HOST_SECRET", "local-test-value")
    result = tmp_path / "environment"
    worker = tmp_path / "worker.py"
    worker.write_text("import os, pathlib\npathlib.Path(" + repr(str(result))
                      + ").write_text(str(os.environ.get('EVIDENCE_HOST_SECRET')))\n")
    assert control(normalizer).run([sys.executable, str(worker)]) == 0
    assert result.read_text() == "None"


class SteppedClock:
    """Monotonic and UTC wall time that move only when the test advances them.

    Real sleeps made the budget race the scheduler: on a loaded host a 0.04 s
    sleep can overshoot a 0.08 s allowance before the second snapshot runs.
    """

    def __init__(self):
        self.utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.monotonic_seconds = 1_000.0

    def monotonic(self):
        return self.monotonic_seconds

    def advance(self, seconds, *, monotonic=None):
        """Move both clocks by ``seconds``, or the monotonic one by ``monotonic`` instead."""
        self.utc += timedelta(seconds=seconds)
        self.monotonic_seconds += seconds if monotonic is None else monotonic


def stepped_clock(normalizer, monkeypatch):
    """Point the ``time`` and ``datetime`` names ``OperationControl`` reads at one clock."""
    clock = SteppedClock()

    class SteppedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return clock.utc

    names = normalizer.OperationControl.checkpoint.__globals__
    monkeypatch.setitem(names, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setitem(names, "datetime", SteppedDatetime)
    return clock


def test_fixed_timeout_cannot_be_renewed_by_snapshot(owners, monkeypatch):
    _, normalizer = owners
    clock = stepped_clock(normalizer, monkeypatch)
    budget, far = 0.08, 3600
    # Each case puts the other limit an hour away, so only the bound under test can stop the work.
    for bound in ("timeout", "deadline"):
        deadline = clock.utc + timedelta(seconds=budget if bound == "deadline" else far)
        operation = normalizer.OperationControl(
            deadline_utc=deadline.isoformat().replace("+00:00", "Z"),
            timeout_seconds=far if bound == "deadline" else budget)
        original = operation.snapshot()
        assert original["remaining_seconds"] == pytest.approx(budget), bound
        clock.advance(0.04)
        later = operation.snapshot()
        assert later["deadline_utc"] == original["deadline_utc"], bound
        assert later["remaining_seconds"] < original["remaining_seconds"], bound
        # No fresh allowance: the snapshot spent exactly the elapsed time and refunded none.
        assert later["remaining_seconds"] == pytest.approx(budget - 0.04), bound
        # The UTC deadline still binds when the wall clock runs ahead of the monotonic one,
        # as it does across a host suspend or a forward clock correction.
        clock.advance(0.05, monotonic=0.0 if bound == "deadline" else None)
        with pytest.raises(Exception) as error:
            operation.snapshot()
        assert error.value.error_code == "OPERATION_DEADLINE_EXCEEDED", bound


@pytest.mark.parametrize("cancel", [lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("lost"))])
def test_unknown_cancel_observation_does_not_continue(owners, cancel):
    _, normalizer = owners
    with pytest.raises(Exception) as error:
        control(normalizer, cancel=cancel).checkpoint("capture")
    assert error.value.error_code == "OPERATION_CANCEL_UNKNOWN"


def test_native_worker_refuses_unsupported_process_groups_before_dispatch(owners, monkeypatch):
    from types import SimpleNamespace

    _, normalizer = owners
    operation = control(normalizer)
    monkeypatch.setitem(operation.run.__func__.__globals__, "os", SimpleNamespace(name="nt"))
    calls = []
    monkeypatch.setattr(operation.run.__func__.__globals__["subprocess"], "Popen", lambda *a, **k: calls.append(True))
    with pytest.raises(Exception) as error:
        operation.run([sys.executable, "-c", "raise RuntimeError('must not run')"])
    assert error.value.error_code == "OPERATION_PROCESS_BOUNDARY_UNSUPPORTED"
    assert not calls
