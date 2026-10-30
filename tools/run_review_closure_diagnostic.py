#!/usr/bin/env python3
"""Measure native journal closing with fresh custody and no review dispatch."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_wiki import Workspace
from tools import run_review_dependency_workload as workload
from tools._execution_budget import (
    execution_policy as select_execution_policy,
)
from tools._execution_budget import (
    validate_execution_binding,
    validate_runtime_allocation,
)
from tools._observation_freshness import (
    is_batch_policy,
    validate_observation_policy,
    validate_observer_window,
)
from tools._observation_freshness import (
    observation_policy as select_observation_policy,
)
from tools._review_dependency_workload import (
    OriginalResponses,
    OwnedChildren,
    authenticated_journal,
    digest,
    file_manifest,
    provision,
    retain,
    workload_deadline,
)
from tools._review_dependency_workload import (
    failure_document as workload_failure_document,
)
from tools.decide_release import _HostDeadline, _read
from tools.replay_qualification_native import verify_journal_closing

PLAN_SCHEMA = "review-closure-diagnostic-plan/v1"
POLICY_PLAN_SCHEMA = "review-closure-diagnostic-plan/v2"
RESULT_SCHEMA = "review-closure-diagnostic-observation/v1"
POLICY_RESULT_SCHEMA = "review-closure-diagnostic-observation/v2"
EXECUTION_PLAN_SCHEMA = "review-closure-diagnostic-plan/v3"
EXECUTION_RESULT_SCHEMA = "review-closure-diagnostic-observation/v3"
PHASE_CAPS = {"setup": 180, "owner_read": 120, "cleanup": 60, "global_close": 120}
BATCH_PHASE_CAPS = {"setup": 180, "owner_read": 600, "cleanup": 60, "global_close": 600}
EXECUTION_PHASE_CAPS = dict(BATCH_PHASE_CAPS, setup=600)
DIAGNOSTIC_REASONS = {"closure_journal_count_invalid", "closure_allocation_invalid",
    "closure_plan_expired_or_clock_invalid", "closure_witness_clock_invalid",
    "closure_unexpected_execution_records", "closure_review_attempt_observed",
    "closure_freeze_arguments_required", "closure_run_arguments_required",
    "observation_policy_invalid", "observer_clock_invalid", "observer_policy_binding_invalid",
    "closure_owner_closing_changed", "closure_authority_clock_invalid"}


def failure_document(error):
    """Retain only bounded local reasons alongside the shared native diagnostic."""
    result = workload_failure_document(error)
    if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in DIAGNOSTIC_REASONS:
        result["reason"] = error.args[0]
    return result


def allocation_seconds(caps):
    return caps["setup"] + 2 * caps["owner_read"] + caps["cleanup"] + caps["global_close"]


def reference_plan(bundle, *, journal_count=4, phase_caps=None, observation_profile=None, execution_profile=None):
    """Bind the same full fixture and candidate before creating fresh services."""
    if type(journal_count) is not int or not 4 <= journal_count <= 13:
        raise ValueError("closure_journal_count_invalid")
    policy = None if observation_profile is None else select_observation_policy(observation_profile)
    execution = None if execution_profile is None else select_execution_policy(execution_profile)
    if execution is not None and (policy is None or not is_batch_policy(policy)):
        raise ValueError("execution_observation_profile_invalid")
    authority_seconds = 7200 if execution is None else execution["authority_seconds"]
    limits = EXECUTION_PHASE_CAPS if execution is not None else BATCH_PHASE_CAPS if policy is not None and is_batch_policy(policy) else PHASE_CAPS
    if execution is not None:
        ceilings = workload.phase_ceilings(policy, execution)
        limits = dict(limits, **{key: ceilings[key] for key in ("owner_read", "global_close")})
    caps = dict(limits if phase_caps is None else phase_caps)
    if (set(caps) != set(limits)
            or any(type(value) is not int or not 0 < value <= limits[key] for key, value in caps.items())
            or allocation_seconds(caps) >= authority_seconds):
        raise ValueError("closure_allocation_invalid")
    native = workload.reference_plan(bundle)
    result = {"schema_version": EXECUTION_PLAN_SCHEMA if execution is not None else PLAN_SCHEMA if policy is None else POLICY_PLAN_SCHEMA,
        "candidate_sha256": native["candidate_sha256"],
        "artifacts": native["artifacts"], "host": native["host"], "hard_limits": native["hard_limits"],
        "scope": native["scope"], "frozen_at": native["frozen_at"], "expires_at": native["expires_at"],
        "phase_caps": caps, "elapsed_cap_seconds": allocation_seconds(caps),
        "journal_count": journal_count, "run_ids": ["closure-journal-" + str(index) for index in range(journal_count)],
        "journal_mode": "native_execution_begin_without_review_attempts", "reference_checks": 200,
        "review_jobs": 0, "prior_authority_reuse": False, "qualification": False, "stdin": "closed"}
    if policy is not None:
        result.update(observation_policy=policy, observation_policy_sha256=digest(policy),
            hard_limits=workload.runtime_hard_limits(policy, execution), source_protocol_limits=native["hard_limits"])
    if execution is not None:
        result.update(execution_policy=execution, execution_policy_sha256=digest(execution),
            expires_at=(datetime.fromisoformat(result["frozen_at"]) + timedelta(seconds=authority_seconds)).isoformat())
    return result


def validate_plan(plan, expected_plan):
    if type(plan) is not dict or plan.get("schema_version") not in {PLAN_SCHEMA, POLICY_PLAN_SCHEMA, EXECUTION_PLAN_SCHEMA} or digest(plan) != expected_plan:
        raise ValueError("workload_plan_binding_invalid")
    profile = None
    execution = validate_execution_binding(plan.get("execution_policy"), plan) if plan["schema_version"] == EXECUTION_PLAN_SCHEMA else None
    if plan["schema_version"] in {POLICY_PLAN_SCHEMA, EXECUTION_PLAN_SCHEMA}:
        policy = validate_observation_policy(plan.get("observation_policy"))
        if plan.get("observation_policy_sha256") != digest(policy):
            raise ValueError("observer_policy_binding_invalid")
        profile = policy["profile"]
    expected = reference_plan(Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent,
        journal_count=plan["journal_count"], phase_caps=plan["phase_caps"], observation_profile=profile,
        execution_profile=execution["profile"] if execution is not None else None)
    if set(plan) != set(expected) or any(plan[key] != expected[key] for key in expected if key not in {"frozen_at", "expires_at"}):
        raise ValueError("workload_candidate_or_recipe_changed")
    frozen, expires = (datetime.fromisoformat(plan[key]) for key in ("frozen_at", "expires_at"))
    if (frozen.tzinfo is None or expires.tzinfo is None or expires - frozen != timedelta(seconds=7200 if execution is None else execution["authority_seconds"])
            or not frozen <= datetime.now(timezone.utc) < expires):
        raise ValueError("closure_plan_expired_or_clock_invalid")
    return plan


def preflight(plan, expected_plan):
    validate_plan(plan, expected_plan)
    return {"status": "prepared_diagnostic_unqualified", "plan_sha256": expected_plan,
        "journal_count": plan["journal_count"], "allocation_seconds": plan["elapsed_cap_seconds"],
        "reference_checks": 200, "review_jobs": 0, "qualification": False}


class WitnessOriginals(OriginalResponses):
    """Record first observed witness timing while retaining unchanged originals."""

    def __init__(self, directory, report, started, observation_policy=None):
        super().__init__(directory)
        self.report, self.started = report, started
        self.observation_policy = observation_policy
        self.capture_original_window = True

    def append(self, entry):
        label, value = entry
        first = None
        authority = None
        if label == "runtime-allocation" and self.observation_policy is not None:
            authority = {key: value[key] for key in ("authority_start", "authority_end")}
            self.report["runtime_authority"] = authority
        if label.endswith("-native-host-qualification") and self.capture_original_window:
            instant = datetime.fromisoformat(value["observed_utc"])
            if instant.tzinfo is None:
                raise ValueError("closure_witness_clock_invalid")
            if self.observation_policy is not None and instant > datetime.now(timezone.utc):
                super().append(entry)
                raise ValueError("observer_clock_invalid")
            limit = 30 if self.observation_policy is None else self.observation_policy["max_age_seconds"]
            bound = instant + timedelta(seconds=limit)
            prior = self.report["first_witness"]
            if prior is None or instant < datetime.fromisoformat(prior["observed_utc"]):
                first = {"observed_utc": instant.isoformat(),
                    "observer_recency_not_after": bound.isoformat(), "original_pin": None,
                    "retained_elapsed_seconds": time.monotonic() - self.started,
                    "closing_latency_seconds": None, "closed_within_original_bound": None}
                self.report["first_witness"] = first
                if self.observation_policy is not None:
                    first.update(max_age_seconds=limit,
                        observation_policy_version=self.observation_policy["schema_version"],
                        observation_policy_sha256=digest(self.observation_policy))
        pin = super().append(entry)
        if first is not None:
            first["original_pin"] = pin
        if authority is not None:
            authority["original_pin"] = pin
        return pin


def create_journals(root, runtime, run_ids, originals):
    """Create actual begin events without preparing or reserving review work."""
    from evidence_wiki import execution_session as native
    with runtime.context.activate(), Workspace.open(root) as workspace:
        for run_id in run_ids:
            with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
                with native.gate(session):
                    native._begin(session)
    receipts = []
    for run_id in run_ids:
        records, receipt, _, _ = authenticated_journal(root, runtime, run_id, originals)
        if len(records) != 1 or records[0]["kind"] != "execution_begin":
            raise ValueError("closure_unexpected_execution_records")
        receipts.append(receipt)
    if runtime.execution.executor._attempts:
        raise ValueError("closure_review_attempt_observed")
    return receipts


def run_diagnostic(plan, expected_plan, directory):
    validate_plan(plan, expected_plan)
    policy = plan.get("observation_policy")
    execution_policy = plan.get("execution_policy")
    directory = Path(directory).absolute()
    if directory.resolve() != directory or directory.exists() or directory.is_relative_to(ROOT):
        raise ValueError("workload_new_private_external_directory_required")
    directory.mkdir(mode=0o700)
    started = time.monotonic()
    report = {"schema_version": EXECUTION_RESULT_SCHEMA if execution_policy is not None else RESULT_SCHEMA if policy is None else POLICY_RESULT_SCHEMA,
        "plan_sha256": expected_plan,
        "candidate_sha256": plan["candidate_sha256"], "study": "closure_feasibility_diagnostic",
        "terminal_outcome": "started_stop_unknown", "failure": None, "failures": [],
        "pair_qualified": False, "pilot_completed": False, "automatic_canonical_completion": False,
        "review_jobs": 0, "review_attempts": None, "applied_jobs": 0, "acceptance_credits": 0,
        "reference_checks": None, "run_ids": plan["run_ids"], "journal_mode": plan["journal_mode"],
        "phase_elapsed": [], "owner_reads": [], "owner_read_identities": [], "first_witness": None,
        "global_closing_utc": None, "closing_status": "unobserved", "journal_receipts": [],
        "initial_journal_receipts": [], "original_response_pins": [], "native_journal_pins": [],
        "input_pins": [], "candidate_checks": [], "cleanup": None, "child_cleanup": None,
        "retention": {"status": "unobserved"}, "elapsed_seconds": None}
    if policy is not None:
        report.update(observation_policy=policy, observation_policy_sha256=digest(policy),
            hard_limits=plan["hard_limits"], source_protocol_limits=plan["source_protocol_limits"],
            closing_owner_read=None, closing_owner_read_identity=None,
            runtime_authority=None,
            observation_freshness={"actual_age_seconds": None, "max_age_seconds": policy["max_age_seconds"],
                "policy_version": policy["schema_version"], "policy_sha256": digest(policy)})
    if execution_policy is not None:
        report.update(execution_policy=execution_policy, execution_policy_sha256=digest(execution_policy))
    originals = WitnessOriginals(directory / "observations", report, started, policy)
    tracker = OwnedChildren(originals, expected_plan)
    root = runtime = None
    opened_runs, snapshots = [], []

    def phase(label, cap, operation):
        tick = time.monotonic()
        begin_utc = datetime.now(timezone.utc).isoformat()
        status = "failed"
        try:
            with workload_deadline(cap):
                result = operation()
                if time.monotonic() - tick > cap:
                    raise _HostDeadline()
                status = "completed"
                return result
        finally:
            report["phase_elapsed"].append({"phase": label, "started_utc": begin_utc,
                "finished_utc": datetime.now(timezone.utc).isoformat(), "status": status,
                "started_elapsed_seconds": tick - started, "elapsed_seconds": time.monotonic() - tick,
                "cap_seconds": cap})

    def fail(error):
        failure = failure_document(error)
        report["failures"].append(failure)
        if report["failure"] is None:
            report["failure"] = failure
        report["terminal_outcome"] = "diagnostic_failed_unqualified"

    def check_candidate(label):
        validate_plan(plan, expected_plan)
        pin = originals.append((label + "-candidate-check", {"candidate_sha256": plan["candidate_sha256"],
            "plan_sha256": expected_plan, "observed_utc": datetime.now(timezone.utc).isoformat()}))
        report["candidate_checks"].append(pin)

    def owner_read():
        if policy is None:
            return workload._owner_read(root, runtime, opened_runs, originals)
        return workload._owner_read(root, runtime, opened_runs, originals, observation_policy=policy)

    def check_records(records):
        if (set(records) != set(opened_runs)
                or any(len(rows) != 1 or rows[0]["kind"] != "execution_begin" for rows in records.values())):
            raise ValueError("closure_unexpected_execution_records")

    try:
        tracker.open()
        main_cap = plan["phase_caps"]["setup"] + 2 * plan["phase_caps"]["owner_read"]
        remaining = (datetime.fromisoformat(plan["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
        with workload_deadline(min(main_cap, remaining)):
            def setup():
                nonlocal root, runtime
                check_candidate("before")
                artifacts = {}
                for name, pin in plan["artifacts"].items():
                    value, raw = workload.read_pinned_artifact(pin)
                    artifacts[name] = value
                    report["input_pins"].append(retain(directory / "inputs" / name, raw, raw=True))
                root, runtime, _, _, _ = provision(directory / "fixture", artifacts["fresh-fixture-input.json"]["fixture"],
                    originals, children=tracker, run_seconds=plan["elapsed_cap_seconds"], allocation_binding={
                        "plan_sha256": expected_plan, "candidate_sha256": plan["candidate_sha256"],
                        "study": report["study"], "phase_caps": plan["phase_caps"], "run_ids": plan["run_ids"], "review_jobs": 0,
                        **({"observation_policy": policy, "observation_policy_sha256": digest(policy),
                            "hard_limits": plan["hard_limits"], "source_protocol_limits": plan["source_protocol_limits"]}
                            if policy is not None else {}),
                        **({"execution_policy": execution_policy, "execution_policy_sha256": digest(execution_policy),
                            "authority_frozen_at": plan["frozen_at"], "authority_expires_at": plan["expires_at"]}
                            if execution_policy is not None else {})},
                    **({"execution_policy": execution_policy} if execution_policy is not None else {}))
                report["reference_checks"] = 200
                retain(directory / "realized-input-manifest.json", file_manifest(root))
                opened_runs.extend(plan["run_ids"])
                report["initial_journal_receipts"] = create_journals(root, runtime, opened_runs, originals)
                report["review_attempts"] = len(runtime.execution.executor._attempts)
            phase("setup", plan["phase_caps"]["setup"], setup)
            tracker.confirm_completed()
            for index in range(2):
                observed, round_snapshots, records = phase("owner-read-" + str(index + 1),
                    plan["phase_caps"]["owner_read"], owner_read)
                tracker.confirm_completed()
                report["owner_reads"].append(observed)
                snapshots.extend(round_snapshots)
                identity = workload.owner_read_identity(observed)
                report["owner_read_identities"].append(identity)
                check_records(records)
                if len(report["owner_read_identities"]) == 2 and report["owner_read_identities"][0] != identity:
                    raise ValueError("workload_owner_two_pass_changed")
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
                report["review_attempts"] = len(runtime.execution.executor._attempts)
                for actual in runtime.execution.executor._attempts.values():
                    try:
                        attempt = actual.attempt
                        verified = runtime.execution.executor.authenticate(runtime.execution.executor.cancel(attempt), attempt)
                        originals.append((attempt.attempt_id + "-cleanup", verified))
                        if verified["state"] == "running" or verified["final_accounting"] is not True:
                            raise ValueError("workload_cleanup_unavailable")
                        cleaned.append(attempt.attempt_id)
                    except (Exception, SystemExit, _HostDeadline) as error:
                        errors.append(error)
                if report["review_attempts"]:
                    errors.append(ValueError("closure_review_attempt_observed"))
            if errors:
                raise ValueError("workload_cleanup_unavailable")
            return {"status": "confirmed", "attempt_ids": cleaned}
        try:
            report["cleanup"] = phase("cleanup", plan["phase_caps"]["cleanup"], cleanup)
        except (Exception, SystemExit, _HostDeadline) as error:
            report["cleanup"] = {"status": "unavailable", "failure": failure_document(error)}
            fail(error)

    def global_close():
        nonlocal root
        errors = []
        if runtime is not None:
            for run_id in opened_runs:
                try:
                    records, receipt, _, _ = authenticated_journal(root, runtime, run_id, originals)
                    report["journal_receipts"].append(receipt)
                    if len(records) != 1 or records[0]["kind"] != "execution_begin":
                        raise ValueError("closure_unexpected_execution_records")
                except (Exception, SystemExit, _HostDeadline) as error:
                    originals.append((run_id + "-history-refusal", failure_document(error)))
                    errors.append(error)
        if root is None and (directory / "fixture/workspace").is_dir():
            root = directory / "fixture/workspace"
        if root is not None:
            report["native_journal_pins"], report["retention"] = workload.collect_journals(root, directory)
            if report["retention"]["status"] != "complete":
                errors.append(ValueError("workload_journal_collection_bound"))
        if (policy is not None and is_batch_policy(policy) and runtime is not None
                and len(report["owner_reads"]) == 2 and report["failure"] is None and not errors):
            originals.capture_original_window = False
            try:
                owner_started = time.monotonic()
                with workload_deadline(plan["phase_caps"]["owner_read"]):
                    observed, closing_snapshots, records = owner_read()
                tracker.confirm_completed()
                if time.monotonic() - owner_started > plan["phase_caps"]["owner_read"]:
                    raise _HostDeadline()
                report["closing_owner_read"] = observed
                identity = workload.owner_read_identity(observed)
                report["closing_owner_read_identity"] = identity
                originals.append(("closing-owner-read", observed))
                snapshots.extend(closing_snapshots)
                check_records(records)
                if any(prior != identity for prior in report["owner_read_identities"]):
                    raise ValueError("closure_owner_closing_changed")
            except (Exception, SystemExit, _HostDeadline) as error:
                errors.append(error)
        check_candidate("after")
        if errors:
            raise errors[0]
        if len(report["owner_reads"]) != 2 or report["failure"] is not None:
            return
        verify_journal_closing(snapshots)
        if execution_policy is not None:
            allocation_pins = [pin for pin in originals.pins if pin["label"] == "runtime-allocation"]
            if len(allocation_pins) != 1:
                raise ValueError("execution_runtime_binding_invalid")
            allocation, _ = workload.read_pinned_artifact(allocation_pins[0])
            workload.validate_credential_originals(originals.pins, plan["expires_at"])
        closing = datetime.now(timezone.utc)
        if execution_policy is not None:
            validate_runtime_allocation(allocation, plan, closing=closing)
        report["global_closing_utc"] = closing.isoformat()
        if policy is not None and not datetime.fromisoformat(plan["frozen_at"]) <= closing < datetime.fromisoformat(plan["expires_at"]):
            raise ValueError("closure_plan_expired_or_clock_invalid")
        if policy is not None:
            authority = report["runtime_authority"]
            if authority is None:
                raise ValueError("closure_authority_clock_invalid")
            begin, end = (datetime.fromisoformat(authority[key]) for key in ("authority_start", "authority_end"))
            if begin.tzinfo is None or end.tzinfo is None or not begin <= closing < end:
                raise ValueError("closure_authority_clock_invalid")
        witness = report["first_witness"]
        if witness is None:
            raise ValueError("workload_owner_current_unavailable")
        witness["closing_latency_seconds"] = (closing - datetime.fromisoformat(witness["observed_utc"])).total_seconds()
        witness["closed_within_original_bound"] = datetime.fromisoformat(witness["observed_utc"]) <= closing < datetime.fromisoformat(witness["observer_recency_not_after"])
        report["closing_status"] = "within_original_bound" if witness["closed_within_original_bound"] else "original_witness_expired"
        if policy is not None:
            report["observation_freshness"]["actual_age_seconds"] = witness["closing_latency_seconds"]
            for observed in report["owner_reads"]:
                validate_observer_window(observed, closing, policy)
            if is_batch_policy(policy):
                validate_observer_window(report["closing_owner_read"], closing, policy)
        if not witness["closed_within_original_bound"]:
            raise ValueError("workload_original_witness_expired")
        report["terminal_outcome"] = "diagnostic_completed_unqualified"

    try:
        phase("global-close", plan["phase_caps"]["global_close"], global_close)
    except (Exception, SystemExit, _HostDeadline) as error:
        fail(error)
        if root is not None and report["retention"]["status"] == "unobserved":
            report["native_journal_pins"], report["retention"] = workload.collect_journals(root, directory)
    report["original_response_pins"] = originals.pins
    report["elapsed_seconds"] = time.monotonic() - started
    if report["elapsed_seconds"] > plan["elapsed_cap_seconds"]:
        fail(_HostDeadline())
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
    parser.add_argument("--journal-count", type=int, default=4)
    parser.add_argument("--observation-profile", choices=("interactive", "batch", "batch-extended"), default="batch")
    parser.add_argument("--execution-profile", choices=("two-hour", "four-hour", "six-hour"))
    args = parser.parse_args(argv)
    try:
        if args.action == "freeze":
            if args.bundle is None or args.output is None:
                raise ValueError("closure_freeze_arguments_required")
            path = args.output.absolute()
            if path.resolve() != path or path.is_relative_to(ROOT) or path.exists():
                raise ValueError("workload_new_private_external_directory_required")
            plan = reference_plan(args.bundle, journal_count=args.journal_count,
                observation_profile=args.observation_profile, execution_profile=args.execution_profile)
            retain(path, plan)
            result = {"plan_path": str(path), "plan_sha256": digest(plan), "runtime_qualification": "NOT RUN"}
        else:
            if args.plan is None or args.expected_plan is None:
                raise ValueError("closure_run_arguments_required")
            plan = _read(args.plan)
            if args.action == "preflight":
                result = preflight(plan, args.expected_plan)
            else:
                if args.directory is None:
                    raise ValueError("closure_run_arguments_required")
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
