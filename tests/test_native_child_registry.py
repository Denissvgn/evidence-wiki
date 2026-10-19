"""Per-birth certificates survive numeric process identifier reuse."""

import copy
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from tools import _native_child_registry as registry
from tools import run_native_owner_feasibility as runner
from tools._release_decision import canonical, digest


def binding(tmp_path):
    return {"schema_version": "native-owned-child/v2", "plan_sha256": "a" * 64,
        "control_directory": str(tmp_path), "stage": "decide", "worker_pid": os.getpid(),
        "adapter_raw_sha256": "b" * 64, "controller_sha256": "c" * 64,
        "candidate_sha256": "d" * 64}


def tracker(tmp_path):
    path = tmp_path / "registry.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return registry.ChildTracker(descriptor, binding(tmp_path)), path


def process(pid=999999):
    actual = SimpleNamespace(pid=pid, returncode=None, _waitpid_lock=threading.Lock())
    actual.poll = lambda: actual.returncode
    return actual


def records(raw):
    return [json.loads(row) for row in raw.splitlines()]


def rechain(rows):
    head, raw = "0" * 64, b""
    for sequence, row in enumerate(rows):
        row["sequence"], row["previous_sha256"] = sequence, head
        original = canonical(row)
        head = runner.raw_pin(original)
        raw += original
    return raw


def completed_registry(tmp_path, monkeypatch):
    owned, path = tracker(tmp_path)
    actual = process()
    owned.real_popen = lambda *args, **kwargs: actual
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(registry, "_group_alive", lambda _: False)
    owned.popen(["fixed"], start_new_session=True)
    actual.returncode = 0
    assert owned.confirm_completed(actual)
    owned.close(60)
    return path, binding(tmp_path)


def test_repeated_pid_births_consume_original_absence_without_late_probes(tmp_path, monkeypatch):
    owned, path = tracker(tmp_path)
    first, second = process(), process()
    processes = iter((first, second))
    owned.real_popen = lambda *args, **kwargs: next(processes)
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: pid)
    probes = []
    def absent(actual):
        probes.append(actual)
        return False
    monkeypatch.setattr(registry, "_group_alive", absent)
    assert owned.popen(["fixed"], start_new_session=True) is first
    first.returncode = 0
    assert owned.confirm_completed(first)
    first.poll = lambda: pytest.fail("certified birth must not be polled")
    assert owned.popen(["fixed"], start_new_session=True) is second
    second.returncode = 2
    assert owned.confirm_completed(second)
    monkeypatch.setattr(registry, "_group_alive", lambda _: pytest.fail("historical numeric probe"))
    monkeypatch.setattr(registry.os, "killpg", lambda *_: pytest.fail("historical numeric signal"))
    owned.close(60)
    result = registry.verify_registry(path.read_bytes(), binding(tmp_path))
    assert result["spawn_count"] == 2
    assert probes == [first, second]
    with pytest.raises(ValueError, match="native_child_launch_bound"):
        owned.popen(["late"], start_new_session=True)


@pytest.mark.parametrize("alive", [True, None, 0, "", PermissionError("unknown")])
def test_completion_requires_literal_absence_and_actual_wait(tmp_path, monkeypatch, alive):
    owned, path = tracker(tmp_path)
    actual = process()
    owned.real_popen = lambda *args, **kwargs: actual
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: pid)
    def probe(_):
        if isinstance(alive, BaseException):
            raise alive
        return alive
    monkeypatch.setattr(registry, "_group_alive", probe)
    owned.popen(["fixed"], start_new_session=True)
    assert not owned.confirm_completed(actual)
    actual.returncode = 0
    assert not owned.confirm_completed(actual)
    with pytest.raises(RuntimeError, match="native_child_cleanup_unconfirmed"):
        owned.close(60)
    rows = records(path.read_bytes())
    assert not any(row["kind"] == "completion" for row in rows)
    assert rows[-1]["data"]["cleanup_confirmed"] is False
    with pytest.raises(ValueError, match="native_child_registry_seal"):
        registry.verify_registry(path.read_bytes(), binding(tmp_path))


def test_numeric_pid_cannot_select_owned_child(tmp_path, monkeypatch):
    owned, path = tracker(tmp_path)
    actual = process()
    owned.real_popen = lambda *args, **kwargs: actual
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(registry, "_group_alive", lambda _: False)
    owned.popen(["fixed"], start_new_session=True)
    with pytest.raises(ValueError, match="native_child_object_not_owned"):
        owned.confirm_completed(actual.pid)
    actual.returncode = 0
    owned.close(60)
    assert path.is_file()


@pytest.mark.parametrize("change", ["chain", "spawn_pin", "ordinal", "missing", "duplicate", "present", "parent"])
def test_registry_rejects_unbound_or_incomplete_certificates(tmp_path, monkeypatch, change):
    path, expected = completed_registry(tmp_path, monkeypatch)
    rows = records(path.read_bytes())
    if change == "spawn_pin":
        rows[2]["data"]["spawn_sha256"] = "f" * 64
    elif change == "ordinal":
        rows[2]["data"]["ordinal"] = 1
    elif change == "missing":
        rows.pop(2)
    elif change == "duplicate":
        rows.insert(3, copy.deepcopy(rows[2]))
    elif change == "present":
        rows[2]["data"]["group_absent"] = False
    elif change == "parent":
        rows[2]["data"]["producer_group_absent"] = True
    raw = rechain(rows)
    if change == "chain":
        raw = raw.replace(b'"previous_sha256":"', b'"previous_sha256":"f', 1)
    with pytest.raises(ValueError):
        registry.verify_registry(raw, expected)


def test_registry_bounds_and_child_cardinality(tmp_path, monkeypatch):
    path, expected = completed_registry(tmp_path, monkeypatch)
    raw = path.read_bytes()
    monkeypatch.setattr(registry, "REGISTRY_LIMIT", len(raw) - 1)
    with pytest.raises(ValueError, match="native_child_registry_bound"):
        registry.verify_registry(raw, expected)
    monkeypatch.setattr(registry, "REGISTRY_LIMIT", 8_388_608)
    rows = records(raw)
    rows.insert(3, copy.deepcopy(rows[1]))
    rows[3]["data"]["ordinal"] = 1
    rows[-1]["data"].update(spawn_count=2)
    with pytest.raises(ValueError):
        registry.verify_registry(rechain(rows), expected)
    monkeypatch.setattr(registry, "MAX_CHILDREN", 0)
    (tmp_path / "empty").mkdir()
    owned, _ = tracker(tmp_path / "empty")
    try:
        with pytest.raises(ValueError, match="native_child_launch_bound"):
            owned.popen(["fixed"], start_new_session=True)
    finally:
        owned.close(60)


def test_actual_short_child_certifies_after_wait(tmp_path):
    owned, path = tracker(tmp_path)
    child = owned.popen([str(runner.ROOT / ".venv/bin/python"), "-c", "pass"],
                        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert child.wait(timeout=10) == 0
    assert owned.confirm_completed(child)
    owned.close(60)
    assert registry.verify_registry(path.read_bytes(), binding(tmp_path))["spawn_count"] == 1


def test_v2_requires_separate_bound_producer_completion(tmp_path, monkeypatch):
    root = tmp_path / "study"
    (root / "children").mkdir(parents=True, mode=0o700)
    plan = {"candidate_sha256": "d" * 64, "scope": {
        "native_child_control_directory": str(root), "native_cleanup_recipe": "owned-popen-groups/v2",
        "native_child_adapter_raw_sha256": "b" * 64, "owner_read_controller_sha256": "c" * 64}}
    expected = binding(root) | {"plan_sha256": digest(plan), "worker_pid": 10001}
    path = root / "children/decide.jsonl"
    rows = [expected | {"kind": "open", "data": {"maximum_children": registry.MAX_CHILDREN}},
        expected | {"kind": "closed", "data": {"spawn_count": 0, "completion_count": 0,
                                                "cleanup_confirmed": True}}]
    path.write_bytes(rechain(rows))
    path.chmod(0o600)
    monkeypatch.setattr(runner, "_group_alive", lambda _: pytest.fail("numeric producer probe"))
    proof = {"worker_pid": 10001, "plan_sha256": digest(plan), "stage": "decide",
             "group_absent": True, "returncode": 0, "command_receipt_sha256": "e" * 64}
    for invalid in (None, proof | {"worker_pid": 10002}, proof | {"plan_sha256": "f" * 64},
                    proof | {"group_absent": None}, proof | {"stage": "replay"}):
        with pytest.raises(ValueError, match="native_child_producer_completion"):
            runner.verify_child_registry(path, plan=plan, stage="decide", worker_pid=10001,
                                         producer_cleanup=invalid)
    assert runner.verify_child_registry(path, plan=plan, stage="decide", worker_pid=10001,
                                        producer_cleanup=proof)["cleanup_confirmed"]


@pytest.mark.parametrize("body_failure", [False, True])
def test_context_yields_tracker_and_keeps_launch_closed_after_seal(tmp_path, monkeypatch, body_failure):
    root = tmp_path / "context"
    (root / "children").mkdir(parents=True)
    plan = {"candidate_sha256": "d" * 64, "scope": {
        "native_child_control_directory": str(root), "native_cleanup_recipe": "owned-popen-groups/v2"}}
    actual = process()
    monkeypatch.setattr(runner, "_cleanup_binding", lambda *args: (root, "decide"))
    monkeypatch.setattr(runner, "_adapter_pin", lambda: "b" * 64)
    monkeypatch.setattr(runner, "source_manifest", lambda: {})
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: actual)
    monkeypatch.setattr(registry.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(registry, "_group_alive", lambda _: False)
    def run():
        with runner.native_cleanup_context(plan, "decide") as selected:
            assert isinstance(selected, registry.ChildTracker)
            assert subprocess.Popen(["fixed"], start_new_session=True) is actual
            actual.returncode = 0
            assert selected.confirm_completed()
            if body_failure:
                raise ValueError("original_body_failure")
        return selected
    if body_failure:
        with pytest.raises(ValueError, match="original_body_failure"):
            run()
    else:
        assert run().closed
    with pytest.raises(ValueError, match="native_child_launch_bound"):
        subprocess.Popen(["late"], start_new_session=True)
    rows = records((root / "children/decide.jsonl").read_bytes())
    assert rows[-1]["kind"] == "closed" and rows[-1]["data"]["cleanup_confirmed"] is True


def test_reader_rejects_child_count_over_limit_before_close(tmp_path, monkeypatch):
    expected = binding(tmp_path)
    rows = [{"kind": "open", "data": {"maximum_children": registry.MAX_CHILDREN}}]
    rows += [{"kind": "spawn", "data": {"ordinal": ordinal, "pid": 100000 + ordinal,
                                          "pgid": 100000 + ordinal}}
             for ordinal in range(registry.MAX_CHILDREN + 1)]
    rows += [{"kind": "closed", "data": {"spawn_count": registry.MAX_CHILDREN + 1,
                                         "completion_count": 0, "cleanup_confirmed": True}}]
    with pytest.raises(ValueError, match="native_child_registry_spawn"):
        registry.verify_registry(rechain([expected | row for row in rows]), expected)
