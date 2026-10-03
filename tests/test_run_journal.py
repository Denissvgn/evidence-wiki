"""Native journal barriers, bounded captures and canonical fault recovery."""

from __future__ import annotations

import errno
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki.host_context import HostContext
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.telemetry_contracts import JOURNAL, validate_document
from tests._script_loader import load_module
from tests.test_run_controller import RunControllerTests
from tests.test_telemetry_contracts import event_document

SCRIPTS = Path(__file__).resolve().parents[1] / "workspace-template/scripts"


@pytest.fixture
def journal(tmp_path):
    helper = RunControllerTests()
    root = helper.init_workspace(tmp_path).resolve()
    controller = load_module("bounded_run_journal", SCRIPTS / "run_controller.py")
    args = controller.parse_args(["--project-root", str(root), "start", "--run-id", "trace", "--agent-id", "worker"])
    controller.run_start(root, args)
    return controller, root


def record(controller, root):
    return controller.run_telemetry_record(root, run_id="trace", agent_id="worker", event=event_document())


def artifacts(controller, root):
    return (controller.run_state_path(root, "trace").read_bytes(), controller.events_path(root, "trace").read_bytes())


def recover(controller, root):
    return controller.run_recover(root, SimpleNamespace(run_id="trace", agent_id="worker"))


def test_canonical_event_shape_durable_barriers_and_exact_logical_replay(journal, monkeypatch):
    controller, root = journal
    original = controller._run_fsync
    barriers = []
    def observed(descriptor, boundary):
        barriers.append((boundary, stat.S_ISDIR(controller._native_os.fstat(descriptor).st_mode)))
        return original(descriptor, boundary)
    monkeypatch.setattr(controller, "_run_fsync", observed)
    retained = record(controller, root)
    assert set(retained) == {"schema_version", "event_id", "run_id", "occurred_at", "agent_id",
        "event_type", "from_state", "to_state", "message", "data"}
    assert retained["event_type"] == controller.TELEMETRY_EVENT_TYPE
    assert retained["data"]["event"] == event_document()
    phases = [(boundary, directory) for boundary, directory in barriers if boundary != "custody.directory"]
    assert phases == [("run_state_pending.file", False), ("run_state_pending.directory", True),
        ("event_log.file", False), ("event_log.directory", True),
        ("run_state_committed.file", False), ("run_state_committed.directory", True)]
    before = artifacts(controller, root)
    barriers.clear()
    assert record(controller, root) == retained
    assert artifacts(controller, root) == before
    assert barriers == [("replay.file", False), ("replay.directory", True),
                        ("replay.file", False), ("replay.directory", True)]
    changed = event_document()
    changed["metrics"][0]["value"] = "2"
    with pytest.raises(controller.RunControllerError) as error:
        controller.run_telemetry_record(root, run_id="trace", agent_id="worker", event=changed)
    assert error.value.error_code == "RUN_EVENT_ID_CONFLICT"
    assert artifacts(controller, root) == before
    snapshot = controller.run_telemetry_events(root, run_id="trace")
    assert validate_document(JOURNAL, snapshot) == snapshot
    assert snapshot["count"] == 2 and not snapshot["evidence_accepted"]


@pytest.mark.parametrize("failure", [
    "run_state_pending.file", "run_state_pending.directory", "event_log.file",
    "event_log.directory", "run_state_committed.file", "run_state_committed.directory",
])
def test_every_flush_failure_preserves_inspectable_state_and_recovers_without_duplicate(journal, monkeypatch, failure):
    controller, root = journal
    before = artifacts(controller, root)
    protected = root / "raw/preserved.txt"
    protected.parent.mkdir(exist_ok=True)
    protected.write_bytes(b"Retained original.\r\n")
    original = controller._run_fsync
    triggered = False
    def interrupted(descriptor, boundary):
        nonlocal triggered
        if boundary == failure and not triggered:
            triggered = True
            raise OSError(errno.EIO, "injected flush failure")
        return original(descriptor, boundary)
    monkeypatch.setattr(controller, "_run_fsync", interrupted)
    with pytest.raises(controller.RunControllerError) as error:
        record(controller, root)
    assert triggered and error.value.error_code == "RUN_MUTATION_WRITE_FAILED"
    if failure == "run_state_pending.file":
        assert artifacts(controller, root) == before
    elif failure != "run_state_committed.directory":
        with pytest.raises(controller.RunControllerError) as pending:
            controller.load_run_state(root, "trace")
        assert pending.value.error_code == "RUN_MUTATION_RECOVERY_REQUIRED"
    monkeypatch.setattr(controller, "_run_fsync", original)
    cold = load_module("recovered_run_journal", SCRIPTS / "run_controller.py")
    recovered = recover(cold, root)
    assert "_pending_event" not in recovered and protected.read_bytes() == b"Retained original.\r\n"
    retained = record(cold, root)
    events = cold.load_events(cold.events_path(root, "trace"))
    observations = [row for row in events if row.get("event_type") == cold.TELEMETRY_EVENT_TYPE]
    assert observations == [retained]
    before_replay = artifacts(cold, root)
    assert record(cold, root) == retained
    assert artifacts(cold, root) == before_replay


@pytest.mark.parametrize("destination", ["run-state.json", "events.jsonl"])
def test_native_replace_failure_retains_previous_generation_or_pending_commit(journal, monkeypatch, destination):
    controller, root = journal
    before = artifacts(controller, root)
    original = controller._replace_run_file
    def interrupted(directory, temporary, name):
        if name == destination:
            raise OSError(errno.ENOSPC, "injected publication failure")
        return original(directory, temporary, name)
    monkeypatch.setattr(controller, "_replace_run_file", interrupted)
    with pytest.raises(controller.RunControllerError):
        record(controller, root)
    if destination == "run-state.json":
        assert artifacts(controller, root) == before
    else:
        assert controller.events_path(root, "trace").read_bytes() == before[1]
        assert "_pending_event" in json.loads(controller.run_state_path(root, "trace").read_bytes())
    monkeypatch.setattr(controller, "_replace_run_file", original)
    recover(controller, root)
    retained = record(controller, root)
    assert sum(row.get("event_type") == controller.TELEMETRY_EVENT_TYPE
        for row in controller.load_events(controller.events_path(root, "trace"))) == 1
    assert record(controller, root) == retained


def test_replay_durability_failure_is_not_reported_as_committed(journal, monkeypatch):
    controller, root = journal
    record(controller, root)
    before = artifacts(controller, root)
    original = controller._run_fsync
    def refused(descriptor, boundary):
        if boundary == "replay.directory":
            raise OSError(errno.EIO, "injected replay failure")
        return original(descriptor, boundary)
    monkeypatch.setattr(controller, "_run_fsync", refused)
    with pytest.raises(controller.RunControllerError) as error:
        record(controller, root)
    assert error.value.error_code == "RUN_MUTATION_WRITE_FAILED" and artifacts(controller, root) == before


@pytest.mark.parametrize("field", ["model", "input_refs", "role", "prompt"])
def test_metric_identity_conflicts_refuse_before_journal_effects(journal, field):
    controller, root = journal
    record(controller, root)
    before = artifacts(controller, root)
    value = event_document(identifier="different-event")
    if field == "model":
        value["model"]["version"] = "2"
    elif field == "input_refs":
        value["input_refs"] = ["sha256:" + "b" * 64]
    elif field == "role":
        value["role"] = "evaluator"
    else:
        value["prompt"]["version"] = "2"
    with pytest.raises(controller.RunControllerError) as error:
        controller.run_telemetry_record(root, run_id="trace", agent_id="worker", event=value)
    assert error.value.error_code == "RUN_EVENT_ID_CONFLICT"
    assert artifacts(controller, root) == before


def test_same_measurement_with_new_narrative_event_retains_exact_measurement_identity(journal):
    controller, root = journal
    record(controller, root)
    value = event_document(identifier="different-event")
    value["summary"]["text"] = "A different evidence-linked summary of the same receipt."
    value["occurred_at"] = "2026-10-04T00:00:01Z"
    value["attempt_status"] = "completed"
    retained = controller.run_telemetry_record(root, run_id="trace", agent_id="worker", event=value)
    assert retained["data"]["event"]["metrics"] == event_document()["metrics"]


@pytest.mark.parametrize("kind", ["scalar", "missing", "logical", "run", "actor", "decision_scalar", "decision_record"])
def test_malformed_recognized_journal_wrappers_refuse_before_read_or_write(journal, kind):
    controller, root = journal
    inner = event_document()
    data = {"logical_id": inner["logical_id"], "event": inner}
    envelope = controller.custom_event(root, "trace", "worker", event_type=controller.TELEMETRY_EVENT_TYPE,
        message="Retained wrapper fixture", data=data, _event_id="malformed")
    if kind == "scalar":
        envelope["data"] = "unsupported"
    elif kind == "missing":
        envelope["data"] = {"logical_id": "missing"}
    elif kind == "logical":
        envelope["data"]["logical_id"] = "different"
    elif kind == "run":
        envelope["run_id"] = "different"
    elif kind == "actor":
        envelope["agent_id"] = "different"
    else:
        envelope["event_type"] = "custom.research.decision"
        envelope["data"] = "unsupported" if kind == "decision_scalar" else {"request_id": "request", "record": "unsupported"}
    controller.append_event(controller.events_path(root, "trace"), envelope)
    before = artifacts(controller, root)
    for operation in (lambda: controller.run_telemetry_events(root, run_id="trace"), lambda: record(controller, root)):
        with pytest.raises(controller.RunControllerError) as error:
            operation()
        assert error.value.error_code == "RUN_EVENTS_INVALID"
        assert artifacts(controller, root) == before


@pytest.mark.parametrize("kind", ["bytes", "events", "lines", "event_bytes", "depth", "duplicate_key", "nan", "huge_exponent"])
def test_bounded_reader_refuses_before_legacy_loader_or_oversized_decode(journal, monkeypatch, kind):
    controller, root = journal
    path = controller.events_path(root, "trace")
    if kind == "bytes":
        monkeypatch.setattr(controller, "JOURNAL_MAX_BYTES", 32)
    elif kind == "events":
        path.write_bytes(b'{"event_id":"one"}\n{"event_id":"two"}\n')
        monkeypatch.setattr(controller, "JOURNAL_MAX_EVENTS", 1)
    elif kind == "lines":
        path.write_bytes(b"\n" * 20)
        monkeypatch.setattr(controller, "JOURNAL_MAX_LINES", 8)
    elif kind == "event_bytes":
        path.write_bytes(b'{"event_id":"one","data":"' + b"x" * 100 + b'"}\n')
        monkeypatch.setattr(controller, "JOURNAL_MAX_EVENT_BYTES", 64)
    elif kind == "depth":
        path.write_bytes(b'{"event_id":"one","data":' + b"[" * 40 + b"0" + b"]" * 40 + b"}\n")
    elif kind == "duplicate_key":
        path.write_bytes(b'{"event_id":"one","event_id":"two"}\n')
    elif kind == "nan":
        path.write_bytes(b'{"event_id":"one","data":NaN}\n')
    else:
        path.write_bytes(b'{"event_id":"one","data":1e9999}\n')
    def legacy_forbidden(*args, **kwargs):
        raise AssertionError("New path used the unbounded legacy loader")
    monkeypatch.setattr(controller, "load_events", legacy_forbidden)
    with pytest.raises(controller.RunControllerError) as error:
        controller._bounded_journal(path)
    assert error.value.error_code in {"RUN_JOURNAL_LIMIT", "RUN_EVENTS_INVALID"}


def test_outer_event_profile_accepts_large_existing_job_record(journal):
    controller, root = journal
    path = controller.events_path(root, "trace")
    controller.append_event(path, {"event_id": "large", "data": {"original": "x" * 70000}})
    snapshot = controller.run_telemetry_events(root, run_id="trace")
    assert snapshot["count"] == 2 and snapshot["events"][-1]["data"]["original"] == "x" * 70000


@pytest.mark.parametrize("kind", ["file_link", "parent_link", "hardlink"])
def test_capture_and_publication_refuse_aliases_without_touching_outside_bytes(journal, tmp_path, kind):
    controller, root = journal
    path = controller.events_path(root, "trace")
    outside = tmp_path / "outside-events"
    outside.write_bytes(path.read_bytes())
    if kind == "file_link":
        path.unlink()
        path.symlink_to(outside)
    elif kind == "hardlink":
        path.unlink()
        controller._native_os.link(outside, path)
    else:
        parent = path.parent
        retained = parent.with_name("retained-run")
        parent.rename(retained)
        parent.symlink_to(retained, target_is_directory=True)
    before = outside.read_bytes()
    with pytest.raises(controller.RunControllerError):
        controller.run_telemetry_events(root, run_id="trace")
    with pytest.raises(controller.RunControllerError):
        record(controller, root)
    assert outside.read_bytes() == before


def test_controlled_canonical_writer_requires_live_native_workspace_gate(journal, tmp_path):
    controller, root = journal
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    grants = ["run_controller.run_telemetry_record", "run_controller.run_telemetry_events"]
    with HostContext([root], grants, "worker", [root], mutation_control_directory=control).activate():
        before = artifacts(controller, root)
        with pytest.raises(Exception) as error:
            record(controller, root)
        assert error.value.error_code == "HOST_CONTEXT_REFUSED" and artifacts(controller, root) == before
        with MutationStore(root, control).workspace_locked():
            retained = record(controller, root)
        assert retained["data"]["event"]["authorized"] is False


def test_original_metadata_byte_format_is_preserved_by_native_publication(tmp_path):
    controller = load_module("native_journal_format", SCRIPTS / "run_controller.py")
    root = tmp_path.resolve()
    path = root / "run-state.json"
    value = {"schema_version": "1.0", "unicode": "Δ", "items": [1, 2]}
    controller.write_run_state_atomic(path, value)
    assert path.read_bytes() == (json.dumps(value, indent=2, sort_keys=False) + "\n").encode("utf-8")
