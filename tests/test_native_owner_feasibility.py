"""Private orchestration preserves native owner authority and missing obligations."""

import contextlib
import copy
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_integration_improvement import NOW, fixture
from tools import _integration_improvement as gate
from tools import run_native_owner_feasibility as runner
from tools import run_repeated_integrations as cli
from tools._release_decision import canonical, digest
from tools.run_comparative_trials import publish_new


def test_recipe_preserves_native_bounds_and_nine_obligations():
    recipe = runner.RECIPE
    assert recipe["authority_seconds"] == 7200
    assert recipe["witness_seconds"] == 30
    assert recipe["job_and_mcp_seconds"] == 600
    assert recipe["owner_pass_seconds"] == 120
    assert recipe["decision_seconds"] == 900
    assert recipe["owner_raw_bytes"] == 1_048_576
    assert recipe["owner_frame_bytes"] == 2_097_152
    assert recipe["native_artifact_bytes"] == 4_194_304
    assert recipe["denominator"] == 9 and recipe["retry_limit"] == 0


def test_producer_argv_is_the_native_owners_exact_eleven_arguments():
    root = Path("/private/tmp/native-fixture")
    argv = runner.producer_argv(root, "primary", "direct", "a" * 64)
    assert len(argv) == 11
    assert argv[:4] == [str(runner.ROOT / ".venv/bin/python"),
                        str(runner.ROOT / "tools/run_repeated_integrations.py"), "run", "--plan"]
    assert argv[5:] == ["--expected-plan", "a" * 64, "--host", "direct", "--directory",
                        str(root / "primary/work/direct/scenario")]


def test_custom_native_exception_class_names_are_not_exported():
    private = type("private_factory_key", (ValueError,), {})
    assert runner.exception_token(private("private diagnostic")) == "unknown"
    assert runner.exception_token(ValueError("private diagnostic")) == "ValueError"


def frozen(tmp_path, monkeypatch, *, mode="limited", execution_profile=None):
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
    root = tmp_path / "study"
    root.mkdir(mode=0o700)
    reporter = tmp_path / "archival-reporter.py"
    reporter.write_bytes(b"# archival source reference; never executed\n")
    reporter.chmod(0o600)
    proposals = tmp_path / "proposals.json"
    proposals.write_bytes(canonical([]))
    proposals.chmod(0o600)
    identity = runner._source_identity(reporter)
    args = SimpleNamespace(control=None, reporter=reporter, proposals=proposals,
        expected_proposals_raw=runner.raw_pin(proposals.read_bytes()), mode=mode,
        expected_candidate=identity["candidate_sha256"], expected_controller=identity["controller_sha256"],
        expected_adapter=identity["adapter_raw_sha256"], expected_reporter=identity["reference_reporter_raw_sha256"])
    if execution_profile is not None:
        args.execution_profile = execution_profile
    monkeypatch.setattr("tempfile.mkdtemp", lambda **_: str(root))
    result = runner.freeze_study(args)
    args.control = root
    args.expected_study = result["study_sha256"]
    args.expected_plan = result["plan_sha256"]
    monkeypatch.setattr(runner, "_external_root", lambda value: Path(value))
    return args, result, runner.load_study(args)


@pytest.mark.parametrize("mode,primary_hosts", [("limited", {"direct", "current_caller"}),
                                               ("full", set(runner.ROUTES))])
def test_freeze_predeclares_native_inputs_and_separate_audit_custody(tmp_path, monkeypatch, mode, primary_hosts):
    args, result, (root, study, plan, configs) = frozen(tmp_path, monkeypatch, mode=mode)
    assert result["native_execution_started"] is False
    assert len(plan["slots"]) == 9
    assert {s["cycle"] for s in plan["slots"]} == {"baseline", "change_1", "change_2"}
    assert plan["scope"]["native_run_seconds"] == runner.NATIVE_RUN_SECONDS
    assert all(s["elapsed_cap_seconds"] == 900 for s in plan["slots"])
    assert plan["scope"]["owner_read_workers"] == 3
    assert set(configs["primary"]["jobs"]) == primary_hosts
    assert set(configs["audit"]["jobs"]) == {"scoped_mcp"}
    assert Path(configs["primary"]["key_path"]).read_bytes() != Path(configs["audit"]["key_path"]).read_bytes()
    assert not list((root / "primary/receipts").iterdir())
    assert not list((root / "audit/work").iterdir())
    assert study["source_identity"]["native_reporter_raw_sha256"] != args.expected_reporter
    assert configs["primary"]["cost_plan"]["binding"] == {"integration_plan_sha256": digest(plan)}


def test_source_change_invalidates_frozen_study_without_renewal(tmp_path, monkeypatch):
    args, _, _ = frozen(tmp_path, monkeypatch)
    args.reporter.write_bytes(b"changed reference\n")
    with pytest.raises(ValueError, match="native_current_source_changed"):
        runner.load_study(args)


def test_current_runtime_config_mutation_is_rejected(tmp_path, monkeypatch):
    args, _, (root, _, _, _) = frozen(tmp_path, monkeypatch)
    path = root / "primary/control/runtime.json"
    changed = json.loads(path.read_bytes())
    changed["jobs"]["direct"][-1] = str(root / "different-fixture")
    path.write_bytes(canonical(changed))
    with pytest.raises(ValueError, match="native_runtime_external_pin"):
        runner.load_study(args)


def registry_fixture(tmp_path, monkeypatch, *, changes=None, include_cleanup=True):
    from tests._usage_fixture import require_reference_storage
    require_reference_storage()
    plan, _, _, _ = fixture()
    root = tmp_path / "registry"
    root.mkdir(mode=0o700)
    (root / "children").mkdir(mode=0o700)
    plan["scope"].update(native_child_control_directory=str(root), native_child_adapter_raw_sha256="a" * 64,
                          owner_read_controller_sha256="b" * 64)
    binding = {"schema_version": "native-owned-child/v1", "plan_sha256": digest(plan),
        "control_directory": str(root), "stage": "decide", "worker_pid": 10001,
        "adapter_raw_sha256": "a" * 64, "controller_sha256": "b" * 64,
        "candidate_sha256": plan["candidate_sha256"]}
    records = [("open", {"maximum_children": runner.MAX_CHILDREN}),
               ("spawn", {"pid": 10002, "pgid": 10002})]
    if include_cleanup:
        records += [("cleanup", {"pid": 10002, "pgid": 10002, "returncode": 2})]
    records += [("seal", {"spawn_count": 1, "cleanup_confirmed": True})]
    head, rows = "0" * 64, []
    for sequence, (kind, data) in enumerate(records):
        row = binding | {"sequence": sequence, "previous_sha256": head, "kind": kind, "data": data}
        if changes and sequence == 1:
            row.update(changes)
        raw = canonical(row)
        rows.append(raw)
        head = runner.raw_pin(raw)
    path = root / "children/decide.jsonl"
    path.write_bytes(b"".join(rows))
    path.chmod(0o600)
    monkeypatch.setattr(runner, "_group_alive", lambda _: False)
    return plan, path


def test_archived_child_registry_proves_complete_inventory_without_signalling(tmp_path, monkeypatch):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    plan, path = registry_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.os, "killpg", lambda *_: pytest.fail("archived custody must never signal"))
    result = runner.verify_child_registry(path, plan=plan, stage="decide", worker_pid=10001)
    assert result["spawn_count"] == 1 and result["cleanup_confirmed"] is True


@pytest.mark.parametrize("changes", [{"worker_pid": 10003}, {"candidate_sha256": "c" * 64},
                                     {"controller_sha256": "c" * 64}, {"sequence": 4},
                                     {"previous_sha256": "c" * 64}, {"data": {"authenticated": True}}])
def test_child_registry_cannot_replace_source_or_actual_owned_group(tmp_path, monkeypatch, changes):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    plan, path = registry_fixture(tmp_path, monkeypatch, changes=changes)
    with pytest.raises(ValueError):
        runner.verify_child_registry(path, plan=plan, stage="decide", worker_pid=10001)


def test_missing_child_cleanup_and_live_residual_group_are_fail_closed(tmp_path, monkeypatch):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    plan, path = registry_fixture(tmp_path, monkeypatch, include_cleanup=False)
    with pytest.raises(ValueError, match="native_child_registry_seal"):
        runner.verify_child_registry(path, plan=plan, stage="decide", worker_pid=10001)
    raw = json.loads(path.read_bytes().splitlines()[0])
    assert raw["worker_pid"] == 10001


def test_sealed_registry_still_requires_current_group_absence(tmp_path, monkeypatch):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    plan, path = registry_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "_group_alive", lambda _: True)
    with pytest.raises(ValueError, match="native_child_group_still_live"):
        runner.verify_child_registry(path, plan=plan, stage="decide", worker_pid=10001)


def test_full_decision_grammar_keeps_failure_and_missing_slots():
    plan, _, pins, readers = fixture({"direct:baseline": {"integration_success": False, "accepted": False,
                                                       "useful": None, "review_attempts": None}})
    selected = tuple(reader for reader in readers if not reader.slot_id.startswith("scoped_mcp:"))
    report = gate.decide(plan, digest(plan), pins, digest(pins), selected, now=NOW)
    assert runner.full_report(report, plan, digest(pins))
    assert report["outcome"] == "revise" and len(report["slots"]) == 9
    failed = next(row for row in report["slots"] if row["slot"]["slot_id"] == "direct:baseline")
    assert failed["evidence"]["projection"]["review_attempts"] is None
    missing = [row for row in report["slots"] if row["slot"]["host_id"] == "scoped_mcp"]
    assert len(missing) == 3 and all(row["evidence"] is None for row in missing)
    changed = copy.deepcopy(report)
    changed["authenticated"] = True
    assert not runner.full_report(changed, plan, digest(pins))
    changed = copy.deepcopy(report)
    changed["slots"].pop()
    assert not runner.full_report(changed, plan, digest(pins))
    assert not runner.full_report({"outcome": "continue", "authenticated": True}, plan, digest(pins))


def test_unconfirmed_cleanup_preserves_inventory_and_raw_failure(tmp_path, monkeypatch):
    args, _, (root, _, plan, configs) = frozen(tmp_path, monkeypatch)
    config = configs["primary"]
    path = root / "primary/work/direct/scenario"
    path.mkdir(parents=True, mode=0o700)
    report = {"failure": {"kind": "TimeoutError"}, "review_count": None}
    publish_new(path / "report.json", report)
    original = {"path": str(path / "report.json"), "raw_sha256": runner.raw_pin((path / "report.json").read_bytes()),
                "source_head_sha256": digest(report), "observation": {}}
    observed = {"receipt": {"status": "failed"}}
    anchor = {"host": "direct", "role": "primary", "observation": observed, "original": original,
              "cleanup": None, "cleanup_error_class": "RuntimeError"}
    publish_new(root / "primary/anchors/direct.json", anchor)
    publish_new(root / "control/primary-direct-pin.json", {"anchor_sha256": digest(anchor),
        "receipt_sha256": "d" * 64, "runtime_control_sha256": digest(config), "plan_sha256": digest(plan)})
    monkeypatch.setattr(runner, "_verify_observation", lambda *_, **__: (observed, 10001))
    def unclosed(*_, **__):
        raise RuntimeError("private diagnostic")
    monkeypatch.setattr(runner, "verify_child_registry", unclosed)
    retained = runner._originals(root, plan, configs)
    assert retained["denominator"] == 9 and len(retained["inventory"]) == 9
    direct = [row for row in retained["inventory"] if row["host_id"] == "direct"]
    assert all(row["producer_available"] and not row["original_selected"] for row in direct)
    assert all(row["review_count"] is None and row["acceptance"] is None for row in retained["inventory"])
    assert runner.document(path / "report.json") == report
    assert runner.document(root / "control/originals.json") == {}


@pytest.mark.parametrize("reaped,residual", [(False, False), (True, False), (True, True)])
def test_cleanup_signals_only_actual_live_owned_objects(tmp_path, monkeypatch, reaped, residual):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    plan, _, _, _ = fixture()
    root = tmp_path / "cleanup"
    root.mkdir(mode=0o700)
    (root / "children").mkdir(mode=0o700)
    plan["scope"]["native_child_control_directory"] = str(root)
    process = SimpleNamespace(pid=10002, returncode=2 if reaped else None)
    process.poll = lambda: process.returncode
    stopped = []
    def stop(actual):
        assert actual is process
        stopped.append(actual)
        process.returncode = 2
    monkeypatch.setattr(runner, "_cleanup_binding", lambda *args: (root, "run-primary-direct"))
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(runner, "_stop_owned_group", stop)
    monkeypatch.setattr(runner, "_group_alive", lambda actual: residual)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    def run():
        with runner.native_cleanup_context(plan, "run", "direct", root / "unused"):
            assert runner.subprocess.Popen(["fixed-native-command"], start_new_session=True) is process
    if residual:
        with pytest.raises(RuntimeError, match="native_child_cleanup_unconfirmed"):
            run()
    else:
        run()
    assert len(stopped) == (0 if reaped else 1)
    rows = [json.loads(raw) for raw in (root / "children/run-primary-direct.jsonl").read_bytes().splitlines()]
    assert rows[-1]["data"]["cleanup_confirmed"] is (not residual)
    assert b"fixed-native-command" not in (root / "children/run-primary-direct.jsonl").read_bytes()


def test_original_provisioning_exception_still_closes_owned_children(tmp_path, monkeypatch):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    plan, _, _, _ = fixture()
    root = tmp_path / "failed-provision"
    root.mkdir(mode=0o700)
    (root / "children").mkdir(mode=0o700)
    plan["scope"]["native_child_control_directory"] = str(root)
    process = SimpleNamespace(pid=10002, returncode=None)
    process.poll = lambda: process.returncode
    monkeypatch.setattr(runner, "_cleanup_binding", lambda *args: (root, "run-primary-direct"))
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(runner, "_stop_owned_group", lambda actual: setattr(actual, "returncode", -15))
    monkeypatch.setattr(runner, "_group_alive", lambda actual: False)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(ValueError, match="fixture provisioning failed"):
        with runner.native_cleanup_context(plan, "run", "direct", root / "unused"):
            runner.subprocess.Popen(["fixed-native-command"], start_new_session=True)
            raise ValueError("fixture provisioning failed")
    assert process.returncode == -15
    rows = [json.loads(raw) for raw in (root / "children/run-primary-direct.jsonl").read_bytes().splitlines()]
    assert rows[-1]["kind"] == "seal" and rows[-1]["data"]["cleanup_confirmed"] is True


def test_production_cleanup_closes_before_final_decision_clock(tmp_path, monkeypatch):
    plan, _, _, _ = fixture()
    plan["candidate_sha256"] = digest({"candidate": "fixed"})
    plan["scope"].update(native_child_control_directory=str(tmp_path), owner_read_workers=3)
    for host in plan["hosts"]:
        host["host_sha256"] = digest({"route": host["route"]})
    events = []
    @contextlib.contextmanager
    def cleanup(*args):
        events.append("cleanup_open")
        try:
            yield
        finally:
            events.append("cleanup_closed")
    monkeypatch.setattr(runner, "native_cleanup_context", cleanup)
    monkeypatch.setattr(cli, "_read", lambda path: plan if path.name == "plan.json" else {})
    monkeypatch.setattr(cli, "candidate_manifest", lambda root: {"candidate": "fixed"})
    monkeypatch.setattr(cli, "host_identity", lambda route: {"route": route})
    monkeypatch.setattr(cli, "evidence_pins", lambda *args: {})
    controller = SimpleNamespace(readers=(), prepare_pass=lambda: events.append("prepare_pass"),
                                 closing_check=lambda: events.append("controller_closed"))
    monkeypatch.setattr(cli, "owner_pass", lambda *args, **kwargs: controller)
    def decide(*args, **kwargs):
        kwargs["prepare_pass"]()
        kwargs["prepare_pass"]()
        kwargs["closing_check"]()
        events.append("final_clock")
        return {"outcome": "continue"}
    monkeypatch.setattr(cli, "decide", decide)
    monkeypatch.setattr(cli, "_publish", lambda *args: events.append("published"))
    # The test observes the CLI wiring only; no native producer/owner executes.
    monkeypatch.setattr(cli, "_deadline", lambda *args: contextlib.nullcontext())
    plan["frozen_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    plan["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = cli.main(["decide", "--plan", str(tmp_path / "plan.json"), "--expected-plan", digest(plan),
                       "--originals", str(tmp_path / "originals.json"), "--expected-evidence", digest({}),
                       "--output", str(tmp_path / "decision.json")])
    assert result == 0
    assert events == ["cleanup_open", "prepare_pass", "prepare_pass", "controller_closed",
                      "cleanup_closed", "final_clock", "published"]


@pytest.mark.parametrize("action", ["run", "decide", "decide-stdout", "replay"])
def test_cleanup_binding_rejects_changed_actual_argv(tmp_path, monkeypatch, action):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    stdout_only = action == "decide-stdout"
    if stdout_only:
        action = "decide"
    plan, _, _, _ = fixture()
    (tmp_path / "children").mkdir(mode=0o700)
    (tmp_path / "control").mkdir(mode=0o700)
    publish_new(tmp_path / "control/originals.json", {})
    plan["candidate_sha256"] = digest({"candidate": "fixed"})
    plan["scope"].update(native_child_control_directory=str(tmp_path), native_child_adapter_raw_sha256="a" * 64,
                         owner_read_controller_sha256=digest({}), native_cleanup_recipe="owned-popen-groups/v1")
    monkeypatch.setattr(runner, "_external_root", lambda path: Path(path))
    monkeypatch.setattr(runner, "_adapter_pin", lambda: "a" * 64)
    monkeypatch.setattr(runner, "source_manifest", lambda: {})
    monkeypatch.setattr(runner, "candidate_manifest", lambda path: {"candidate": "fixed"})
    if action == "run":
        argv = runner.producer_argv(tmp_path, "primary", "direct", digest(plan))
        host, directory = "direct", tmp_path / "primary/work/direct/scenario"
    else:
        argv = [str(runner.ROOT / ".venv/bin/python"), str(runner.ROOT / "tools/run_repeated_integrations.py"), action,
                "--plan", str(tmp_path / "control/study-plan.json"), "--expected-plan", digest(plan),
                "--originals", str(tmp_path / "control/originals.json"), "--expected-evidence", digest({}),
                "--output" if action == "decide" else "--report", str(tmp_path / "decisions/decision.json")]
        host, directory = None, None
        if stdout_only:
            argv = argv[:-2]
    monkeypatch.setattr(runner.sys, "argv", argv[1:])
    assert runner._cleanup_binding(plan, action, host, directory)[0] == tmp_path
    monkeypatch.setattr(runner.sys, "argv", argv[1:-1] + [str(tmp_path / "different-destination")])
    with pytest.raises(ValueError, match="native_cleanup_actual_"):
        runner._cleanup_binding(plan, action, host, directory)


def test_background_launch_transaction_finishes_before_interrupted_cleanup(tmp_path, monkeypatch):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    plan, _, _, _ = fixture()
    (tmp_path / "children").mkdir(mode=0o700)
    plan["scope"]["native_child_control_directory"] = str(tmp_path)
    monkeypatch.setattr(runner, "_cleanup_binding", lambda *args: (tmp_path, "run-primary-direct"))
    real_popen, started, processes, errors = subprocess.Popen, threading.Event(), [], []
    def slow_owned_launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        started.set()
        time.sleep(0.05)
        return process
    monkeypatch.setattr(runner.subprocess, "Popen", slow_owned_launch)
    def background():
        try:
            runner.subprocess.Popen([str(runner.ROOT / ".venv/bin/python"), "-c", "import time;time.sleep(10)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=background)
    with pytest.raises(ValueError, match="interrupted fixture"):
        with runner.native_cleanup_context(plan, "run", "direct", tmp_path / "unused"):
            thread.start()
            assert started.wait(timeout=2)
            raise ValueError("interrupted fixture")
    thread.join(timeout=2)
    assert not thread.is_alive() and not errors and len(processes) == 1
    assert processes[0].poll() is not None and not runner._group_alive(processes[0])
    assert processes[0].pid != os.getpid()
    rows = [json.loads(raw) for raw in (tmp_path / "children/run-primary-direct.jsonl").read_bytes().splitlines()]
    assert [row["kind"] for row in rows] == ["open", "spawn", "cleanup", "seal"]
    assert rows[-1]["data"] == {"spawn_count": 1, "cleanup_confirmed": True}


def test_production_cleanup_failure_emits_one_fixed_token_without_traceback(tmp_path, monkeypatch, capsys):
    plan, _, _, _ = fixture()
    plan["candidate_sha256"] = digest({"candidate": "fixed"})
    plan["frozen_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    plan["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    plan["scope"]["native_child_control_directory"] = str(tmp_path)
    private_error = type("private_factory_key", (RuntimeError,), {})
    @contextlib.contextmanager
    def cleanup(*args):
        try:
            yield
        finally:
            raise private_error("private factory diagnostic")
    def failed_provision(*args, **kwargs):
        raise ValueError("private provisioning diagnostic")
    monkeypatch.setattr(runner, "native_cleanup_context", cleanup)
    monkeypatch.setattr(cli, "_read", lambda path: plan)
    monkeypatch.setattr(cli, "candidate_manifest", lambda root: {"candidate": "fixed"})
    monkeypatch.setattr(cli, "run_scenario", failed_provision)
    assert cli.main(["run", "--plan", str(tmp_path / "plan.json"), "--expected-plan", digest(plan),
                     "--host", "direct", "--directory", str(tmp_path / "scenario")]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"outcome": "revise", "reason": "unknown", "release_published": False}
    assert "private" not in captured.out


def test_owned_signal_holds_actual_wait_lock_through_identity_and_signal(monkeypatch):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    process = SimpleNamespace(pid=10002, returncode=None, _waitpid_lock=threading.Lock())
    signals = []
    def group(pid):
        assert process._waitpid_lock.locked()
        return pid
    def send(pid, selected_signal):
        assert process._waitpid_lock.locked() and process.returncode is None
        signals.append((pid, selected_signal))
    monkeypatch.setattr(runner.os, "getpgid", group)
    monkeypatch.setattr(runner.os, "killpg", send)
    runner._signal_owned_group(process, signal.SIGTERM)
    assert signals == [(10002, signal.SIGTERM)]
    assert not process._waitpid_lock.locked()
    process.returncode = 2
    runner._signal_owned_group(process, signal.SIGKILL)
    assert signals == [(10002, signal.SIGTERM)]


@pytest.mark.parametrize("state", ["already_reaped", "reaped_after_term", "live_until_kill"])
def test_safe_stopper_cannot_signal_a_reaped_leader_residual_group(monkeypatch, state):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    process = SimpleNamespace(pid=10002, returncode=2 if state == "already_reaped" else None,
                              _waitpid_lock=threading.Lock())
    signals, waits = [], []
    def send(pid, selected_signal):
        assert process._waitpid_lock.locked() and process.returncode is None
        signals.append(selected_signal)
        if selected_signal == signal.SIGKILL:
            process.returncode = -9
    def wait(*, timeout):
        waits.append(timeout)
        if process.returncode is None:
            if state == "reaped_after_term":
                # Model the native watcher reaping between wait and escalation.
                process.returncode = 2
            raise subprocess.TimeoutExpired("owned-native-worker", timeout)
        return process.returncode
    process.wait = wait
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(runner.os, "killpg", send)
    monkeypatch.setattr(runner, "_group_alive", lambda actual: state != "live_until_kill")
    if state == "live_until_kill":
        runner._stop_owned_group(process)
        assert signals == [signal.SIGTERM, signal.SIGKILL]
    else:
        with pytest.raises(ValueError, match="native_child_cleanup_unconfirmed"):
            runner._stop_owned_group(process)
        assert signals == ([] if state == "already_reaped" else [signal.SIGTERM])
    assert waits and all(timeout == 5 for timeout in waits)


def test_changed_group_identity_is_rejected_without_signal(monkeypatch):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    process = SimpleNamespace(pid=10002, returncode=None, _waitpid_lock=threading.Lock())
    monkeypatch.setattr(runner.os, "getpgid", lambda pid: pid + 1)
    monkeypatch.setattr(runner.os, "killpg", lambda *args: pytest.fail("changed group must never receive a signal"))
    with pytest.raises(ValueError, match="native_child_group_identity"):
        runner._signal_owned_group(process, signal.SIGTERM)
    assert not process._waitpid_lock.locked()
