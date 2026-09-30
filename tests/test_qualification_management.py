"""Qualification progress, process lifetime, parallel isolation and complete evidence."""

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tests.test_release_workflow import inline_python, make_sdist, make_wheel, run_inline_python
from tools import probe_installed_cli as installed_cli
from tools import qualify_journeys as journeys
from tools import validate_installed_artifacts as artifacts
from tools import verify_installed_artifacts as verification
from tools._qualification_process import CommandRunner, positive_seconds, stop_process


@pytest.fixture
def installed_commands_fixture(tmp_path):
    """Model process observations without presenting the synthetic layout as an installed artifact."""
    environment = tmp_path / "environment"
    python, cli = artifacts.venv_python(environment), artifacts.venv_cli(environment)
    python.parent.mkdir(parents=True)
    python.touch()
    cli.touch()
    (environment / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="utf-8")
    site = environment / "lib/site-packages"
    package = site / "evidence_wiki"
    package.mkdir(parents=True)
    modules = {name: str(package / filename) for name, filename in (
        ("evidence_wiki", "__init__.py"), ("evidence_wiki.cli", "cli.py"), ("evidence_wiki.__main__", "__main__.py"))}
    for filename in modules.values():
        with open(filename, "w", encoding="utf-8"):
            pass
    identity = {"executable": str(python), "prefix": str(environment), "base_prefix": str(tmp_path / "host"),
                "package_version": "9.9.9", "distribution_version": "9.9.9", "distribution_root": str(site), "modules": modules}
    outside, checkout = tmp_path / "outside directory", tmp_path / "checkout"
    outside.mkdir()
    checkout.mkdir()
    calls = []
    reply = SimpleNamespace(returncode=0, stdout="result\n", stderr="")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if kwargs["label"] == "installation-identity":
            return subprocess.CompletedProcess(argv, 0, json.dumps(identity), "")
        return subprocess.CompletedProcess(argv, reply.returncode, reply.stdout, reply.stderr)

    def create(**overrides):
        options = {"cwd": outside, "checkout_root": checkout, "expected_version": "9.9.9",
                   "runner": SimpleNamespace(run=run, records=[]), **overrides}
        return installed_cli.InstalledCommands(python, cli, **options)

    return SimpleNamespace(create=create, identity=identity, calls=calls, reply=reply,
                           python=python, cli=cli, checkout=checkout, outside=outside)


@pytest.mark.parametrize("defect", ["prefix", "executable", "base_prefix", "distribution_version", "candidate_version", "distribution_root",
                                   "package_origin", "cli_origin", "main_origin", "missing_main"])
def test_installed_commands_reject_incompatible_identity(installed_commands_fixture, defect):
    """A selected version alone cannot substitute for matching interpreter and module origins."""
    fixture = installed_commands_fixture
    if defect in {"package_origin", "cli_origin", "main_origin", "missing_main"}:
        name = {"package_origin": "evidence_wiki", "cli_origin": "evidence_wiki.cli"}.get(defect, "evidence_wiki.__main__")
        fixture.identity["modules"][name] = None if defect == "missing_main" else str(fixture.checkout / "foreign.py")
    elif defect == "candidate_version":
        fixture.identity.update(package_version="8.8.8", distribution_version="8.8.8")
    else:
        fixture.identity[defect] = fixture.identity["prefix"] if defect == "base_prefix" else "different"
    with pytest.raises(ValueError):
        fixture.create()
    assert len(fixture.calls) == 1


@pytest.mark.parametrize("location", ["cwd", "environment"])
def test_installed_commands_require_external_execution_locations(installed_commands_fixture, location):
    fixture = installed_commands_fixture
    options = {"cwd": fixture.checkout} if location == "cwd" else {"checkout_root": fixture.python.parent.parent}
    with pytest.raises(ValueError, match="outside the checkout"):
        fixture.create(**options)
    assert fixture.calls == []


def test_installed_commands_preserve_argv_expected_exit_and_environment(installed_commands_fixture, monkeypatch):
    fixture = installed_commands_fixture
    monkeypatch.setenv("PYTHONPATH", str(fixture.checkout))
    monkeypatch.setenv("PYTHONHOME", str(fixture.checkout))
    monkeypatch.setenv("PATH", str(fixture.python.parent) + os.pathsep + str(fixture.outside))
    commands = fixture.create()
    argv = [str(fixture.python), "-B", "-m", "evidence_wiki", "agent", "apply", "--from-file", "plan with spaces.json"]
    original = list(argv)
    fixture.reply.returncode = 3
    result = commands.command(argv, label="refusal", expected=3, timeout=17)
    called, options = fixture.calls[-1]
    assert called is argv and argv == original and result.returncode == 3
    assert options["expected"] == (3,) and options["timeout"] == 17
    assert options["cwd"] == fixture.outside.resolve()
    assert "PYTHONPATH" not in options["env"] and "PYTHONHOME" not in options["env"]
    assert options["env"]["PATH"] == str(fixture.outside)
    assert options["env"]["PYTHONNOUSERSITE"] == options["env"]["PYTHONDONTWRITEBYTECODE"] == "1"


def test_installed_commands_do_not_fallback_after_failure(installed_commands_fixture):
    fixture = installed_commands_fixture
    commands = fixture.create()
    fixture.reply.returncode, fixture.reply.stderr = 1, "package entry point missing"
    argv = [str(fixture.python), "-B", "-m", "evidence_wiki", "--version"]
    with pytest.raises(ValueError, match="package entry point missing"):
        commands.command(argv, label="package-version")
    assert len(fixture.calls) == 2 and fixture.calls[-1][0] is argv
    with pytest.raises(ValueError, match="observed installation launcher"):
        commands.command(["evidence-wiki", "--version"], label="unbound")
    assert len(fixture.calls) == 2


@pytest.mark.parametrize("group", installed_cli.OUTCOME_CASES)
@pytest.mark.parametrize("defect", [None, "missing", "extra", "failed", "not_run", "unknown", "not_mapping"])
def test_installed_outcomes_require_every_successful_case(group, defect):
    outcomes = dict.fromkeys(installed_cli.OUTCOME_CASES[group], "passed")
    first = next(iter(outcomes))
    if defect == "missing":
        outcomes.pop(first)
    elif defect == "extra":
        outcomes["unobserved"] = "passed"
    elif defect in {"failed", "not_run", "unknown"}:
        outcomes[first] = defect
    elif defect == "not_mapping":
        outcomes = list(outcomes)
    if defect is None:
        assert installed_cli.checked_outcomes(group, outcomes) == outcomes
    else:
        with pytest.raises(ValueError, match="Incomplete installed outcomes"):
            installed_cli.checked_outcomes(group, outcomes)


def test_installed_command_helper_is_a_bound_qualification_input():
    path = "tools/probe_installed_cli.py"
    assert path in artifacts.REQUIRED_SDIST_MEMBERS and path in artifacts.fixture_members()
    assert path in artifacts.validation_identity()


@pytest.mark.parametrize("kind", ["empty", "local_source"])
@pytest.mark.parametrize("defect", [None, "missing_inspection", "wrong_target", "wrong_plan", "wrong_selector",
                                   "inspection_write", "transaction", "replay_write", "source_gap"])
def test_installed_setup_actions_require_selected_workspace_postconditions(tmp_path, kind, defect):
    target = tmp_path / "selected workspace"
    target.mkdir()
    retained = target / "retained.txt"
    retained.write_text("original", encoding="utf-8")
    paths = ["raw/source.txt"] if kind == "local_source" else []
    inspect = ["selected-python", "-B", "-m", "evidence_wiki", "agent", "source-status" if paths else "inspect",
               "--target", str(target), "--format", "json"]
    for path in paths:
        inspect.extend(["--source-path", path])
    replay = ["selected-python", "-B", "-m", "evidence_wiki", "agent", "apply", "--from-file", str(tmp_path / "plan.json")]
    receipt = {"target": str(target), "checkpoint": str(tmp_path / "checkpoint.json"),
               "plan_id": "plan", "transaction_id": "transaction",
               "sources": [{"observations": [{"raw_paths": paths}]}] if paths else [],
               "next_actions": [{"action": "inspect_sources", "argv": inspect}, {"action": "resume_setup", "argv": replay}]}
    inspection = {"schema_version": "evidence-source-inspection/v1", "target": {"selected": True, "state": "present"},
                  "research_ready": False, "sources": [{"raw_paths": paths, "usability": "usable"}] if paths else []}
    resumed = {"schema_version": "evidence-setup-result/v1", "status": "ready", "setup_ready": True,
               "plan_id": "plan", "transaction_id": "transaction", "research_complete": False, "claims_verified": False}
    if defect == "missing_inspection":
        receipt["next_actions"].pop(0)
    elif defect == "wrong_target":
        inspect[inspect.index("--target") + 1] = str(tmp_path / "other")
    elif defect == "wrong_plan":
        replay[-1] = str(tmp_path / "other.json")
    elif defect == "wrong_selector":
        inspect.extend(["--source-path", "raw/other.txt"])
    elif defect == "transaction":
        resumed["transaction_id"] = "different"
    elif defect == "source_gap":
        inspection["sources"] = [{"raw_paths": ["raw/other.txt"], "usability": "unusable"}]
    original = copy.deepcopy(receipt)
    calls = []

    def command(argv, *, label, timeout):
        calls.append(argv)
        assert timeout == 120
        is_inspection = label.endswith("-inspection")
        if defect == ("inspection_write" if is_inspection else "replay_write"):
            retained.write_text("changed", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps(inspection if is_inspection else resumed), "")

    if defect:
        with pytest.raises(ValueError):
            installed_cli.setup_next_actions(SimpleNamespace(command=command), receipt, target=target, kind=kind)
    else:
        assert installed_cli.setup_next_actions(SimpleNamespace(command=command), receipt, target=target, kind=kind) == {
            kind + "_inspection": "passed", kind + "_replay": "passed"}
        assert len(calls) == 2 and calls[0] is inspect and calls[1] is replay
        assert receipt == original and retained.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("operation", ["start", "heartbeat"])
@pytest.mark.parametrize("defect", [None, "missing", "duplicate", "expired", "authorized", "executed", "evidence_accepted",
                                   "wrong_target", "wrong_actor", "result_actor", "result_run", "result_operation", "complete", "stderr"])
def test_installed_research_action_preserves_advice_and_caller_identity(tmp_path, operation, defect):
    argv = ["selected-python", "-B", "-m", "evidence_wiki", "agent", operation,
            "--target", str(tmp_path), "--agent-id", "current"]
    run_id = "generated-run" if operation == "heartbeat" else None
    if run_id is not None:
        argv.extend(["--run-id", run_id])
    action = {"operation": operation, "argv": argv, "authorized": False, "evidence_accepted": False,
              "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}
    advice = {"actions_executed": False, "research_complete": False, "actions": [action,
              {"operation": "stop", "authorized": False, "evidence_accepted": False, "parameters": {"reason": "inspect"}}]}
    result = {"schema_version": "evidence-caller-run-result/v1", "operation": operation, "research_complete": False,
              "run": {"run_id": "generated-run", "agent_id": "current", "caller_context": {"context_id": "controls"}}}
    if defect == "missing":
        advice["actions"].pop(0)
    elif defect == "duplicate":
        advice["actions"].append(copy.deepcopy(action))
    elif defect == "expired":
        action["expires_at"] = "2000-01-01T00:00:00+00:00"
    elif defect in {"authorized", "evidence_accepted"}:
        action[defect] = True
    elif defect == "executed":
        advice["actions_executed"] = True
    elif defect == "wrong_target":
        argv[argv.index("--target") + 1] = str(tmp_path / "other")
    elif defect == "wrong_actor":
        argv[argv.index("--agent-id") + 1] = "other"
    elif defect == "result_actor":
        result["run"]["agent_id"] = "other"
    elif defect == "result_run":
        result["run"]["run_id"] = "other" if run_id else ""
    elif defect == "result_operation":
        result["operation"] = "other"
    elif defect == "complete":
        result["research_complete"] = True
    original, calls = copy.deepcopy(advice), []

    def command(observed, *, label, timeout):
        calls.append(observed)
        assert label == operation and timeout == 120
        return subprocess.CompletedProcess(observed, 0, json.dumps(result), "unexpected" if defect == "stderr" else "")

    options = dict(operation=operation, target=tmp_path, agent_id="current", run_id=run_id)
    if defect:
        with pytest.raises(ValueError):
            installed_cli.research_next_action(SimpleNamespace(command=command), advice, **options)
    else:
        assert installed_cli.research_next_action(SimpleNamespace(command=command), advice, **options) == result
        assert len(calls) == 1 and calls[0] is argv
    assert advice == original


@pytest.mark.parametrize("defect", [None, "empty_package", "wrong_version", "missing_schema", "wrong_refusal", "stderr", "help_parity"])
def test_installed_entrypoint_report_requires_real_case_results(installed_commands_fixture, defect):
    """All three launchers must satisfy the complete behavior matrix before any report is returned."""
    fixture = installed_commands_fixture
    commands = fixture.create()
    calls = []

    def run(argv, **options):
        calls.append((list(argv), options))
        arguments = argv[1:] if argv[0] == str(fixture.cli) else argv[4:]
        case = options["label"].split("-", 1)[1]
        stderr = ""
        if case == "help":
            assert arguments == ["--help"]
            stdout = "evidence-wiki: research\nUsage:\nevidence-wiki agent apply\n"
        elif case == "version":
            assert arguments == ["--version"]
            stdout = "evidence-wiki 9.9.9\n"
        elif case == "schemas":
            assert arguments == ["agent", "source-schemas", "--format", "json"]
            stdout = json.dumps({"schema_ids": ["evidence-source-inspection/v1"]})
        elif case == "unknown_command":
            assert arguments == ["unknown-command"]
            stdout, stderr = "", "usage: evidence-wiki\nerror: unknown command: unknown-command\n"
        else:
            assert arguments[:3] == ["agent", "apply", "--from-file"]
            with open(arguments[3], encoding="utf-8") as source:
                assert json.load(source) == {}
            stdout = json.dumps({"schema_version": "1.0", "error_code": "ONBOARDING_INVALID", "recoverable": False,
                                 "details": {}, "message": "Invalid input", "remediation": "Inspect the input"})
        if options["label"].startswith("package_module-"):
            if defect == "empty_package":
                stdout, stderr = "", ""
            elif defect == "wrong_version" and case == "version":
                stdout = "evidence-wiki 8.8.8\n"
            elif defect == "missing_schema" and case == "schemas":
                stdout = '{"schema_ids":[]}'
            elif defect == "wrong_refusal" and case == "invalid_plan":
                stdout = stdout.replace("ONBOARDING_INVALID", "ONBOARDING_WRITE_FAILED")
            elif defect == "stderr" and case == "schemas":
                stderr = "unexpected diagnostic\n"
            elif defect == "help_parity" and case == "help":
                stdout += "Extra text\n"
        commands.runner.records.append({"stage": options["label"], "status": "passed", "exit_code": options["expected"][0]})
        return subprocess.CompletedProcess(argv, options["expected"][0], stdout, stderr)

    commands.runner.run = run
    if defect:
        with pytest.raises(ValueError):
            installed_cli.entrypoint_report(commands)
    else:
        report = installed_cli.entrypoint_report(commands)
        assert report["cli_entrypoints"] == dict.fromkeys(installed_cli.OUTCOME_CASES["cli_entrypoints"], "passed")
        assert report["cli_entrypoint_identity"] == {**fixture.identity, "console_script": str(fixture.cli)}
        assert len(calls) == 15
        assert sum(options["expected"] == (2,) for _, options in calls) == 6
        assert not list(fixture.outside.iterdir())


@pytest.mark.parametrize("payload", [None, [], {}, {"cli_entrypoints": {"package_module": "passed"}}])
def test_installed_cli_adapter_refuses_incomplete_probe_reports(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(artifacts, "run", lambda *args, **kwargs: json.dumps(payload))
    with pytest.raises(ValueError, match="Incomplete installed outcomes"):
        artifacts.validate_cli_entrypoints(tmp_path/"python", tmp_path/"evidence-wiki", outside=tmp_path,
                                            fixture_root=tmp_path, output=tmp_path/"commands")


@pytest.mark.parametrize("kind", ["setup", "research"])
@pytest.mark.parametrize("payload", [None, [], {}, "partial", "complete"])
def test_installed_action_adapter_requires_complete_probe_reports(tmp_path, monkeypatch, kind, payload):
    group = "generated_" + kind + "_actions"
    complete = payload == "complete"
    if payload in ("complete", "partial"):
        payload = {group: dict.fromkeys(installed_cli.OUTCOME_CASES[group], "passed")}
        if not complete:
            payload[group].pop(next(iter(payload[group])))

    def run(argv, **options):
        assert argv[:5] == [str(tmp_path / "python"), "-B", "-I", "-c", getattr(artifacts, kind.upper() + "_PROBE")]
        assert argv[5:] == list(map(str, [tmp_path / "evidence-wiki", tmp_path / "scenario",
            tmp_path / "tools/probe_installed_cli.py", artifacts.REPO_ROOT, tmp_path / "commands"]))
        assert options == {"cwd": tmp_path, "label": kind + "-actions"}
        return json.dumps(payload)

    monkeypatch.setattr(artifacts, "run", run)
    options = dict(kind=kind, root=tmp_path / "scenario", outside=tmp_path, fixture_root=tmp_path, output=tmp_path / "commands")
    if complete:
        assert artifacts.validate_action_probe(tmp_path / "python", tmp_path / "evidence-wiki", **options) == payload
    else:
        with pytest.raises(ValueError, match="Incomplete installed outcomes"):
            artifacts.validate_action_probe(tmp_path / "python", tmp_path / "evidence-wiki", **options)


@pytest.mark.parametrize("label", ["wheel", "sdist"])
@pytest.mark.parametrize("retain", [False, True])
def test_installed_validation_runs_the_cli_probe_with_retained_diagnostics(tmp_path, monkeypatch, label, retain):
    """Both installation paths enter the shared probe and honor the chosen diagnostics destination."""
    environment, scratch = tmp_path/"environment", tmp_path/"execution"
    cli = artifacts.venv_cli(environment)
    cli.parent.mkdir(parents=True)
    cli.touch()
    scratch.mkdir()
    fixtures = scratch/"qualification-inputs"
    evidence = tmp_path/"evidence" if retain else None
    monkeypatch.setattr(artifacts._OPTIONS, "evidence", evidence)
    monkeypatch.setattr(artifacts, "isolated_fixtures", lambda root: fixtures)
    monkeypatch.setattr(artifacts, "run", lambda *args, **kwargs: pytest.fail("unselected legacy probe"))

    def probe(python, selected_cli, **options):
        assert python == artifacts.venv_python(environment) and selected_cli == cli
        assert options["outside"] == scratch/"outside-checkout" and options["outside"].is_dir()
        assert options["fixture_root"] == fixtures and options["expected_version"] == "9.9.9"
        expected = evidence/label/"cli-entrypoint-commands" if retain else scratch/"cli-entrypoint-commands"
        assert options["output"] == expected
        raise RuntimeError("entrypoint probe reached")

    monkeypatch.setattr(artifacts, "validate_cli_entrypoints", probe)
    with pytest.raises(RuntimeError, match="entrypoint probe reached"):
        artifacts.validate_installed(environment, scratch, "9.9.9", label)


@pytest.mark.parametrize("label", ["wheel", "sdist"])
@pytest.mark.parametrize("retain", [False, True])
def test_installed_validation_wires_both_action_probes_and_log_destinations(tmp_path, monkeypatch, label, retain):
    environment, scratch = tmp_path / "environment", tmp_path / "execution"
    cli = artifacts.venv_cli(environment)
    cli.parent.mkdir(parents=True)
    cli.touch()
    scratch.mkdir()
    fixtures = scratch / "qualification-inputs"
    evidence = tmp_path / "evidence" if retain else None
    monkeypatch.setattr(artifacts._OPTIONS, "evidence", evidence)
    monkeypatch.setattr(artifacts, "isolated_fixtures", lambda root: fixtures)
    monkeypatch.setattr(artifacts, "validate_cli_entrypoints", lambda *args, **kwargs: {})
    monkeypatch.setattr(artifacts, "validate_html_installation", lambda *args, **kwargs: html_observations())

    def legacy(argv, **options):
        assert artifacts.SETUP_PROBE not in argv and artifacts.RESEARCH_PROBE not in argv
        return json.dumps({"pending_action_id": "action-0001"})

    calls = []

    def probe(python, selected_cli, **options):
        kind = options["kind"]
        calls.append(kind)
        assert python == artifacts.venv_python(environment) and selected_cli == cli
        assert options["outside"] == scratch / "outside-checkout" and options["fixture_root"] == fixtures
        assert options["root"] == scratch / ("caller research" if kind == "research" else "workspace application")
        expected = evidence / label if retain else scratch
        assert options["output"] == expected / (kind + "-action-commands")
        if kind == "setup":
            raise RuntimeError("both probes reached")
        return {"generated_research_actions": {"start": "passed", "heartbeat": "passed"}}

    monkeypatch.setattr(artifacts, "run", legacy)
    monkeypatch.setattr(artifacts, "validate_action_probe", probe)
    with pytest.raises(RuntimeError, match="both probes reached"):
        artifacts.validate_installed(environment, scratch, "9.9.9", label)
    assert calls == ["research", "setup"]


def test_progress_is_live_and_json_stdout_remains_separate(tmp_path, capsys):
    runner = CommandRunner(tmp_path, heartbeat=0.05)
    result = runner.run([sys.executable, "-c", "import time; time.sleep(.2); print('{\"ok\": true}')"], label="probe")
    assert json.loads(result.stdout) == {"ok": True}
    captured = capsys.readouterr()
    assert not captured.out
    events = [json.loads(line) for line in (tmp_path / "progress.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "started" and events[-1]["event"] == "passed"
    assert any(row["event"] == "running" for row in events)
    assert '"event": "running"' in captured.err
    assert json.loads(next(tmp_path.glob("*.stdout.log")).read_text()) == {"ok": True}


def test_timeout_stops_children_and_keeps_partial_output(tmp_path):
    runner = CommandRunner(tmp_path / "logs", heartbeat=0.1)
    marker, started = tmp_path / "escaped", tmp_path / "started"
    child = f"import pathlib,time; pathlib.Path({str(started)!r}).touch(); time.sleep(2); pathlib.Path({str(marker)!r}).touch()"
    program = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); print('before-timeout',flush=True); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run([sys.executable, "-c", program], label="stalled", timeout=1)
    assert started.is_file(), "child must have started for the cleanup regression to be meaningful"
    assert "before-timeout" in next((tmp_path / "logs").glob("*.stdout.log")).read_text()
    assert runner.records[-1]["status"] == "timed_out"
    time.sleep(2.1)
    assert not marker.exists(), "the timed-out command left a child running"


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM cleanup is exercised on POSIX CI hosts")
def test_cancellation_unwinds_runner_and_preserves_diagnostics(tmp_path):
    started = tmp_path / "started"
    program = f"import pathlib,time; pathlib.Path({str(started)!r}).touch(); time.sleep(60)"
    nested = ("from tools._qualification_process import CommandRunner, interruptible\n"
              f"with interruptible(): CommandRunner({str(tmp_path / 'nested')!r}).run({[sys.executable, '-c', program]!r}, label='nested')\n")
    outer = ("from tools._qualification_process import CommandRunner, interruptible\n"
             f"with interruptible(): CommandRunner({str(tmp_path / 'logs')!r}).run({[sys.executable, '-c', nested]!r}, label='cancelled')\n")
    process = subprocess.Popen([sys.executable, "-c", outer], cwd=artifacts.REPO_ROOT,  # noqa: S603 -- owned fixture.
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.is_file()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) != 0
        records = [json.loads(path.read_text()) for path in (tmp_path / "logs").glob("*.json")]
        assert records and records[-1]["status"] == "interrupted"
        nested_records = [json.loads(path.read_text()) for path in (tmp_path / "nested").glob("*.json")]
        assert nested_records and nested_records[-1]["status"] == "interrupted"
    finally:
        stop_process(process)


def test_expected_refusal_is_a_completed_command(tmp_path):
    runner = CommandRunner(tmp_path)
    result = runner.run([sys.executable, "-c", "raise SystemExit(3)"], label="refusal", expected=(3,))
    assert result.returncode == 3 and runner.records[0]["status"] == "passed"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_unbounded_timeouts_are_rejected(value):
    with pytest.raises(ValueError):
        positive_seconds(value)


@pytest.mark.skipif(os.name != "posix", reason="The research qualification harness currently selects POSIX hosts")
def test_parallel_cases_overlap_keep_order_and_do_not_share_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(journeys, "candidate_identity", lambda: {"code": "unchanged"})
    monkeypatch.setenv("CASE_OBSERVATION", "parent")
    suite = {"grading_basis": "synthetic", "cases": [{"id": "slow"}, {"id": "fast"}]}
    def worker(output, reports, case, trial, protected, runner, timeout):
        delay = 0.5 if case["id"] == "slow" else 0.1
        result = runner.run([sys.executable, "-c", f"import os,time; time.sleep({delay}); print(os.environ['CASE_OBSERVATION'])"],
            label=case["id"], env=dict(os.environ, CASE_OBSERVATION=case["id"]), timeout=timeout)
        assert result.stdout.strip() == case["id"]
        return {"case_id": case["id"], "outcome": "passed", "commands": []}
    monkeypatch.setattr(journeys, "trial_worker", worker)
    report = journeys.qualify(tmp_path / "execution", suite, workers=2, report_dir=tmp_path / "reports")
    assert report["status"] == "passed" and os.environ["CASE_OBSERVATION"] == "parent"
    assert [row["case_id"] for row in report["trials"]] == ["slow", "fast"]
    events = [json.loads(line) for line in (tmp_path / "reports/commands/progress.jsonl").read_text().splitlines()]
    started = [datetime.fromisoformat(row["time"]) for row in events if row["event"] == "started"]
    finished = [datetime.fromisoformat(row["time"]) for row in events if row["event"] == "passed"]
    assert max(started) < min(finished)
    assert len(report["planned_trials"]) == len(report["trials"]) == 2


@pytest.mark.skipif(os.name != "posix", reason="The research qualification harness currently selects POSIX hosts")
def test_failed_case_does_not_hide_other_case_results(tmp_path, monkeypatch):
    monkeypatch.setattr(journeys, "candidate_identity", lambda: {"code": "unchanged"})
    suite = {"grading_basis": "synthetic", "cases": [{"id": "failed"}, {"id": "passed"}]}
    monkeypatch.setattr(journeys, "trial_worker", lambda output, reports, case, *args:
                        {"case_id": case["id"], "outcome": case["id"], "commands": []})
    report = journeys.qualify(tmp_path / "execution", suite, workers=2)
    assert report["status"] == "failed" and len(report["trials"]) == 2
    assert json.loads((tmp_path / "execution/observations.json").read_text())["status"] == "failed"


def html_observations():
    """Synthetic report-shape inputs; actual installed execution belongs to the artifact gate."""
    def cases(names):
        return [{"case": name, "evidence_accepted": False, "semantic_adequacy": "not_evaluated"} for name in names]
    return {"fresh": {"html_cli_journeys": "passed", "cases": cases(artifacts.HTML_CASE_NAMES),
                      "cached_refresh": "passed", "selected_replay": "passed"},
            "upgrade": {"html_upgrade": "passed", "cases": cases(("gateway-body", "signin-body", "numeric-data")),
                        "future_profile_refusal": "passed", "originals_preserved": True}}


def installation_reports(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = make_wheel(dist / "evidence_wiki-1.0.0-py3-none-any.whl", sorted(set(artifacts.REQUIRED_WHEEL_MEMBERS)))
    sdist = make_sdist(dist / "evidence_wiki-1.0.0.tar.gz", sorted(set(artifacts.REQUIRED_SDIST_MEMBERS)))
    cases = verification.load_cases(artifacts.REPO_ROOT / "tests/fixtures/onboarding-journeys/cases.json")["cases"]
    cases += verification.reference_cases(artifacts.REPO_ROOT / "tests/fixtures/strict-evidence/review-cases.json")[0]
    trials = []
    for case in cases:
        outcomes = {"q" + str(index): value for index, value in enumerate(case["release_expected"], 1)}
        trials.append({"case_id": case["id"], "trial": 1, "expected_outcomes": outcomes, "actual_outcomes": outcomes,
                       "outcome": "blocked_scope" if case["id"] == "unsupported-domain" else "passed"})
    reports = {}
    for kind in ("wheel", "sdist"):
        report = {"artifact": kind, "status": "partial", "selection_status": "passed",
            "workflow": {"GITHUB_SHA": "commit", "GITHUB_RUN_ID": "run"}, "validation_inputs": artifacts.validation_identity(),
            "wheel": {"name": wheel.name, "sha256": artifacts.sha256_of(wheel)},
            "sdist": {"name": sdist.name, "sha256": artifacts.sha256_of(sdist)},
            "commands": [{"stage": "probe", "status": "passed"}],
            "checks": {"membership": artifacts.check_archive_membership(wheel, sdist), "installed_" + kind: {
                "label": kind, "checkout_imports": "disabled", "version": verification.__version__,
                "html_usability": html_observations(),
                **{group: dict.fromkeys(cases, "passed") for group, cases in installed_cli.OUTCOME_CASES.items()},
                "native_initialization": {"direct_profile": "passed", "nested_profile": "passed",
                    "no_write_refusals": "passed", "controller_submission": "passed"},
                "planned_delegation": {"schema_discovery": "passed", "plan_check": "passed", "apply_replay": "passed",
                    "strict_bindings": "passed", "controller_submission": "passed"}, "journeys": {
                    "status": "passed", "candidate_unchanged": True, "package_version": verification.__version__,
                    "planned_trials": [{"case_id": row["case_id"], "trial": 1} for row in trials], "trials": copy.deepcopy(trials)}}}}
        reports[kind] = report
    return dist, reports


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("defect", ["missing", "failed", "missing_case", "duplicate_case", "acceptance", "semantic",
                                   "no_refresh", "no_replay", "no_future_refusal", "originals_changed"])
def test_final_gate_requires_complete_html_qualification(tmp_path, kind, defect):
    dist, reports = installation_reports(tmp_path)
    installed = reports[kind]["checks"]["installed_" + kind]
    html = installed["html_usability"]
    if defect == "missing":
        del installed["html_usability"]
    elif defect == "failed":
        html["upgrade"]["html_upgrade"] = "failed"
    elif defect == "missing_case":
        html["fresh"]["cases"].pop()
    elif defect == "duplicate_case":
        html["fresh"]["cases"][-1] = dict(html["fresh"]["cases"][0])
    elif defect == "acceptance":
        html["fresh"]["cases"][0]["evidence_accepted"] = True
    elif defect == "semantic":
        html["fresh"]["cases"][0]["semantic_adequacy"] = "approved"
    elif defect == "no_refresh":
        html["fresh"]["cached_refresh"] = "not_run"
    elif defect == "no_replay":
        html["fresh"]["selected_replay"] = "not_run"
    elif defect == "no_future_refusal":
        html["upgrade"]["future_profile_refusal"] = "not_run"
    else:
        html["upgrade"]["originals_preserved"] = False
    evidence = tmp_path / "reports"
    for label, report in reports.items():
        directory = evidence / label
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="HTML"):
        verification.verify(dist, evidence, commit="commit", run_id="run")


def test_html_qualification_inputs_are_bound_and_copied(tmp_path):
    required = {"tools/probe_html_usability.py", "tests/fixtures/html-usability/pages.json",
                "tests/fixtures/html-usability/legacy-records.json"}
    assert required <= set(artifacts.REQUIRED_SDIST_MEMBERS)
    assert required <= set(artifacts.fixture_members())
    copied = artifacts.isolated_fixtures(tmp_path)
    hashes = json.loads((copied / "inputs.json").read_text())
    for name in required:
        assert hashes[name] == artifacts.validation_identity()[name]
        assert (copied / name).read_bytes() == (artifacts.REPO_ROOT / name).read_bytes()


def test_installed_html_checks_run_both_journeys_through_the_selected_environment(tmp_path, monkeypatch):
    calls = []
    observed = html_observations()
    def run(argv, **options):
        calls.append((argv, options))
        return json.dumps(observed["upgrade" if "--legacy-fixture" in argv else "fresh"])
    monkeypatch.setattr(artifacts, "run", run)
    python, cli = tmp_path / "python", tmp_path / "cli"
    result = artifacts.validate_html_installation(python, cli, scratch=tmp_path / "execution",
                outside=tmp_path / "outside", fixture_root=tmp_path / "inputs")
    assert result == observed and len(calls) == 2
    for argv, options in calls:
        assert argv[:2] == [str(python), "-I"] and argv[argv.index("--cli") + 1] == str(cli)
        assert options["cwd"] == tmp_path / "outside"
    assert calls[0][1]["label"] == "html-fresh" and calls[1][1]["label"] == "html-upgrade"


@pytest.mark.parametrize("label", ["wheel", "sdist"])
def test_both_installations_enter_the_html_qualification_owner(tmp_path, monkeypatch, label):
    environment, scratch = tmp_path / "environment", tmp_path / "execution"
    cli = artifacts.venv_cli(environment)
    cli.parent.mkdir(parents=True)
    cli.touch()
    scratch.mkdir()
    fixtures = scratch / "qualification-inputs"
    monkeypatch.setattr(artifacts, "isolated_fixtures", lambda root: fixtures)
    monkeypatch.setattr(artifacts, "validate_cli_entrypoints", lambda *args, **kwargs: {})
    monkeypatch.setattr(artifacts, "run", lambda *args, **kwargs: pytest.fail("HTML owner was skipped"))
    def probe(python, selected_cli, **options):
        assert python == artifacts.venv_python(environment) and selected_cli == cli
        assert options == {"scratch": scratch, "outside": scratch / "outside-checkout", "fixture_root": fixtures}
        raise RuntimeError("HTML qualification reached")
    monkeypatch.setattr(artifacts, "validate_html_installation", probe)
    with pytest.raises(RuntimeError, match="HTML qualification reached"):
        artifacts.validate_installed(environment, scratch, "9.9.9", label)


@pytest.mark.parametrize("defect", [None, "missing", "duplicate", "commit", "run", "inputs", "bytes", "unfinished",
                                    "missing-case", "duplicate-case", "wrong-outcome", "failed-command", "candidate-changed",
                                    "native-missing", "native-incomplete", "planned-missing", "planned-incomplete"])
def test_final_gate_requires_exact_bytes_and_every_scenario(tmp_path, defect):
    dist, reports = installation_reports(tmp_path)
    report = reports["sdist"]
    journeys_report = report["checks"]["installed_sdist"]["journeys"]
    if defect == "missing":
        reports.pop("sdist")
    elif defect == "native-missing":
        del report["checks"]["installed_sdist"]["native_initialization"]
    elif defect == "native-incomplete":
        report["checks"]["installed_sdist"]["native_initialization"]["controller_submission"] = "not_run"
    elif defect == "planned-missing":
        del report["checks"]["installed_sdist"]["planned_delegation"]
    elif defect == "planned-incomplete":
        report["checks"]["installed_sdist"]["planned_delegation"]["strict_bindings"] = "not_run"
    elif defect == "duplicate":
        reports["duplicate"] = report
    elif defect == "commit":
        report["workflow"]["GITHUB_SHA"] = "another"
    elif defect == "run":
        report["workflow"]["GITHUB_RUN_ID"] = "another"
    elif defect == "inputs":
        report["validation_inputs"].clear()
    elif defect == "bytes":
        report["wheel"]["sha256"] = "different"
    elif defect == "unfinished":
        report["selection_status"] = "running"
    elif defect == "missing-case":
        journeys_report["trials"].pop()
    elif defect == "duplicate-case":
        journeys_report["trials"].append(journeys_report["trials"][0])
    elif defect == "wrong-outcome":
        journeys_report["trials"][0]["actual_outcomes"] = {"q1": False}
    elif defect == "failed-command":
        report["commands"][0]["status"] = "timed_out"
    elif defect == "candidate-changed":
        journeys_report["candidate_unchanged"] = False
    evidence = tmp_path / "reports"
    for kind, row in reports.items():
        directory = evidence / kind
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(json.dumps(row))
    if defect:
        with pytest.raises(ValueError):
            verification.verify(dist, evidence, commit="commit", run_id="run")
    else:
        assert verification.verify(dist, evidence, commit="commit", run_id="run")["status"] == "passed"


@pytest.mark.parametrize("label", ["wheel", "sdist"])
@pytest.mark.parametrize("group", installed_cli.OUTCOME_CASES)
@pytest.mark.parametrize("defect", ["missing", "null", "list", "text", "extra"])
def test_final_gate_refuses_absent_or_malformed_installed_groups(tmp_path, label, group, defect):
    """Historical or malformed reports cannot imply unobserved installed behavior."""
    dist, reports = installation_reports(tmp_path)
    installed = reports[label]["checks"]["installed_" + label]
    if defect == "missing":
        del installed[group]
    elif defect == "extra":
        installed[group]["unobserved"] = "passed"
    else:
        installed[group] = {"null": None, "list": list(installed[group]), "text": "passed"}[defect]
    evidence = tmp_path / "reports"
    for kind, report in reports.items():
        directory = evidence / kind
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match=group):
        verification.verify(dist, evidence, commit="commit", run_id="run")


@pytest.mark.parametrize("label", ["wheel", "sdist"])
@pytest.mark.parametrize("group,case", [(group, case) for group, cases in installed_cli.OUTCOME_CASES.items() for case in cases])
@pytest.mark.parametrize("status", [None, "failed", "not_run", "unknown", True])
def test_final_gate_requires_each_installed_case_to_pass(tmp_path, label, group, case, status):
    """Each required case independently blocks verification in either artifact report."""
    dist, reports = installation_reports(tmp_path)
    outcomes = reports[label]["checks"]["installed_" + label][group]
    if status is None:
        del outcomes[case]
    else:
        outcomes[case] = status
    evidence = tmp_path / "reports"
    for kind, report in reports.items():
        directory = evidence / kind
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match=group):
        verification.verify(dist, evidence, commit="commit", run_id="run")


def test_single_artifact_and_timeout_options_are_explicit():
    args = artifacts.parse_args(["--artifact", "sdist", "--journey-workers", "3", "--command-timeout", "50"])
    assert args.artifact == "sdist" and args.journey_workers == 3 and args.command_timeout == 50
    with pytest.raises(SystemExit):
        artifacts.parse_args(["--artifact", "sdist", "--skip-sdist"])


@pytest.mark.parametrize("defect", [None, "missing-version", "different-report-version", "different-requested-version"])
def test_release_reports_promote_only_the_explicitly_qualified_version(tmp_path, monkeypatch, defect):
    dist, reports = installation_reports(tmp_path)
    version = verification.__version__
    for report in reports.values():
        report["expected_version"] = version
    if defect == "missing-version":
        reports["sdist"].pop("expected_version")
    elif defect == "different-report-version":
        reports["sdist"]["expected_version"] = "9.9.9"
    evidence = tmp_path / "reports"
    for kind, report in reports.items():
        directory = evidence / kind
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(json.dumps(report))
    requested = "9.9.9" if defect == "different-requested-version" else version
    if defect:
        with pytest.raises(ValueError, match="version"):
            verification.verify(dist, evidence, commit="commit", run_id="run", expected_version=requested)
        return
    result = verification.verify(dist, evidence, commit="commit", run_id="run", expected_version=requested)
    candidate = tmp_path / "release-candidate"
    shutil.copytree(dist, candidate / "dist")
    (candidate / "artifact-validation.json").write_text(json.dumps(result))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion="{version}"\n')
    monkeypatch.chdir(tmp_path)
    run_inline_python(inline_python("release-gate", "Verify the exact distribution bytes before promotion"))


def test_failed_retention_restores_validator_runtime(tmp_path, monkeypatch):
    before = artifacts._RUNNER, artifacts._OPTIONS
    monkeypatch.setattr(artifacts, "validate_distributions", lambda args, summary, scratch: None)
    def failed_retention(scratch, evidence):
        raise OSError("retention unavailable")
    monkeypatch.setattr(artifacts, "retain_evidence", failed_retention)
    with pytest.raises(OSError, match="retention unavailable"):
        artifacts.main(["--evidence-dir", str(tmp_path / "reports")])
    assert (artifacts._RUNNER, artifacts._OPTIONS) == before
    assert json.loads((tmp_path / "reports/summary.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("defect", ["escape", "link", "special"])
def test_source_archive_refuses_unsafe_members_before_extraction(tmp_path, defect):
    source = tmp_path / "source.tar.gz"
    member = tarfile.TarInfo("../escape" if defect == "escape" else "project/member")
    if defect == "link":
        member.type, member.linkname = tarfile.SYMTYPE, "../../outside"
    elif defect == "special":
        member.type = tarfile.FIFOTYPE
    with tarfile.open(source, "w:gz") as archive:
        archive.addfile(member)
    with pytest.raises(artifacts.ValidationError):
        artifacts.build_wheel_from_sdist(source, tmp_path)
    assert not (tmp_path.parent / "escape").exists()
