"""Authenticated native stdout is validated before exclusive decision publication."""

import contextlib
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_integration_improvement import NOW, fixture
from tools import _integration_improvement as gate
from tools import run_native_owner_feasibility as runner
from tools import run_repeated_integrations as cli
from tools._release_decision import canonical, digest


def decision_run(tmp_path, monkeypatch, *, outcome="continue", report_change=None, receipt_change=None,
                 refusal=None, action="decide"):
    plan, _, pins, readers = fixture()
    report = gate.decide(plan, digest(plan), pins, digest(pins), readers, now=NOW)
    report["outcome"] = outcome
    if report_change is not None:
        report = report_change(report)
    if "report_sha256" in report:
        report["report_sha256"] = digest({key: value for key, value in report.items() if key != "report_sha256"})
    raw = json.dumps(report, sort_keys=True).encode() + b"\n"
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    (tmp_path / "decisions").mkdir(mode=0o700)
    stdout = receipts / (action + ".stdout")
    stdout.write_bytes(raw)
    stdout.chmod(0o600)
    decision_path = tmp_path / "decisions/decision.json"
    if action == "replay":
        runner.publish_new(decision_path, report)
    receipt = {"exit_code": 0 if outcome == "continue" else 2,
               "status": "succeeded" if outcome == "continue" else "failed", "failure": None,
               "output_overflow": False, "output_reader_work_unresolved": False,
               "meter_work_unresolved": False, "meter_id": None,
               "streams": {"stdout": {"retained_bytes": len(raw), "sha256": runner.raw_pin(raw)}}}
    verified = {"receipt": receipt, "receipt_sha256": "a" * 64, "coverage_gap": None,
                "current_command_manifest_matches": True}
    if receipt_change is not None:
        receipt_change(verified)
    observed = copy.deepcopy(verified)
    events = []
    config = {"receipt_directory": str(receipts), "proof_work_root": str(tmp_path), "key_path": "key",
              "cost_plan": {}}
    originals = {"originals": "fixed"}
    args = SimpleNamespace(action=action, expected_originals=digest(originals), expected_evidence=digest(pins),
                           expected_decision_raw=runner.raw_pin(canonical(report)))
    monkeypatch.setattr(runner, "load_study", lambda args: (tmp_path, {"recipe": {"decision_seconds": 900}},
                                                               plan, {"primary": config}))
    actual_document = runner.document
    monkeypatch.setattr(runner, "document", lambda path: originals if Path(path).name == "originals.json" else
                        pins if Path(path).name == "evidence-pins.json" else actual_document(path))
    monkeypatch.setattr(runner, "evidence_pins", lambda *args: pins)

    def invocation(root, name, argv, *args):
        if action == "decide":
            assert argv[-2:] == ["--expected-evidence", digest(pins)]
        else:
            assert argv[-2:] == ["--report", str(decision_path)]
        assert "--output" not in argv
        events.append("invocation")
        return "invocation-pin"

    def observe(*args, **kwargs):
        events.append("observe")
        if refusal == "occupied":
            decision_path.write_bytes(b"occupied")
            decision_path.chmod(0o600)
        elif refusal == "symlink":
            decision_path.symlink_to(tmp_path / "absent")
        return observed

    def authenticate(*args, **kwargs):
        events.append("authenticate")
        if refusal == "authentication":
            raise ValueError("rejected")
        if refusal == "stdout_mutation":
            stdout.write_bytes(b"{}")
        return verified, 123

    def proof(*args, **kwargs):
        events.append("producer_proof")
        if refusal == "producer_proof":
            raise ValueError("rejected")
        return {}

    def cleanup(*args, **kwargs):
        events.append("cleanup")
        if refusal == "cleanup":
            raise ValueError("rejected")
        return {"confirmed": True}

    actual_read, actual_publish = runner.read_private, runner.publish_new

    def read(path, maximum):
        if str(path) == "key":
            return b"key"
        events.append("read_stdout" if Path(path) == stdout else "read_decision")
        return actual_read(path, maximum)

    def publish(path, value):
        events.append("publish_decision" if Path(path) == decision_path else "publish_retention")
        actual_publish(path, value)
        if refusal == "readback_mutation" and Path(path) == decision_path:
            decision_path.write_bytes(b"{}")

    monkeypatch.setattr(runner, "_invocation", invocation)
    monkeypatch.setattr(runner, "observe_command", observe)
    monkeypatch.setattr(runner, "_verify_observation", authenticate)
    monkeypatch.setattr(runner, "_cleanup_options", proof)
    monkeypatch.setattr(runner, "verify_child_registry", cleanup)
    monkeypatch.setattr(runner, "read_private", read)
    monkeypatch.setattr(runner, "publish_new", publish)
    return args, report, observed, verified, decision_path, events


@pytest.mark.parametrize("outcome", ["continue", "revise", "stop", "narrow"])
def test_authenticated_full_stdout_publishes_after_cleanup_and_before_retention(tmp_path, monkeypatch, outcome):
    args, report, _, _, path, events = decision_run(tmp_path, monkeypatch, outcome=outcome)
    retention = runner.execute(args)
    assert retention["verified"] is True
    assert retention["failure_classes"] == []
    assert path.read_bytes() == canonical(report)
    assert retention["decision_raw_sha256"] == runner.raw_pin(path.read_bytes())
    assert events == ["invocation", "observe", "authenticate", "producer_proof", "cleanup", "read_stdout",
                      "publish_decision", "read_decision", "publish_retention"]


@pytest.mark.parametrize("refusal", ["authentication", "producer_proof", "cleanup", "stdout_mutation"])
def test_untrusted_or_unclosed_stdout_never_creates_decision(tmp_path, monkeypatch, refusal):
    args, _, _, _, path, events = decision_run(tmp_path, monkeypatch, refusal=refusal)
    if refusal == "stdout_mutation":
        monkeypatch.setattr(runner, "_original", lambda raw: pytest.fail("changed stdout must not be parsed"))
    retention = runner.execute(args)
    assert retention["verified"] is False and retention["failure_classes"]
    assert not path.exists()
    assert "publish_decision" not in events
    if refusal != "stdout_mutation":
        assert "read_stdout" not in events


@pytest.mark.parametrize("change", [
    lambda value: value["receipt"].update(failure="TimeoutError"),
    lambda value: value.update(coverage_gap="outside window"),
    lambda value: value["receipt"].update(output_overflow=True),
    lambda value: value["receipt"].update(output_reader_work_unresolved=True),
    lambda value: value["receipt"].update(meter_work_unresolved=True),
    lambda value: value["receipt"].update(meter_id="external"),
    lambda value: value["receipt"].update(exit_code=1),
    lambda value: value["receipt"].update(status="interrupted"),
])
def test_incomplete_authenticated_receipt_refuses_before_stdout_read(tmp_path, monkeypatch, change):
    args, _, observed, _, path, events = decision_run(tmp_path, monkeypatch, receipt_change=change)
    # An unauthenticated success summary cannot override the authenticated receipt.
    observed["receipt"].update(exit_code=0, status="succeeded", failure=None, output_overflow=False,
                               output_reader_work_unresolved=False, meter_work_unresolved=False, meter_id=None)
    observed["coverage_gap"] = None
    retention = runner.execute(args)
    assert retention["verified"] is False and not path.exists()
    assert "read_stdout" not in events and "publish_decision" not in events


@pytest.mark.parametrize("change", [
    lambda report: {"outcome": "continue"},
    lambda report: {"outcome": "revise", "reason": "ValueError", "release_published": False},
    lambda report: dict(report, slots=report["slots"][:-1]),
    lambda report: dict(report, plan_sha256="0" * 64),
])
def test_signed_summary_error_or_incomplete_report_is_not_published(tmp_path, monkeypatch, change):
    args, _, _, _, path, events = decision_run(tmp_path, monkeypatch, report_change=change)
    retention = runner.execute(args)
    assert retention["verified"] is False and not path.exists()
    assert events.count("read_stdout") == 1 and "publish_decision" not in events


@pytest.mark.parametrize("refusal", ["occupied", "symlink"])
def test_exclusive_publication_preserves_occupied_target(tmp_path, monkeypatch, refusal):
    args, _, _, _, path, events = decision_run(tmp_path, monkeypatch, refusal=refusal)
    retention = runner.execute(args)
    assert retention["verified"] is False and retention["failure_classes"]
    assert events.count("publish_decision") == 1
    if refusal == "occupied":
        assert path.read_bytes() == b"occupied"
    else:
        assert path.is_symlink() and not path.resolve().exists()


def test_publication_readback_must_match_before_verified_retention(tmp_path, monkeypatch):
    args, _, _, _, _, events = decision_run(tmp_path, monkeypatch, refusal="readback_mutation")
    retention = runner.execute(args)
    assert retention["verified"] is False and retention["failure_classes"]
    assert retention["decision_raw_sha256"] is None
    assert events.index("read_decision") < events.index("publish_retention")


@pytest.mark.parametrize("with_output", [False, True])
def test_decide_cli_prints_entire_report_with_optional_output(tmp_path, monkeypatch, capsys, with_output):
    plan, _, pins, readers = fixture()
    plan["candidate_sha256"] = digest({})
    plan["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    report = {"outcome": "revise", "slots": ["all native obligations"], "full": "report"}
    monkeypatch.setattr(cli, "validate_plan", lambda *args: plan)
    monkeypatch.setattr(cli, "_read", lambda path: {})
    monkeypatch.setattr(cli, "candidate_manifest", lambda root: {})
    monkeypatch.setattr(cli, "evidence_pins", lambda *args: pins)
    monkeypatch.setattr(cli, "host_readers", lambda *args: readers)
    monkeypatch.setattr(cli, "_deadline", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(cli, "decide", lambda *args, **kwargs: report)
    published = []
    monkeypatch.setattr(cli, "_publish", lambda *args: published.append(args))
    argv = ["decide", "--plan", str(tmp_path / "plan.json"), "--expected-plan", digest(plan),
            "--originals", str(tmp_path / "originals.json"), "--expected-evidence", digest(pins)]
    if with_output:
        argv += ["--output", str(tmp_path / "decision.json")]
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().out) == report
    assert published == ([(tmp_path / "decision.json", report)] if with_output else [])


def test_freeze_cli_still_requires_output(monkeypatch, capsys):
    monkeypatch.setattr(cli, "reference_plan", lambda *args, **kwargs: pytest.fail("freeze must require output"))
    assert cli.main(["freeze"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "ValueError"


@pytest.mark.parametrize("attempt", ["decide", "replay"])
@pytest.mark.parametrize("mutation", [None, "hash", "size"])
def test_authenticated_stdout_reads_once_and_pins_before_parsing(tmp_path, monkeypatch, attempt, mutation):
    raw = b'{"complete": true}\n'
    path = tmp_path / (attempt + ".stdout")
    path.write_bytes(raw)
    path.chmod(0o600)
    stream = {"sha256": runner.raw_pin(raw), "retained_bytes": len(raw)}
    if mutation == "hash":
        stream["sha256"] = "0" * 64
    elif mutation == "size":
        stream["retained_bytes"] += 1
    verified = {"receipt": {"streams": {"stdout": stream}, "exit_code": 2, "status": "failed", "failure": None,
                            "output_overflow": False, "output_reader_work_unresolved": False,
                            "meter_work_unresolved": False, "meter_id": None}, "coverage_gap": None}
    actual_read, actual_parse = runner.read_private, runner._original
    events = []

    def read(*args):
        events.append("read")
        return actual_read(*args)

    def parse(raw):
        events.append("parse")
        return actual_parse(raw)

    monkeypatch.setattr(runner, "read_private", read)
    monkeypatch.setattr(runner, "_original", parse)
    if mutation:
        with pytest.raises(ValueError, match="native_authenticated_stdout_changed"):
            runner._authenticated_stdout({"receipt_directory": str(tmp_path)}, attempt, verified)
        assert events == ["read"]
    else:
        assert runner._authenticated_stdout({"receipt_directory": str(tmp_path)}, attempt, verified) == {"complete": True}
        assert events == ["read", "parse"]


@pytest.mark.parametrize("field,value", [
    ("failure", "TimeoutError"), ("output_overflow", True),
    ("output_reader_work_unresolved", True), ("meter_work_unresolved", True),
])
def test_replay_rejects_incomplete_authenticated_full_output(tmp_path, monkeypatch, field, value):
    args, _, _, _, path, events = decision_run(tmp_path, monkeypatch, outcome="revise", action="replay",
        receipt_change=lambda result: result["receipt"].update({field: value}))
    original = path.read_bytes()
    retention = runner.execute(args)
    assert retention["verified"] is False and retention["failure_classes"]
    assert path.read_bytes() == original
    assert "read_stdout" not in events and "publish_decision" not in events


def test_replay_keeps_legitimate_full_revise_exit_two(tmp_path, monkeypatch):
    args, _, _, _, path, events = decision_run(tmp_path, monkeypatch, outcome="revise", action="replay")
    original = path.read_bytes()
    retention = runner.execute(args)
    assert retention["verified"] is True and retention["failure_classes"] == []
    assert path.read_bytes() == original and events.count("read_stdout") == 1
    assert "publish_decision" not in events


@pytest.mark.parametrize("field,value", [
    (None, None), ("failure", "TimeoutError"), ("output_overflow", True),
    ("output_reader_work_unresolved", True), ("meter_work_unresolved", True),
])
def test_hidden_replay_rechecks_complete_authenticated_observation(tmp_path, monkeypatch, field, value):
    args, _, _, verified, path, events = decision_run(tmp_path, monkeypatch, outcome="revise", action="replay")
    root, study, plan, configs = runner.load_study(args)
    study["source_identity"] = {}
    config = configs["primary"]
    originals = runner.document(root / "control/originals.json")
    argv = [str(runner.ROOT / ".venv/bin/python"), str(runner.ROOT / "tools/run_repeated_integrations.py"), "replay",
            "--plan", str(root / "control/study-plan.json"), "--expected-plan", digest(plan),
            "--originals", str(root / "control/originals.json"), "--expected-evidence", args.expected_evidence,
            "--report", str(path)]
    monkeypatch.setattr(runner, "command_manifest", lambda argv: {"argv": argv})
    invocation = {"argv": argv, "command_manifest": {"argv": argv}, "study_sha256": digest(study),
                  "source_identity": {}}
    retention = {"verified": True, "output_hidden": True, "decision_raw_sha256": args.expected_decision_raw,
                 "originals_sha256": args.expected_originals, "evidence_sha256": args.expected_evidence,
                 "observation": copy.deepcopy(verified), "cleanup": {"confirmed": True}, "failure_classes": [],
                 "receipt_sha256": verified["receipt_sha256"], "invocation_sha256": digest(invocation)}
    args.expected_replay = digest(retention)
    documents = {"replay-retention.json": retention, "originals.json": originals, "replay.json": invocation}
    monkeypatch.setattr(runner, "document", lambda path: documents[Path(path).name])
    if field is not None:
        verified["receipt"][field] = value
        with pytest.raises(ValueError, match="native_decision_observation_incomplete"):
            runner.verify_hidden_replay(root, study, plan, config, args)
        assert "read_stdout" not in events
    else:
        runner.verify_hidden_replay(root, study, plan, config, args)
        assert events.count("read_stdout") == 1
