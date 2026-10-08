"""Actual bounded processes and authenticated full comparative attempt coverage."""

import copy
import hashlib
import os
import sys

import pytest

from tools._comparative_protocol import ARMS, freeze_plan, generator_packet
from tools._journey_cases import digest
from tools.run_comparative_trials import TrialLedger, clock, command_manifest

PROGRAM = """import json,sys
p=json.load(sys.stdin);s=p['sources'][0];t=s['content']
print(json.dumps({'original_text':p['original_text'],'behavior':'answered','text':t,
'scope':'Exact supplied observation.','limitations':['No broader inference.'],
'citations':[{'source_id':s['source_id'],'start':0,'end':len(t),'quote':t}]}))
"""


def plan(command=None, *, seconds=10, max_attempts=3):
    command = command or [sys.executable, "-c", PROGRAM]
    return freeze_plan(study_id="comparison", cases=[{"id": "observation", "original_text": "What value was observed?",
        "stratum": "controlled_snapshot", "sources": [{"source_id": "source-1", "content": "Observed value was 7.",
            "provenance": {"origin": "authored reference", "captured_at": None, "authorization": None},
            "rights": {"status": "allowed", "basis": "Authored reference input."}}]}],
        conditions={"candidate": {"reference": "candidate", "revision": "revision-1", "identity_sha256": "a" * 64},
            "model": {"reference": None, "revision": None, "settings": {}}, "capabilities": ["read-supplied-evidence"],
            "rubric_sha256": "b" * 64, "host_observation": "scripted",
            "policy": {"generator_command_sha256": digest(command),
                "generator_files_sha256": digest(command_manifest(command)), "grading_after_generation": True},
            "interface_guides": {arm: {"sha256": hashlib.sha256(arm.encode()).hexdigest(),
                "content": arm, "descriptor": None} for arm in ARMS}},
        budgets={"seconds": seconds, "model_calls": 1, "tool_calls": 10, "tokens": 1000, "cost": 1,
            "max_attempts": max_attempts}, seed=5)


def ledger(tmp_path, selected=None):
    selected = selected or plan()
    return TrialLedger(tmp_path / "private", selected, observer_key=b"k" * 32,
        work_root=tmp_path / "workers", create=True)


def test_actual_processes_all_arms_seal_and_exact_replay_never_redispatch(tmp_path, monkeypatch):
    owner = ledger(tmp_path)
    for slot in owner.plan["schedule"]:
        result = owner.dispatch(slot["slot_id"], [sys.executable, "-c", PROGRAM])
        assert result["status"] == "succeeded" and result["started_at"] is not None
        assert result["receipt"]["model_calls"] is None and result["receipt"]["cost"] is None
    packet = owner.seal_generation()
    assert packet and owner.report()["generation_closed"]
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("Replay redispatched."))
    with pytest.raises(ValueError, match="generation_closed"):
        owner.dispatch(owner.plan["schedule"][0]["slot_id"], [sys.executable, "-c", PROGRAM])
    assert owner.seal_generation() == packet
    reopened = TrialLedger(tmp_path / "private", owner.plan, observer_key=b"k" * 32, work_root=tmp_path / "workers",
        expected_head=owner.report()["retention_head_sha256"])
    assert reopened.report() == owner.report()


def test_failed_process_retained_explicit_retry_cannot_change_command_or_reset_inventory(tmp_path):
    command = [sys.executable, "-c", "raise RuntimeError('controlled failure')"]
    owner = ledger(tmp_path, plan(command))
    slot = owner.plan["schedule"][0]["slot_id"]
    first = owner.dispatch(slot, command)
    assert first["status"] == "failed" and first["started_at"]
    assert owner.dispatch(slot, command) == first
    second = owner.dispatch(slot, command, retry=True)
    assert second["parent_attempt_id"] == first["attempt_id"] and second["status"] == "failed"
    with pytest.raises(ValueError, match="command_changed"):
        owner.dispatch(slot, [sys.executable, "-c", PROGRAM], retry=True)
    assert len(owner.attempts()) == 2


def test_reservation_or_started_unknown_is_retained_without_inferred_redispatch(tmp_path):
    owner = ledger(tmp_path)
    slot = owner.plan["schedule"][0]["slot_id"]
    row, packet, fresh = owner.reserve(slot)
    assert fresh and row["started_at"] is None and packet == generator_packet(owner.plan, slot)
    assert owner.reserve(slot)[2] is False
    running = owner.observe(row["attempt_id"], status="running", started_at=clock())
    unknown = owner.observe(row["attempt_id"], status="unknown")
    assert unknown["started_at"] == running["started_at"] and unknown["answer"] is None
    with pytest.raises(ValueError, match="unsettled"):
        owner.reserve(slot, retry=True)
    with pytest.raises(ValueError, match="generation_incomplete"):
        owner.seal_generation()


def test_frozen_assignment_and_binding_changes_are_refused(tmp_path):
    owner = ledger(tmp_path)
    with pytest.raises(ValueError, match="assignment_order"):
        owner.reserve(owner.plan["schedule"][1]["slot_id"])
    changed = copy.deepcopy(owner.plan)
    changed["conditions"]["capabilities"].append("extra")
    with pytest.raises(ValueError):
        TrialLedger(tmp_path / "private", changed, observer_key=b"k" * 32, work_root=tmp_path / "workers")
    with pytest.raises(ValueError, match="binding_changed"):
        TrialLedger(tmp_path / "private", owner.plan, observer_key=b"z" * 32, work_root=tmp_path / "workers",
            expected_head=owner.report()["retention_head_sha256"])


def test_tampered_or_partial_event_never_becomes_a_clean_missing_start(tmp_path):
    owner = ledger(tmp_path)
    owner.reserve(owner.plan["schedule"][0]["slot_id"])
    path = tmp_path / "private/events.jsonl"
    raw = path.read_bytes()
    path.write_bytes(raw[:-1])
    with pytest.raises(ValueError, match="retention_incomplete"):
        owner.report()
    path.write_bytes(raw.replace(b'"kind":"attempt"', b'"kind":"attempx"'))
    with pytest.raises(ValueError, match="authentication"):
        owner.report()


def test_symlink_private_file_and_nonprivate_work_root_are_refused(tmp_path):
    owner = ledger(tmp_path)
    events = tmp_path / "private/events.jsonl"
    backup = tmp_path / "saved"
    events.rename(backup)
    events.symlink_to(backup)
    with pytest.raises(OSError):
        owner.report()
    events.unlink(); backup.rename(events)
    os.chmod(tmp_path / "workers", 0o755)  # noqa: S103 -- negative privacy case.
    with pytest.raises(ValueError, match="not_private"):
        with owner.locked():
            pass


def test_deadline_failure_retains_confirmed_start_and_unknown_model_usage(tmp_path):
    command = [sys.executable, "-c", "import time;time.sleep(5)"]
    owner = ledger(tmp_path, plan(command, seconds=1))
    result = owner.dispatch(owner.plan["schedule"][0]["slot_id"], command)
    assert result["status"] == "failed" and result["started_at"] is not None
    assert result["receipt"]["model_calls"] is None


def test_complete_line_rollback_and_full_store_restore_require_latest_external_head(tmp_path):
    owner = ledger(tmp_path)
    events = tmp_path / "private/events.jsonl"
    head = tmp_path / "private/head.json"
    original_events, original_head = events.read_bytes(), head.read_bytes()
    owner.reserve(owner.plan["schedule"][0]["slot_id"])
    latest = owner.report()["retention_head_sha256"]
    events.write_bytes(original_events)
    with pytest.raises(ValueError, match="retention_incomplete"):
        owner.report()
    head.write_bytes(original_head)
    with pytest.raises(ValueError, match="latest_head_mismatch"):
        TrialLedger(tmp_path / "private", owner.plan, observer_key=b"k" * 32,
            work_root=tmp_path / "workers", expected_head=latest)
    with pytest.raises(ValueError, match="requires_trusted_latest_head"):
        TrialLedger(tmp_path / "private", owner.plan, observer_key=b"k" * 32, work_root=tmp_path / "workers")


def test_pending_append_recovery_retains_intent_without_dispatch(tmp_path, monkeypatch):
    owner = ledger(tmp_path)
    latest = owner.report()["retention_head_sha256"]
    original = __import__("tools.run_comparative_trials", fromlist=["replace_private"]).replace_private
    monkeypatch.setattr("tools.run_comparative_trials.replace_private", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lost anchor return")))
    with pytest.raises(RuntimeError, match="lost anchor"):
        owner.reserve(owner.plan["schedule"][0]["slot_id"])
    monkeypatch.setattr("tools.run_comparative_trials.replace_private", original)
    restored = TrialLedger(tmp_path / "private", owner.plan, observer_key=b"k" * 32,
        work_root=tmp_path / "workers", expected_head=latest)
    assert len(restored.attempts()) == 1 and restored.attempts()[0]["status"] == "reserved"
    assert restored.reserve(owner.plan["schedule"][0]["slot_id"])[2] is False


def test_append_recovery_after_head_publication_accepts_only_signed_advancement(tmp_path, monkeypatch):
    owner = ledger(tmp_path)
    before = owner.report()["retention_head_sha256"]
    from tools import run_comparative_trials as module
    original = module.replace_private
    def lost_return(path, value):
        original(path, value)
        raise RuntimeError("head published return lost")
    monkeypatch.setattr(module, "replace_private", lost_return)
    with pytest.raises(RuntimeError, match="return lost"):
        owner.reserve(owner.plan["schedule"][0]["slot_id"])
    monkeypatch.setattr(module, "replace_private", original)
    recovered = TrialLedger(tmp_path / "private", owner.plan, observer_key=b"k" * 32,
        work_root=tmp_path / "workers", expected_head=before)
    assert recovered.attempts()[0]["status"] == "reserved"
    assert not (tmp_path / "private/pending.json").exists()


def test_global_active_attempt_cannot_overlap_or_change_confirmed_start(tmp_path):
    owner = ledger(tmp_path)
    row, _, _ = owner.reserve(owner.plan["schedule"][0]["slot_id"])
    with pytest.raises(ValueError, match="active_attempt"):
        owner.reserve(owner.plan["schedule"][1]["slot_id"])
    owner.observe(row["attempt_id"], status="running", started_at=clock())
    with pytest.raises(ValueError, match="confirmed_start_changed"):
        owner.observe(row["attempt_id"], status="running", started_at=clock())


def test_setup_failure_before_process_spawn_is_retained_unconfirmed(tmp_path, monkeypatch):
    owner = ledger(tmp_path)
    from tools import run_comparative_trials as module
    original = module.publish_new
    def fail_input(path, value):
        if path.name == "input.json":
            raise OSError("controlled setup failure")
        return original(path, value)
    monkeypatch.setattr(module, "publish_new", fail_input)
    result = owner.dispatch(owner.plan["schedule"][0]["slot_id"], [sys.executable, "-c", PROGRAM])
    assert result["status"] == "failed" and result["started_at"] is None


def test_mutable_script_bytes_cannot_change_after_freeze(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(PROGRAM)
    command = [sys.executable, str(worker)]
    owner = ledger(tmp_path, plan(command))
    worker.write_text("print('{}')")
    with pytest.raises(ValueError, match="command_changed"):
        owner.dispatch(owner.plan["schedule"][0]["slot_id"], command)
    assert owner.attempts() == []
