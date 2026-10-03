"""Native deadlines stop real workers and preserve canonical publication ownership."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests._script_loader import load_module
from tests.test_source_owner_api import SCRIPTS, workspace


@pytest.fixture
def owners():
    return (load_module("inventory_control", SCRIPTS / "source_inventory.py"),
            load_module("normalization_control", SCRIPTS / "normalize_sources.py"))


def control(normalizer, *, seconds=10, cancel=None):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    return normalizer.OperationControl(deadline_utc=deadline, timeout_seconds=seconds,
                                       cancel_requested=cancel)


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


def test_cancel_before_capture_creates_no_normalized_outputs(tmp_path, owners):
    inventory, normalizer = owners
    root = workspace(tmp_path / "workspace")
    inventory.run_inventory_report(root)
    with pytest.raises(Exception) as error:
        normalizer.run_normalization_report(root, all_sources=True,
            operation_control=control(normalizer, cancel=lambda: True))
    assert error.value.error_code == "OPERATION_CANCELLED"
    assert not (root / "sources" / "normalized").exists()


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_cancel_after_extraction_is_not_swallowed_as_failed_source(tmp_path, owners):
    inventory, normalizer = owners
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
    operation = control(normalizer, seconds=0.25)
    started = time.monotonic()
    with pytest.raises(Exception) as error:
        operation.run([sys.executable, str(worker)])
    assert error.value.error_code == "OPERATION_DEADLINE_EXCEEDED"
    assert time.monotonic() - started < 2
    assert pid.is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.mark.skipif(os.name != "posix", reason="Native worker control requires POSIX process groups.")
def test_pending_local_cancel_stops_real_worker(tmp_path, owners):
    _, normalizer = owners
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


def test_fixed_timeout_cannot_be_renewed_by_snapshot(tmp_path, owners):
    _, normalizer = owners
    operation = control(normalizer, seconds=0.08)
    original = operation.snapshot()
    time.sleep(0.04)
    later = operation.snapshot()
    assert later["deadline_utc"] == original["deadline_utc"]
    assert later["remaining_seconds"] < original["remaining_seconds"]
    time.sleep(0.05)
    with pytest.raises(Exception) as error:
        operation.snapshot()
    assert error.value.error_code == "OPERATION_DEADLINE_EXCEEDED"


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
