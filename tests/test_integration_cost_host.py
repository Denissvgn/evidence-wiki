"""Host retention boundaries, bounded processes, and partial comparative costs."""
import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tools._comparative_protocol import ARMS, final_judgment_packet, freeze_plan
from tools._integration_costs import METRICS, freeze_cost_plan, project_cost_report
from tools._journey_cases import digest
from tools.measure_integration_costs import from_comparative_ledger, main, observe_command, read_command_receipt
from tools.run_comparative_trials import TrialLedger, clock, command_manifest

KEY = b"k" * 32


def selected_plan():
    now = datetime.now(timezone.utc)
    return freeze_cost_plan(scope_id="observed", binding={"selection": "explicit-command"},
        sources=[{"source_id": "host", "provenance": "host_observed", "kind": "automated",
                  "origin": "Selected host command", "collection_started_at": (now - timedelta(seconds=1)).isoformat(),
                  "collection_ended_at": (now + timedelta(hours=1)).isoformat(), "coverage": "partial", "authentication_ref": None}],
        attempts=[{"attempt_id": "selected", "phase": "operation", "allocation": "repeated", "source_id": "host",
                   "required_metrics": list(METRICS)}])


def observe(tmp_path, program="print('bounded output')", **options):
    directory, work = tmp_path / "private", tmp_path / "work"
    directory.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    plan, argv = selected_plan(), [sys.executable, "-c", program]
    options = {"observer_key": KEY, "directory": directory, "work_root": work, **options}
    result = observe_command(plan, "selected", argv, **options)
    return plan, argv, {key: options[key] for key in ("observer_key", "directory", "work_root")}, result


def test_receipt_authentication_replay_and_provider_usage_unknown(tmp_path, monkeypatch):
    plan, argv, options, result = observe(tmp_path)
    event = result["event"]
    assert event["status"] == "succeeded"
    assert Decimal(event["metrics"]["elapsed_seconds"]) >= 0
    assert all(event["metrics"][key] is None for key in METRICS if key != "elapsed_seconds")
    assert read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options) == result
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("redispatched"))
    with pytest.raises(ValueError, match="already_reserved"):
        observe_command(plan, "selected", argv, **options)
    with pytest.raises(ValueError, match="trusted_receipt"):
        read_command_receipt(plan, "selected", argv, expected_receipt_sha256="0" * 64, **options)
    with pytest.raises(ValueError, match="binding_changed"):
        read_command_receipt(plan, "selected", [*argv[:2], "print('other')"], expected_receipt_sha256=result["receipt_sha256"], **options)
    report = project_cost_report(plan, [event])
    assert report["automated"]["cost"]["total"] is None
    assert report["coverage"]["host_coverage_authenticated"] is False


def test_tamper_missing_final_and_orphan_intent_never_resume(tmp_path):
    plan, argv, options, result = observe(tmp_path)
    final = options["directory"] / "selected.receipt.json"
    value = json.loads(final.read_text())
    value["status"] = "failed"
    final.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="authentication_failed"):
        read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options)
    final.unlink()
    with pytest.raises(FileNotFoundError):
        read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options)
    with pytest.raises(ValueError, match="already_reserved"):
        observe_command(plan, "selected", argv, **options)


def test_spawn_failure_keeps_final_without_confirmed_start(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("controlled spawn failure")
    monkeypatch.setattr("subprocess.Popen", fail)
    plan, argv, options, result = observe(tmp_path)
    assert result["event"]["status"] == "failed"
    assert result["event"]["started_at"] is not None
    assert result["receipt"]["started_at"] is None
    assert result["event"]["metrics"]["elapsed_seconds"] is not None
    assert "spawn failure" in result["receipt"]["failure"]
    assert read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options) == result


@pytest.mark.parametrize("program,options,reason", [
    ("import time; time.sleep(10)", {"timeout_seconds": 0.05}, "timeout"),
    ("import sys; sys.stdout.write('x'*2000000)", {}, "overflow"),
])
def test_timeout_and_output_overflow_retained(tmp_path, program, options, reason):
    plan, argv, options, result = observe(tmp_path, program, **options)
    assert result["event"]["status"] == "failed"
    assert reason in result["receipt"]["failure"]
    assert sum(item["retained_bytes"] for item in result["receipt"]["streams"].values()) <= 1048576
    assert read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options) == result


@pytest.mark.parametrize("meter", [lambda _: {"elapsed_seconds": "0"}, lambda _: {"cost": "0"}, lambda _: 42])
def test_bad_meter_keeps_unknown_metrics(tmp_path, meter):
    plan, argv, options, result = observe(tmp_path, meter=meter, meter_id="explicit-meter")
    assert result["event"]["status"] == "succeeded"
    assert result["event"]["metrics"]["cost"] is None
    assert result["receipt"]["telemetry_qualification"].startswith("unknown:")
    assert result["receipt"]["meter_id"] == "explicit-meter"


def test_selected_callable_usage_cannot_erase_host_timing(tmp_path):
    plan, _, _, result = observe(tmp_path, meter=lambda _: {"model_calls": 2, "tokens": 12,
        "cost": {"amount": "0.03", "currency": "EUR", "pricing_ref": "explicit-price"}}, meter_id="explicit-meter")
    assert result["event"]["metrics"]["tokens"] == 12
    assert result["event"]["metrics"]["elapsed_seconds"] is not None
    report = project_cost_report(plan, [result["event"]])
    assert report["automated"]["cost"]["subtotals"][0]["currency"] == "EUR"
    assert report["automated"]["cost"]["total"] is None


def test_meter_wait_is_bounded_and_late_result_cannot_change_receipt(tmp_path):
    release, completed = threading.Event(), threading.Event()

    def blocked(_):
        release.wait(timeout=5)
        completed.set()
        return {"tokens": 999}

    try:
        began = time.monotonic()
        plan, argv, options, result = observe(tmp_path, meter=blocked, meter_id="selected-meter", meter_timeout_seconds=0.02)
        assert time.monotonic() - began < 2
        assert result["receipt"]["meter_work_unresolved"] is True
        assert result["event"]["metrics"]["tokens"] is None
        retained = (options["directory"] / "selected.receipt.json").read_bytes()
    finally:
        release.set()
    assert completed.wait(timeout=1)
    assert (options["directory"] / "selected.receipt.json").read_bytes() == retained
    assert read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options) == result


def test_meter_owned_mutation_after_validation_cannot_override_host_timing(tmp_path, monkeypatch):
    from tools.measure_integration_costs import _event
    owned = {"tokens": 12, "cost": {"amount": "0.03", "currency": "EUR", "pricing_ref": "declared-price"}}

    def validate_then_mutate(*args, **kwargs):
        validated = _event(*args, **kwargs)
        if args[5] == "0" * 64:
            owned["elapsed_seconds"] = "0"
            owned["tokens"] = 999
            owned["cost"]["amount"] = "99"
        return validated

    monkeypatch.setattr("tools.measure_integration_costs._event", validate_then_mutate)
    _, _, _, result = observe(tmp_path, meter=lambda _: owned, meter_id="selected-meter")
    assert owned["tokens"] == 999
    assert "elapsed_seconds" not in result["receipt"]["usage"]
    assert result["event"]["metrics"]["tokens"] == 12
    assert result["event"]["metrics"]["cost"]["amount"] == "0.03"
    event = result["event"]
    delta = datetime.fromisoformat(event["finished_at"]) - datetime.fromisoformat(event["started_at"])
    expected = Decimal((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds) / 1_000_000
    assert Decimal(event["metrics"]["elapsed_seconds"]) == expected


def test_post_exit_deadline_exception_is_retained_as_failed(tmp_path, monkeypatch):
    from tools.measure_integration_costs import subprocess as observer_subprocess
    real_spawn = observer_subprocess.Popen

    def spawn(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        process.wait(timeout=1)
        time.sleep(0.03)
        return process

    monkeypatch.setattr("tools.measure_integration_costs.subprocess.Popen", spawn)
    _, _, _, result = observe(tmp_path, timeout_seconds=0.01)
    assert result["receipt"]["status"] == "failed"
    assert "TimeoutError" in result["receipt"]["failure"]


def test_private_paths_and_output_tampering_rejected(tmp_path):
    plan, argv, options, result = observe(tmp_path)
    output = options["directory"] / "selected.stdout"
    output.write_bytes(b"changed")
    with pytest.raises(ValueError, match="output_changed"):
        read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options)
    output.unlink()
    output.symlink_to(options["directory"] / "selected.stderr")
    with pytest.raises(OSError):
        read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options)


def test_changed_script_keeps_authenticated_historical_failure(tmp_path):
    script = tmp_path / "command.py"
    script.write_text("from pathlib import Path\nPath(__file__).write_text('changed')\n")
    directory, work = tmp_path / "private", tmp_path / "work"
    directory.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    plan, argv = selected_plan(), [sys.executable, str(script)]
    options = {"observer_key": KEY, "directory": directory, "work_root": work}
    result = observe_command(plan, "selected", argv, **options)
    assert result["receipt"]["failure"] == "cost_command_changed"
    assert result["event"]["status"] == "failed"
    assert (directory / "selected.receipt.json").exists()
    historical = read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options)
    assert historical["event"] == result["event"]
    assert historical["current_command_manifest_matches"] is False


def test_closing_clock_outside_window_retains_receipt_with_coverage_gap(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    ticks = iter([now.isoformat(), now.isoformat(), now.isoformat(), (now + timedelta(hours=2)).isoformat()])
    monkeypatch.setattr("tools.measure_integration_costs._now", lambda: next(ticks))
    plan, argv, options, result = observe(tmp_path)
    assert result["event"] is None
    assert result["coverage_gap"] is not None
    assert result["receipt"]["status"] == "failed"
    assert read_command_receipt(plan, "selected", argv, expected_receipt_sha256=result["receipt_sha256"], **options) == result


def test_setup_failure_and_meter_interrupt_retained(tmp_path):
    directory, work = tmp_path / "private", tmp_path / "work"
    directory.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    (work / "selected").mkdir(mode=0o700)
    plan, argv = selected_plan(), [sys.executable, "-c", "print('never started')"]
    options = {"observer_key": KEY, "directory": directory, "work_root": work}
    result = observe_command(plan, "selected", argv, **options)
    assert result["receipt"]["started_at"] is None
    assert "FileExistsError" in result["receipt"]["failure"]
    second = tmp_path / "second"
    second.mkdir(mode=0o700)

    def interrupt(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as interrupted:
        observe(second, meter=interrupt, meter_id="selected-meter")
    result = interrupted.value.cost_receipt
    assert result["event"]["status"] == "interrupted"
    assert result["event"]["metrics"]["cost"] is None
    assert (second / "private" / "selected.receipt.json").exists()


def test_exited_leader_does_not_leave_ordinary_child_running(tmp_path, monkeypatch):
    from tools.measure_integration_costs import stop_process
    cleanups = []

    def cleanup(process):
        cleanups.append(process.poll())
        stop_process(process)

    monkeypatch.setattr("tools.measure_integration_costs.stop_process", cleanup)
    marker = tmp_path / "survived"
    program = ("import os,time\n"
               "if os.fork() == 0:\n"
               " time.sleep(0.4)\n"
               f" open({str(marker)!r},'w').write('alive')\n"
               "else:\n os._exit(0)\n")
    _, _, _, result = observe(tmp_path, program)
    assert cleanups == [0]
    if result["event"]["status"] == "failed":
        assert result["receipt"]["failure"].startswith("cost_cleanup_failed:")
    time.sleep(0.5)
    assert not marker.exists()


def comparative_plan(command):
    return freeze_plan(study_id="comparison", cases=[{"id": "observation", "original_text": "What value?",
        "stratum": "controlled_snapshot", "sources": [{"source_id": "source", "content": "Observed 7.",
            "provenance": {"origin": "reference", "captured_at": None, "authorization": None},
            "rights": {"status": "allowed", "basis": "Authored reference."}}]}],
        conditions={"candidate": {"reference": "candidate", "revision": "revision", "identity_sha256": "a" * 64},
            "model": {"reference": None, "revision": None, "settings": {}}, "capabilities": ["read-supplied-evidence"],
            "rubric_sha256": "b" * 64, "host_observation": "scripted",
            "policy": {"generator_command_sha256": digest(command), "generator_files_sha256": digest(command_manifest(command)),
                       "grading_after_generation": True},
            "interface_guides": {arm: {"sha256": hashlib.sha256(arm.encode()).hexdigest(), "content": arm, "descriptor": None}
                                 for arm in ARMS}},
        budgets={"seconds": 10, "model_calls": 1, "tool_calls": 10, "tokens": 1000, "cost": 1, "max_attempts": 3}, seed=5,
        judging_budget={"seconds": 10, "model_calls": 1, "tool_calls": 10, "tokens": 1000, "cost": 1, "max_attempts": 1})


def test_readonly_comparative_preserves_retries_missing_grading_and_unknown_provider(tmp_path):
    command = [sys.executable, "-c", "raise RuntimeError('controlled failure')"]
    owner = TrialLedger(tmp_path / "ledger", comparative_plan(command), observer_key=KEY, work_root=tmp_path / "workers", create=True)
    slot = owner.plan["schedule"][0]["slot_id"]
    owner.dispatch(slot, command)
    owner.dispatch(slot, command, retry=True)
    before = (owner.directory / "events.jsonl").read_bytes()
    adapted = from_comparative_ledger(owner, "costs")
    assert (owner.directory / "events.jsonl").read_bytes() == before
    assert len(adapted["events"]) == 2
    update = next(row for row in adapted["plan"]["attempts"] if row["phase"] == "evidence_update")
    assert update["allocation"] == "repeated"
    assert adapted["report"]["allocation"]["one_time_per_use"] is None
    assert all(event["status"] == "failed" for event in adapted["events"])
    assert len(adapted["report"]["coverage"]["missing_attempt_ids"]) == 8
    assert adapted["report"]["automated"]["cost"]["total"] is None
    assert adapted["report"]["external_status"]["human"] == "unobserved"
    assert adapted["retention_head_sha256"] == owner.report()["retention_head_sha256"]
    for assignment in owner.plan["schedule"][1:]:
        owner.dispatch(assignment["slot_id"], command)
    owner.seal_generation()
    judgments = []
    grade_start = clock()
    packet = final_judgment_packet(owner.plan, owner.attempts(), generation_closed=True, binding_outcomes_sealed=True)
    grade = {"provenance": "host_observed", "kind": "scripted_process", "input_sha256": digest(packet),
             "output_sha256": digest(judgments), "rubric_sha256": owner.plan["conditions"]["rubric_sha256"],
             "profile_ref": "selected-grader", "started_at": grade_start, "completed_at": clock(), "elapsed_seconds": 0.0,
             "observed_usage": {"model_calls": 1, "tool_calls": 1, "tokens": 10, "cost": 0.1}}
    owner.record_judgments(judgments, grading_receipt=grade)
    adapted = from_comparative_ledger(owner, "costs")
    review = next(event for event in adapted["events"] if event["attempt_id"] == "grading")
    assert review["metrics"]["elapsed_seconds"] is not None
    assert all(review["metrics"][key] is None for key in METRICS if key != "elapsed_seconds")
    assert adapted["report"]["coverage"]["host_coverage_authenticated"] is False
    with pytest.raises(ValueError, match="live_ledger"):
        from_comparative_ledger(owner.report(), "costs")
    (owner.directory / "events.jsonl").write_bytes(before)
    with pytest.raises(ValueError, match="retention_incomplete"):
        from_comparative_ledger(owner, "costs")


def test_comparative_unknown_start_and_legacy_counters_never_become_provider_usage(tmp_path):
    command = [sys.executable, "-c", "print('selected')"]
    owner = TrialLedger(tmp_path / "ledger", comparative_plan(command), observer_key=KEY,
                        work_root=tmp_path / "workers", create=True)
    first, _, _ = owner.reserve(owner.plan["schedule"][0]["slot_id"])
    legacy = {**first["receipt"], "kind": "scripted_process", "model_calls": 1, "tool_calls": 1, "tokens": 12, "cost": 0.1}
    owner.observe(first["attempt_id"], status="abandoned", receipt=legacy)
    owner.reserve(owner.plan["schedule"][1]["slot_id"])
    result = from_comparative_ledger(owner, "costs")
    assert {event["status"] for event in result["events"]} == {"abandoned", "unknown"}
    assert all(event["started_at"] is None for event in result["events"])
    assert all(all(event["metrics"][key] is None for key in METRICS) for event in result["events"])
    assert result["orchestration_diagnostics"][0]["retained_counters"]["model_calls"] == 1
    assert result["report"]["automated"]["cost"]["total"] is None


def test_comparative_adapter_rejects_missing_lock_and_pending_without_writes(tmp_path):
    command = [sys.executable, "-c", "print('selected')"]
    owner = TrialLedger(tmp_path / "ledger", comparative_plan(command), observer_key=KEY,
                        work_root=tmp_path / "workers", create=True)
    before = (owner.directory / "events.jsonl").read_bytes()
    pending = owner.directory / "pending.json"
    pending.write_bytes(b"unresolved")
    pending.chmod(0o600)
    with pytest.raises(ValueError, match="pending_retention"):
        from_comparative_ledger(owner, "costs")
    assert pending.read_bytes() == b"unresolved"
    assert (owner.directory / "events.jsonl").read_bytes() == before
    pending.unlink()
    lock = owner.directory / "ledger.lock"
    lock.unlink()
    with pytest.raises(FileNotFoundError):
        from_comparative_ledger(owner, "costs")
    assert not lock.exists()


def test_comparative_cli_uses_nonrecovering_readonly_restore(tmp_path, capsys):
    command = [sys.executable, "-c", "print('selected')"]
    owner = TrialLedger(tmp_path / "ledger", comparative_plan(command), observer_key=KEY,
                        work_root=tmp_path / "workers", create=True)
    key_path, plan_path = tmp_path / "key", tmp_path / "plan.json"
    key_path.write_bytes(KEY)
    plan_path.write_text(json.dumps(owner.plan))
    key_path.chmod(0o600)
    plan_path.chmod(0o600)
    expected = owner.report()["retention_head_sha256"]
    arguments = ["comparative", "--plan", str(plan_path), "--directory", str(owner.directory),
                 "--work-root", str(owner.work_root), "--observer-key", str(key_path),
                 "--expected-head", expected, "--scope-id", "costs"]
    before = {path.name: path.read_bytes() for path in owner.directory.iterdir()}
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["retention_head_sha256"] == expected
    assert {path.name: path.read_bytes() for path in owner.directory.iterdir()} == before
    pending = owner.directory / "pending.json"
    pending.write_bytes(b"unresolved")
    pending.chmod(0o600)
    before = {path.name: path.read_bytes() for path in owner.directory.iterdir()}
    with pytest.raises(ValueError, match="read_only_recovery_required"):
        main(arguments)
    assert {path.name: path.read_bytes() for path in owner.directory.iterdir()} == before
