#!/usr/bin/env python3
"""Observe a fixed pair of native review jobs with fresh diagnostic custody."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_wiki.research_journal_contracts import digest_record
from tools import run_review_dependency_workload as workload
from tools._review_dependency_workload import (
    OriginalResponses,
    OwnedChildren,
    authenticated_journal,
    digest,
    failure_document,
    file_manifest,
    provision,
    retain,
    unstarted_job_timing,
    workload_deadline,
)
from tools.decide_release import _HostDeadline, _read

PLAN_SCHEMA = "review-cost-diagnostic-plan/v1"
RESULT_SCHEMA = "review-cost-diagnostic-observation/v1"
PHASE_CAPS = {"setup": 180, "review_group": 1200, "cleanup": 60, "retention_source_check": 120}
JOB_CAP_SECONDS = 600
CASE_ID = "independent-claim-text"
CLAIM_ID = "observed-capacity"


def reference_plan(bundle, *, profile_enabled=False):
    """Freeze the full fixture and exact two-role selection before execution."""
    if type(profile_enabled) is not bool:
        raise ValueError("workload_selection_invalid")
    native = workload.reference_plan(bundle)
    rows = [copy.deepcopy(row) for row in native["assignments"]
        if row["cycle"] == "baseline" and row["claim_id"] == CLAIM_ID]
    if len(rows) != 2 or len(workload.assignment_groups(rows)) != 1:
        raise ValueError("workload_assignment_invalid")
    return {"schema_version": PLAN_SCHEMA, "candidate_sha256": native["candidate_sha256"],
        "artifacts": native["artifacts"], "host": native["host"], "hard_limits": native["hard_limits"],
        "scope": native["scope"], "frozen_at": native["frozen_at"], "expires_at": native["expires_at"],
        "case_id": CASE_ID, "claim_id": CLAIM_ID, "cycle": "baseline", "mutations_applied": False, "assignments": rows,
        "phase_caps": dict(PHASE_CAPS), "job_cap_seconds": JOB_CAP_SECONDS,
        "elapsed_cap_seconds": sum(PHASE_CAPS.values()), "profile_enabled": profile_enabled,
        "reference_checks": 200, "review_jobs": 2, "owner_read_rounds": 0,
        "prior_authority_reuse": False, "qualification": False, "stdin": "closed"}


def validate_plan(plan, expected_plan):
    if type(plan) is not dict or plan.get("schema_version") != PLAN_SCHEMA or digest(plan) != expected_plan:
        raise ValueError("workload_plan_binding_invalid")
    expected = reference_plan(Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent,
        profile_enabled=plan["profile_enabled"])
    if set(plan) != set(expected) or any(plan[key] != expected[key] for key in expected if key not in {"frozen_at", "expires_at"}):
        raise ValueError("workload_candidate_or_recipe_changed")
    frozen, expires = (datetime.fromisoformat(plan[key]) for key in ("frozen_at", "expires_at"))
    if (frozen.tzinfo is None or expires.tzinfo is None or expires - frozen != timedelta(seconds=7200)
            or not frozen <= datetime.now(timezone.utc) < expires):
        raise ValueError("workload_plan_binding_invalid")
    return plan


def preflight(plan, expected_plan):
    validate_plan(plan, expected_plan)
    return {"status": "prepared_diagnostic_unqualified", "plan_sha256": expected_plan,
        "review_jobs": 2, "reference_checks": 200, "allocation_seconds": 1560,
        "profile_enabled": plan["profile_enabled"], "qualification": False}


def bind_observed_jobs(report, records, receipt):
    """Bind only authenticated application records and retained native timings."""
    observed = [row for row in records if row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"]
    expected = {(row["claim_id"], row["config_ref"]): row for row in report["assignments"]}
    if any((row["data"].get("claim_id"), row["data"].get("profile_ref")) not in expected for row in observed):
        raise ValueError("workload_native_review_application_missing")
    report["applied_jobs"] = 0
    for key, assignment in expected.items():
        matches = [row for row in observed if (row["data"].get("claim_id"), row["data"].get("profile_ref")) == key]
        if len(matches) > 1:
            raise ValueError("workload_native_review_application_missing")
        if not matches:
            continue
        applied = matches[0]
        assignment.update(attempt_id=applied["data"]["attempt_id"], application_event_ref=applied["event_ref"],
            native_record_sha256=digest_record(applied), journal_head_pin=receipt["head_ref"],
            journal_source_pin=receipt["source_sha256"], native_passed=applied["data"].get("passed"))
        report["applied_jobs"] += 1
        timings = [row for row in report["job_elapsed"] if (row["native_run_id"], row["claim_id"], row["config_ref"])
            == (assignment["native_run_id"], assignment["claim_id"], assignment["config_ref"])]
        if (len(timings) != 1 or timings[0]["status"] != "applied" or timings[0]["original_timing_pin"] is None
                or type(timings[0]["elapsed_seconds"]) not in (int, float)
                or not math.isfinite(timings[0]["elapsed_seconds"]) or not 0 < timings[0]["elapsed_seconds"] <= JOB_CAP_SECONDS
                or any(timings[0][field] != assignment[field] for field in
                    ("attempt_id", "application_event_ref", "native_record_sha256", "native_passed"))):
            raise ValueError("workload_job_timing_binding_invalid")
    report["observed_work"] = "two_applied" if report["applied_jobs"] == 2 else "partial"


def run_diagnostic(plan, expected_plan, directory):
    validate_plan(plan, expected_plan)
    directory = Path(directory).absolute()
    if directory.resolve() != directory or directory.exists() or directory.is_relative_to(ROOT):
        raise ValueError("workload_new_private_external_directory_required")
    directory.mkdir(mode=0o700)
    started = time.monotonic()
    rows = copy.deepcopy(plan["assignments"])
    run_id = rows[0]["native_run_id"]
    report = {"schema_version": RESULT_SCHEMA, "plan_sha256": expected_plan,
        "candidate_sha256": plan["candidate_sha256"], "study": "role_cost_diagnostic",
        "terminal_outcome": "started_stop_unknown", "failure": None, "failures": [],
        "qualification": False, "pair_qualified": False, "pilot_completed": False,
        "automatic_canonical_completion": False, "acceptance_credits": 0, "owner_read_rounds": 0,
        "host_id": "direct", "case_id": CASE_ID, "claim_id": CLAIM_ID, "cycle": "baseline", "mutations_applied": False,
        "assignments": rows, "review_jobs": 2, "applied_jobs": None, "observed_work": "unobserved",
        "reference_checks": None, "phase_elapsed": [],
        "job_elapsed": [unstarted_job_timing(row, JOB_CAP_SECONDS) for row in rows],
        "instrumentation": {"enabled": plan["profile_enabled"], "status": "unobserved" if plan["profile_enabled"] else "disabled",
            "diagnostic_only": True, "qualification": False, "profile": None},
        "journal_receipts": [], "input_pins": [], "candidate_checks": [], "original_response_pins": [],
        "native_journal_pins": [], "cleanup": None, "child_cleanup": None,
        "retention": {"status": "unobserved"}, "elapsed_seconds": None}
    originals = OriginalResponses(directory / "observations")
    tracker = OwnedChildren(originals, expected_plan)
    root = runtime = None
    opened = False

    def phase(label, operation):
        tick = time.monotonic()
        status = "failed"
        try:
            with workload_deadline(PHASE_CAPS[label]):
                result = operation()
                if time.monotonic() - tick > PHASE_CAPS[label]:
                    raise _HostDeadline()
                status = "completed"
                return result
        finally:
            report["phase_elapsed"].append({"phase": label, "status": status,
                "elapsed_seconds": time.monotonic() - tick, "cap_seconds": PHASE_CAPS[label]})

    def fail(error):
        failure = failure_document(error)
        report["failures"].append(failure)
        originals.append(("failure-" + str(len(report["failures"])), failure))
        if report["failure"] is None:
            report["failure"] = failure
        report["terminal_outcome"] = "diagnostic_failed_unqualified"

    def check_candidate(label):
        validate_plan(plan, expected_plan)
        report["candidate_checks"].append(originals.append((label + "-candidate-check", {
            "candidate_sha256": plan["candidate_sha256"], "plan_sha256": expected_plan,
            "observed_utc": datetime.now(timezone.utc).isoformat()})))

    def observe():
        records, receipt, _, _ = authenticated_journal(root, runtime, run_id, originals)
        report["journal_receipts"].append(receipt)
        bind_observed_jobs(report, records, receipt)

    try:
        tracker.open()
        remaining = (datetime.fromisoformat(plan["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
        with workload_deadline(min(PHASE_CAPS["setup"] + PHASE_CAPS["review_group"], remaining)):
            def setup():
                nonlocal root, runtime
                check_candidate("before")
                artifacts = {}
                for name, pin in plan["artifacts"].items():
                    value, raw = workload.read_pinned_artifact(pin)
                    artifacts[name] = value
                    report["input_pins"].append(retain(directory / "inputs" / name, raw, raw=True))
                root, runtime, _, _, _ = provision(directory / "fixture", artifacts["fresh-fixture-input.json"]["fixture"],
                    originals, run_seconds=1560, allocation_binding={"plan_sha256": expected_plan,
                        "candidate_sha256": plan["candidate_sha256"], "study": report["study"],
                        "assignments": rows, "phase_caps": plan["phase_caps"], "profile_enabled": plan["profile_enabled"]})
                report["reference_checks"] = 200
                retain(directory / "realized-input-manifest.json", file_manifest(root))
            phase("setup", setup)

            def review_group():
                nonlocal opened
                opened = True
                profile = None
                entered = False
                drive_error = None
                if plan["profile_enabled"]:
                    from tools._native_cost_profile import NativeCostProfile
                    profile = NativeCostProfile()
                try:
                    with profile if profile is not None else nullcontext():
                        entered = True
                        workload.drive(root, runtime, "direct", run_id, originals, case_id=CASE_ID,
                            claim_ids={CLAIM_ID}, job_cap_seconds=JOB_CAP_SECONDS, job_elapsed=report["job_elapsed"])
                except (Exception, SystemExit, _HostDeadline) as error:
                    drive_error = error
                    raise
                finally:
                    if profile is not None and entered:
                        try:
                            profile_report = profile.report()
                            report["instrumentation"].update(status="observed", profile=profile_report)
                            originals.append(("native-cost-profile", profile_report))
                        except (Exception, SystemExit, _HostDeadline) as error:
                            report["instrumentation"]["status"] = "unavailable"
                            if drive_error is not None:
                                fail(drive_error)
                            fail(error)
                    elif profile is not None:
                        report["instrumentation"]["status"] = "unavailable"
                observe()
            phase("review_group", review_group)
    except (Exception, SystemExit, _HostDeadline) as error:
        fail(error)
    finally:
        def cleanup():
            errors, cleaned = [], []
            try:
                report["child_cleanup"] = tracker.close()
            except (Exception, SystemExit, _HostDeadline) as error:
                report["child_cleanup"] = {"status": "unavailable", "failure": failure_document(error)}
                errors.append(error)
            if runtime is not None:
                for actual in runtime.execution.executor._attempts.values():
                    try:
                        attempt = actual.attempt
                        verified = runtime.execution.executor.authenticate(runtime.execution.executor.cancel(attempt), attempt)
                        originals.append((attempt.attempt_id + "-cleanup", verified))
                        if verified["state"] == "running" or verified["final_accounting"] is not True:
                            raise ValueError("workload_cleanup_unavailable")
                        cleaned.append(attempt.attempt_id)
                    except (Exception, SystemExit, _HostDeadline) as error:
                        originals.append(("cleanup-failure-" + str(len(errors)), failure_document(error)))
                        errors.append(error)
            if errors:
                raise ValueError("workload_cleanup_unavailable")
            return {"status": "confirmed", "attempt_ids": cleaned}
        try:
            report["cleanup"] = phase("cleanup", cleanup)
        except (Exception, SystemExit, _HostDeadline) as error:
            report["cleanup"] = {"status": "unavailable", "failure": failure_document(error)}
            fail(error)

    def finish():
        nonlocal root
        try:
            if runtime is not None and opened:
                try:
                    observe()
                except (Exception, SystemExit, _HostDeadline) as error:
                    fail(error)
            if root is None and (directory / "fixture/workspace").is_dir():
                root = directory / "fixture/workspace"
            if root is not None:
                report["native_journal_pins"], report["retention"] = workload.collect_journals(root, directory)
                if report["retention"]["status"] != "complete":
                    raise ValueError("workload_journal_collection_bound")
        finally:
            check_candidate("after")
    try:
        phase("retention_source_check", finish)
    except (Exception, SystemExit, _HostDeadline) as error:
        fail(error)
        if root is not None and report["retention"]["status"] == "unobserved":
            report["native_journal_pins"], report["retention"] = workload.collect_journals(root, directory)
            try:
                check_candidate("after-retention")
            except (Exception, SystemExit, _HostDeadline) as source_error:
                fail(source_error)
    if report["failure"] is None:
        if report["applied_jobs"] == 2 and all(row.get("native_passed") is True for row in rows):
            report["terminal_outcome"] = "diagnostic_completed_unqualified"
        else:
            fail(ValueError("workload_native_review_application_missing"))
    report["elapsed_seconds"] = time.monotonic() - started
    if report["elapsed_seconds"] > 1560:
        fail(_HostDeadline())
    report["original_response_pins"] = originals.pins
    retain(directory / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "preflight", "run"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "freeze":
            if args.bundle is None or args.output is None:
                raise ValueError("workload_freeze_arguments_required")
            path = args.output.absolute()
            if path.resolve() != path or path.is_relative_to(ROOT) or path.exists():
                raise ValueError("workload_new_private_external_directory_required")
            plan = reference_plan(args.bundle, profile_enabled=args.profile)
            retain(path, plan)
            result = {"plan_path": str(path), "plan_sha256": digest(plan), "runtime_qualification": "NOT RUN"}
        else:
            if args.plan is None or args.expected_plan is None or args.profile:
                raise ValueError("workload_selection_invalid")
            plan = _read(args.plan)
            if args.action == "preflight":
                result = preflight(plan, args.expected_plan)
            else:
                if args.directory is None:
                    raise ValueError("workload_run_arguments_required")
                report = run_diagnostic(plan, args.expected_plan, args.directory)
                result = {"report_path": str(args.directory / "report.json"), "report_sha256": digest(report),
                    "terminal_outcome": report["terminal_outcome"], "qualification": False}
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if args.action != "run" or report["terminal_outcome"] == "diagnostic_completed_unqualified" else 2
    except (Exception, SystemExit, _HostDeadline) as error:
        print(json.dumps({"status": "refused", "failure": failure_document(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
