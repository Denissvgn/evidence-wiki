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
from datetime import datetime

import pytest

from tests.test_release_workflow import inline_python, make_sdist, make_wheel, run_inline_python
from tools import qualify_journeys as journeys
from tools import validate_installed_artifacts as artifacts
from tools import verify_installed_artifacts as verification
from tools._qualification_process import CommandRunner, positive_seconds, stop_process


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
                "label": kind, "checkout_imports": "disabled", "version": verification.__version__, "journeys": {
                    "status": "passed", "candidate_unchanged": True, "package_version": verification.__version__,
                    "planned_trials": [{"case_id": row["case_id"], "trial": 1} for row in trials], "trials": copy.deepcopy(trials)}}}}
        reports[kind] = report
    return dist, reports


@pytest.mark.parametrize("defect", [None, "missing", "duplicate", "commit", "run", "inputs", "bytes", "unfinished",
                                    "missing-case", "duplicate-case", "wrong-outcome", "failed-command", "candidate-changed"])
def test_final_gate_requires_exact_bytes_and_every_scenario(tmp_path, defect):
    dist, reports = installation_reports(tmp_path)
    report = reports["sdist"]
    journeys_report = report["checks"]["installed_sdist"]["journeys"]
    if defect == "missing":
        reports.pop("sdist")
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
