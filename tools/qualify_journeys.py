#!/usr/bin/env python3
"""Run frozen synthetic research journeys through installed public command owners."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# The capsule contains only these explicit harness/fixture inputs, never src/.
INPUT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INPUT_ROOT))
if "tests" not in sys.modules:
    fixture_package = types.ModuleType("tests")
    fixture_package.__path__ = [str(INPUT_ROOT / "tests")]
    sys.modules["tests"] = fixture_package

from tools._journey_cases import candidate_identity, canonical, digest, load_cases, reference_cases  # noqa: E402
from tools._journey_driver import Journey  # noqa: E402
from tools._qualification_process import CommandRunner, interruptible, positive_seconds  # noqa: E402


def execute_trial(root, case, *, protected_host=False, report_dir=None):
    journey = Journey(root, case, report_dir=report_dir)
    try:
        return journey.run(protected_host=protected_host)
    except Exception as error:
        return {"case_id": case["id"], "outcome": "failed", "reason": str(error)[:4096], "commands": journey.commands}


def trial_worker(output, reports, case, trial, protected_host, runner, timeout):
    """Each case owns a fresh process; host environment changes cannot cross cases."""
    label = case["id"] + "-" + str(trial)
    directory = reports / "workers" / label
    directory.mkdir(parents=True)
    request = directory / "request.json"
    result = directory / "result.json"
    request.write_bytes(canonical({"root": str(output / label), "case": case,
        "protected_host": protected_host, "report_dir": str(directory / "commands"), "result": str(result)}))
    try:
        completed = runner.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker-request", str(request)],
            label=label, cwd=output, timeout=timeout, stream_stderr=True)
        if completed.returncode != 0:
            raise ValueError(f"journey worker exited {completed.returncode}; inspect workers/{label}")
        value = json.loads(result.read_text(encoding="utf-8"))
        if value.get("case_id") != case["id"] or value.get("outcome") not in {"passed", "failed", "blocked_scope"}:
            raise ValueError("journey worker returned a different case or invalid outcome")
        return value
    except Exception as error:
        return {"case_id": case["id"], "outcome": "failed", "reason": str(error)[:4096], "commands": [],
                "diagnostics": "workers/" + label}


def qualify(output, suite, selected=None, trials=1, *, protected_host=False, workers=1,
            case_timeout=1200, report_dir=None):
    import evidence_wiki

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    reports = Path(report_dir).resolve() if report_dir is not None else output
    reports.mkdir(parents=True, exist_ok=True)
    runner = CommandRunner(reports / "commands")
    identity = candidate_identity()
    frozen = {"cases": suite, "selected": selected, "trials": trials, "protected_host": protected_host, "candidate": identity,
              "package_version": evidence_wiki.__version__, "started_at": datetime.now(timezone.utc).isoformat()}
    (reports / "frozen.json").write_bytes(canonical(frozen))
    report = {"schema_version": "evidence-journey-observation/v1", "frozen_sha256": digest(frozen),
        "package_version": evidence_wiki.__version__, "package_location": str(Path(evidence_wiki.__file__).resolve()),
        "platform": platform.platform(), "python": platform.python_version(), "trials": [], "status": "incomplete",
        "grading_basis": suite["grading_basis"], "live_model": False, "candidate": identity,
        "semantic_metrics": None, "operator_interventions": None, "workers": workers,
        "limits": ["Synthetic reference conformance does not certify arbitrary semantic truth.",
                   "Reference evaluator uses disclosed fixture credentials, not production or expert approval.",
                   "Ordinary command execution is artifact_checked; host protection is reported separately."]}
    def save():
        path = reports / "observations.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(canonical(report))
        temporary.replace(path)
    save()
    if os.name != "posix":
        report.update(status="unsupported", reason="Setup and authenticated host storage require POSIX primitives")
        save()
        return report
    planned = [(case, trial) for case in suite["cases"] if not selected or case["id"] in selected
               for trial in range(1, trials + 1)]
    order = {(case["id"], trial): index for index, (case, trial) in enumerate(planned)}
    report["planned_trials"] = [{"case_id": case["id"], "trial": trial} for case, trial in planned]
    save()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(trial_worker, output, reports, case, trial, protected_host, runner, case_timeout):
                   (case, trial) for case, trial in planned}
        for future in as_completed(futures):
            case, trial = futures[future]
            result = future.result()
            path = reports / (case["id"] + "-" + str(trial) + ".json")
            path.write_bytes(canonical(result))
            report["trials"].append({"case_id": case["id"], "trial": trial, "outcome": result["outcome"],
                "evidence": path.name, "sha256": digest(result), "reason": result.get("reason"),
                "expected_outcomes": result.get("expected"), "actual_outcomes": result.get("actual"),
                "first_usable_evidence_seconds": result.get("first_usable_evidence_seconds"),
                "host": result.get("host", {"status": "not_run"})})
            report["trials"].sort(key=lambda row: order[(row["case_id"], row["trial"])])
            save()
            runner.event("case_complete", case["id"], outcome=result["outcome"], completed=len(report["trials"]), total=len(planned))
    except BaseException:
        runner.cancelled.set()
        report["status"] = "interrupted"
        save()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    report["status"] = "passed" if len(report["trials"]) == len(planned) > 0 and all(r["outcome"] in {"passed", "blocked_scope"} for r in report["trials"]) else "failed"
    report["candidate_unchanged"] = candidate_identity() == identity
    if not report["candidate_unchanged"]:
        report.update(status="failed", reason="candidate_changed_during_qualification")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=INPUT_ROOT / "tests/fixtures/onboarding-journeys/cases.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report-dir", type=Path, help="Write live diagnostic files separately from execution workspaces.")
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 9))
    parser.add_argument("--case-timeout", type=positive_seconds, default=1200)
    parser.add_argument("--worker-request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case", action="append")
    parser.add_argument("--trials", type=int, default=1, choices=range(1, 11))
    parser.add_argument("--protected-host", action="store_true")
    args = parser.parse_args(argv)
    if args.worker_request:
        request = json.loads(args.worker_request.read_text(encoding="utf-8"))
        result = execute_trial(request["root"], request["case"], protected_host=request["protected_host"], report_dir=request["report_dir"])
        Path(request["result"]).write_bytes(canonical(result))
        return 0
    if args.output is None:
        parser.error("--output is required")
    suite = load_cases(args.cases)
    references, suite["strict_reference"] = reference_cases(INPUT_ROOT / "tests/fixtures/strict-evidence/review-cases.json")
    suite["cases"].extend(references)
    if args.case and not set(args.case) <= {row["id"] for row in suite["cases"]}:
        parser.error("unknown case selection")
    result = qualify(args.output, suite, args.case, args.trials, protected_host=args.protected_host,
                     workers=args.workers, case_timeout=args.case_timeout, report_dir=args.report_dir)
    return 0 if result["status"] == "passed" else 3


if __name__ == "__main__":
    with interruptible():
        raise SystemExit(main())
