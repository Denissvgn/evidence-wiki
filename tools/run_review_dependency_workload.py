#!/usr/bin/env python3
"""Freeze and observe native review cohorts with independent journal custody."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import platform
import stat
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_wiki import Workspace
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_journal_contracts import canonical_records, digest_record, digest_records
from tools._release_decision import _original
from tools._review_dependency_workload import (
    CLAIM_IDS,
    EXAMPLE,
    OriginalResponses,
    OwnedChildren,
    assignment_groups,
    authenticated_journal,
    changed_basis_control,
    completed_native_cycle,
    digest,
    digest_bytes,
    drive,
    failure_document,
    file_manifest,
    journal_run_id,
    mutation,
    provision,
    reference_dependencies,
    retain,
    unstarted_job_timing,
    workload_deadline,
)
from tools.decide_release import _HostDeadline, _read
from tools.verify_acceptance_paths import candidate_manifest

PLAN_SCHEMA = "review-dependency-workload-plan/v2"
RESULT_SCHEMA = "review-dependency-workload-observation/v2"
ARTIFACTS = ("fresh-fixture-input.json", "pilot-job-inputs.json", "role-claim-journal-inputs.json", "measurement-protocol.json", "adapter-contracts.json", "acceptance-cases.json")
PILOT_CAPS = {"setup": 600, "job": 600, "mutation": 600, "owner_read": 120, "global_close": 120, "cleanup": 60}
HARD_LIMITS = {"each_owner_pass_read_seconds": 120, "job_and_MCP_seconds": 600,
    "native_artifact_payload_bytes": 4194304, "native_collection_nodes": 65536,
    "native_record_collection_bytes": 1048576, "native_record_depth": 16,
    "owner_frame_bytes": 2097152, "owner_raw_report_bytes": 1048576,
    "protected_authority_seconds": 7200, "raw_aggregate_retention_bytes": 67108864,
    "raw_journal_retention_bytes": 16777216, "raw_retention_entries": 4096,
    "raw_retention_files": 256, "raw_retention_seconds": 10, "witness_seconds": 30}


def read_pinned_artifact(row):
    path = Path(row["path"]).absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("workload_artifact_path_invalid")
    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576 or digest_bytes(raw) != row["raw_sha256"]:
        raise ValueError("workload_artifact_binding_invalid")
    return _original(raw), raw



def read_pinned_native_records(pin):
    """Native child/header depth remains native when reading prior collections."""
    path = Path(pin["path"]).absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("workload_artifact_path_invalid")
    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576 or len(raw) != pin["size_bytes"] or digest_bytes(raw) != pin["raw_sha256"]:
        raise ValueError("workload_artifact_binding_invalid")
    records = json.loads(raw)
    if canonical_records(records) != raw:
        raise ValueError("workload_pilot_measurement_invalid")
    return records, raw


def collect_journals(root, directory):
    """Retain raw journals independently of the smaller native record codec."""
    pins, total, entries = [], 0, 0
    def traversal_error(error):
        raise error
    try:
        with workload_deadline(10):
            for base in (root / "runs", root.parent / "fixture-private"):
                if not base.exists():
                    continue
                for folder, dirs, names in os.walk(base, followlinks=False, onerror=traversal_error):
                    dirs.sort()
                    entries += len(dirs) + len(names)
                    if entries > 4096 or any((Path(folder) / name).is_symlink() for name in dirs):
                        raise ValueError("workload_journal_collection_bound")
                    for name in sorted(names):
                        if not name.endswith(".jsonl"):
                            continue
                        path = Path(folder) / name
                        if len(pins) >= 256 or path.resolve(strict=True) != path or not stat.S_ISREG(path.stat().st_mode):
                            raise ValueError("workload_journal_collection_bound")
                        with path.open("rb") as stream:
                            raw = stream.read(16_777_217)
                        total += len(raw)
                        if len(raw) > 16_777_216 or total > 67_108_864:
                            raise ValueError("workload_journal_collection_bound")
                        pins.append(dict(retain(directory / "journals" / (str(len(pins)) + ".jsonl"), raw, raw=True), original_path=str(path)))
    except (Exception, SystemExit, _HostDeadline) as error:
        return pins, {"status": "incomplete", "failure": failure_document(error)}
    return pins, {"status": "complete", "failure": None}


def host_identity(host):
    return {"host_id": host, "interpreter": str(Path(sys.executable).resolve()), "python": platform.python_version(),
        "system": platform.system(), "machine": platform.machine(), "fixture_sources": {
            name: digest_bytes((EXAMPLE / name).read_bytes()) for name in ("fixture_host.py", "support.py", "fake_workers.py")},
        "controller_separation": "protected_distinct_fixture_keys", "clock": "fresh_native_wall_clock",
        "transport": "in_process_jsonrpc" if host == "scoped_mcp" else "public_python_session",
        "qualification": "authenticated_reference_worker_observations"}


def _inputs(bundle):
    paths = {name: bundle / name for name in ARTIFACTS}
    paths["acceptance-cases.json"] = bundle.parent / "preparation-v1/acceptance-cases.json"
    return {name: {"path": str(path), "raw_sha256": digest_bytes(path.read_bytes())} for name, path in paths.items()}


def allocation_seconds(caps, jobs):
    return caps["setup"] + jobs * caps["job"] + caps["mutation"] + 2 * caps["owner_read"] + caps["global_close"] + caps["cleanup"]


def validate_inputs(artifacts, study, host, case_id):
    """Inspect frozen scope and role/journal obligations without running owners."""
    fixture = artifacts["fresh-fixture-input.json"]
    inventory = owner("_strict_contract").claims_document(fixture["fixture"]["inventory"])
    owner("_strict_contract").policy_document(fixture["fixture"]["global_inputs"]["policy"])
    if (fixture["fresh_custody"]["prior_authority_reuse"] is not False
            or fixture["fresh_custody"]["authority_seconds"] != 7200
            or len(inventory["claims"]) != 6 or {row["id"] for row in inventory["claims"]} != CLAIM_IDS
            or len(inventory["questions"]) != 2 or {row["slug"] for row in inventory["questions"]} != {"capacity", "demand"}
            or set(fixture["fixture"]["source_material"]) != {"shared", "demand", "unused"}
            or artifacts["measurement-protocol.json"]["hard_limits"] != HARD_LIMITS
            or artifacts["role-claim-journal-inputs.json"]["assembly"]["page_chars"] != 16384):
        raise ValueError("workload_full_scope_invalid")
    margin = next(row for row in inventory["claims"] if row["id"] == "margin")
    if {row["source_id"] for row in margin["evidence"]} != {"shared", "demand"} or not margin["calculations"]:
        raise ValueError("workload_fixture_lineage_invalid")
    rows = artifacts["pilot-job-inputs.json" if study == "pilot" else "role-claim-journal-inputs.json"]["assignments"]
    rows = [copy.deepcopy(row) for row in rows if row["host_id"] == host and row["case_id"] == case_id]
    assignment_groups(rows)
    expected = {(cycle, claim, config) for cycle, claims in (
        ("baseline", {"observed-capacity", "independent-note"} if study == "pilot" else CLAIM_IDS),
        ("first-change", {"observed-capacity"} if study == "pilot" else CLAIM_IDS)) for claim in claims for config in ("evidence", "critic")}
    if {(row["cycle"], row["claim_id"], row["config_ref"]) for row in rows} != expected or len(rows) != (6 if study == "pilot" else 24):
        raise ValueError("workload_assignment_invalid")
    claim_questions = {claim["id"]: claim["question_slug"] for claim in inventory["claims"]}
    originals = {question["slug"]: question["original_id"] for question in inventory["questions"]}
    for row in rows:
        if row["original_id"] != originals[claim_questions[row["claim_id"]]] or any(row[key] is not None for key in ("native_run_id", "attempt_id", "application_event_ref")):
            raise ValueError("workload_assignment_invalid")
        row["native_run_id"] = journal_run_id(row["journal_key"])
    case = next(row for row in artifacts["acceptance-cases.json"]["cases"] if row["id"] == case_id)
    if study == "pilot" and (case_id != "independent-claim-text" or host != "direct"
            or case["mutations"] != [{"op": "replace", "path": "/inventory/claims/5/text", "value": "The retained service note documents scheduled maintenance."}]):
        raise ValueError("workload_pilot_mutation_invalid")
    return rows, case



def completed_pilot_measurements(measured, candidate_sha256):
    """Budget from exact admitted pilot originals; historical costs grant no review."""
    if type(measured) is not dict or set(measured) != {"pilot_report_pin", "pilot_plan_pin", "phase_caps", "observed_worst_seconds", "group_overhead_cap_seconds", "observed_group_overhead_seconds"}:
        raise ValueError("workload_pilot_measurement_invalid")
    report, _ = read_pinned_artifact(measured["pilot_report_pin"])
    prior_plan, _ = read_pinned_artifact(measured["pilot_plan_pin"])
    expected_prior = reference_plan(Path(prior_plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent)
    if set(prior_plan) != set(expected_prior) or any(prior_plan[key] != expected_prior[key] for key in expected_prior if key not in {"frozen_at", "expires_at"}):
        raise ValueError("workload_pilot_measurement_invalid")
    if (report.get("terminal_outcome") != "pilot_completed_unqualified" or report.get("study") != "pilot"
            or report.get("candidate_sha256") != candidate_sha256 or prior_plan.get("candidate_sha256") != candidate_sha256
            or digest(prior_plan) != report.get("plan_sha256") or prior_plan.get("schema_version") != PLAN_SCHEMA
            or prior_plan.get("study") != "pilot" or prior_plan.get("phase_caps") != PILOT_CAPS
            or prior_plan.get("elapsed_cap_seconds") != 5220 or prior_plan.get("group_overhead_cap_seconds") != 0
            or report.get("applied_jobs") != 6 or report.get("pair_qualified") is not False
            or len(report.get("assignments", [])) != 6 or len(report.get("job_elapsed", [])) != 6):
        raise ValueError("workload_pilot_measurement_invalid")
    if (report.get("schema_version") != RESULT_SCHEMA or report.get("failure") is not None
            or report.get("mode") != "legacy" or report.get("host_id") != "direct" or report.get("case_id") != "independent-claim-text"
            or report.get("automatic_canonical_completion") is not False or report.get("cleanup", {}).get("status") != "confirmed"
            or report.get("child_cleanup", {}).get("status") != "confirmed" or report.get("retention", {}).get("status") != "complete"
            or type(report.get("elapsed_seconds")) not in (int, float) or not math.isfinite(report["elapsed_seconds"])
            or not 0 < report["elapsed_seconds"] <= 5220 or len(report.get("owner_reads", [])) != 2
            or report.get("control", {}).get("native_error_code") != "RESEARCH_BASIS_CHANGED"
            or report["control"].get("applied") is not False or report.get("control_accounting") != "reserved_unstarted_no_review_credit"
            or report.get("cross_journal_compatibility") != "authenticated_partial_scope"):
        raise ValueError("workload_pilot_measurement_invalid")
    first, second = report["owner_reads"]
    if owner_read_identity(first) != owner_read_identity(second) or report["current_gate_results"] != second:
        raise ValueError("workload_pilot_measurement_invalid")
    closing = datetime.fromisoformat(report["global_closing_utc"])
    if closing.tzinfo is None:
        raise ValueError("workload_pilot_measurement_invalid")
    for observed in (first, second):
        witnesses = observed["native_host_witnesses"]
        clocks = [datetime.fromisoformat(witness["observed_utc"]) for witness in witnesses]
        if (observed["witness_semantics"] != "observer_recency_bound_not_native_calibration_witness"
                or observed["native_calibration_witness"] != "unavailable_legacy_qualification_profile"
                or not clocks or any(clock.tzinfo is None or clock > closing for clock in clocks)
                or datetime.fromisoformat(observed["observer_recency_not_after"]) != min(clocks) + timedelta(seconds=30)
                or closing >= min(clocks) + timedelta(seconds=30)):
            raise ValueError("workload_pilot_measurement_invalid")
    rows = report["assignments"]
    groups = assignment_groups(rows)
    if len(groups) != 3:
        raise ValueError("workload_pilot_measurement_invalid")
    frozen = {(row["native_run_id"], row["claim_id"], row["config_ref"]): row for row in prior_plan["assignments"]}
    assigned = {(row["native_run_id"], row["claim_id"], row["config_ref"]): row for row in rows}
    if len(frozen) != 6 or len(assigned) != 6 or set(frozen) != set(assigned):
        raise ValueError("workload_pilot_measurement_invalid")
    timings = {}
    original_pins = report["original_response_pins"]
    for row in report["job_elapsed"]:
        key = (row["native_run_id"], row["claim_id"], row["config_ref"])
        if key not in assigned or key in timings:
            raise ValueError("workload_pilot_measurement_invalid")
        assignment = assigned[key]
        pin = row.get("original_timing_pin")
        if pin is None or not any(all(source.get(field) == pin.get(field) for field in ("path", "raw_sha256", "size_bytes")) for source in original_pins):
            raise ValueError("workload_pilot_measurement_invalid")
        original, raw = read_pinned_artifact(pin)
        if len(raw) != pin["size_bytes"] or original != {field: value for field, value in row.items() if field != "original_timing_pin"}:
            raise ValueError("workload_pilot_measurement_invalid")
        if (row["status"] != "applied" or row["native_passed"] is not True or row["cap_seconds"] != 600
                or type(row["elapsed_seconds"]) not in (int, float) or not math.isfinite(row["elapsed_seconds"])
                or not 0 < row["elapsed_seconds"] <= row["cap_seconds"]
                or any(row[field] != assignment[field] for field in ("attempt_id", "application_event_ref", "native_record_sha256",
                    "host_id", "case_id", "cycle", "task_role", "controller_ref", "journal_key"))
                or any(row[field] != frozen[key][field] for field in ("host_id", "case_id", "cycle", "task_role", "controller_ref", "journal_key"))):
            raise ValueError("workload_pilot_measurement_invalid")
        receipts = [receipt for receipt in report["journal_receipts"] if receipt["run_id"] == row["native_run_id"]]
        if not receipts:
            raise ValueError("workload_pilot_measurement_invalid")
        # Each original owner collection remains under its native codec. This
        # verifies immutable historical proof binding, not fresh HMAC authority.
        receipt = receipts[-1]
        records, record_raw = read_pinned_native_records(receipt["original_pin"])
        if canonical_records(records) != record_raw or digest_records(records) != receipt["records_sha256"]:
            raise ValueError("workload_pilot_measurement_invalid")
        applied = [record for record in records if record["kind"] == "execution_applied"
            and record["data"].get("operation") == "evaluate" and record["data"].get("attempt_id") == row["attempt_id"]
            and record["data"].get("claim_id") == row["claim_id"] and record["data"].get("profile_ref") == row["config_ref"]]
        if (len(applied) != 1 or applied[0]["event_ref"] != row["application_event_ref"]
                or digest_record(applied[0]) != row["native_record_sha256"] or applied[0]["data"].get("passed") is not True
                or applied[0]["data"].get("strict_basis_id") != row["strict_basis_id"]):
            raise ValueError("workload_pilot_measurement_invalid")
        timings[key] = row
    phases = {row["phase"]: row for row in report["phase_elapsed"]}
    expected_phases = {"setup", "mutation", "owner-read-0", "owner-read-1", "cleanup", "global-close", *groups}
    if len(phases) != len(report["phase_elapsed"]) or set(phases) != expected_phases:
        raise ValueError("workload_pilot_measurement_invalid")
    for label, phase in phases.items():
        expected_cap = 1200 if label in groups else PILOT_CAPS["owner_read" if label.startswith("owner-read-") else label.replace("-", "_")]
        if (phase["cap_seconds"] != expected_cap or type(phase["elapsed_seconds"]) not in (int, float)
                or not math.isfinite(phase["elapsed_seconds"]) or not 0 < phase["elapsed_seconds"] <= expected_cap):
            raise ValueError("workload_pilot_measurement_invalid")
    worst = {name: phases[name.replace("_", "-")]["elapsed_seconds"] for name in ("setup", "mutation", "cleanup", "global_close")}
    worst["owner_read"] = max(phases["owner-read-0"]["elapsed_seconds"], phases["owner-read-1"]["elapsed_seconds"])
    worst["job"] = max(row["elapsed_seconds"] for row in timings.values())
    overheads = []
    for key, group in groups.items():
        job_seconds = sum(timings[(row["native_run_id"], row["claim_id"], row["config_ref"])]["elapsed_seconds"] for row in group)
        overhead = phases[key]["elapsed_seconds"] - job_seconds
        if overhead < 0:
            raise ValueError("workload_pilot_measurement_invalid")
        overheads.append(overhead)
    return worst, max(overheads)


def reference_plan(bundle, *, study="pilot", host="direct", case_id="independent-claim-text", measured_recipe=None):
    """Freeze exact candidate, assignments and prospective phase caps before work."""
    bundle = Path(bundle).absolute()
    if bundle.resolve(strict=True) != bundle or study not in {"pilot", "full"}:
        raise ValueError("workload_bundle_not_canonical")
    pins = _inputs(bundle)
    artifacts = {name: read_pinned_artifact(pin)[0] for name, pin in pins.items()}
    assignments, case = validate_inputs(artifacts, study, host, case_id)
    caps = dict(PILOT_CAPS)
    group_overhead_cap = 0
    recipe_pin = None
    if study == "full":
        if measured_recipe is None:
            raise ValueError("workload_measured_allocation_required")
        path = Path(measured_recipe).absolute()
        recipe_pin = {"path": str(path), "raw_sha256": digest_bytes(path.read_bytes())}
        measured, _ = read_pinned_artifact(recipe_pin)
        caps = measured["phase_caps"]
        worst, observed_overhead = completed_pilot_measurements(measured, digest(candidate_manifest(ROOT)))
        group_overhead_cap = measured["group_overhead_cap_seconds"]
        if (measured["observed_worst_seconds"] != worst or measured["observed_group_overhead_seconds"] != observed_overhead
                or type(group_overhead_cap) is not int or not math.ceil(observed_overhead * 1.25) <= group_overhead_cap <= 7200):
            raise ValueError("workload_measured_allocation_required")
        if (set(caps) != set(PILOT_CAPS) or set(worst) != set(PILOT_CAPS)
                or any(type(caps[key]) is not int or not 0 < caps[key] <= PILOT_CAPS[key]
                    or type(worst[key]) not in (int, float) or not math.ceil(worst[key] * 1.25) <= caps[key] for key in caps)):
            raise ValueError("workload_measured_allocation_required")
    outer = allocation_seconds(caps, len(assignments)) + (2 * caps["global_close"] if study == "full" else 0) + len(assignment_groups(assignments)) * group_overhead_cap
    if outer > 7200 or study == "pilot" and outer != 5220:
        raise ValueError("workload_allocation_exceeds_authority")
    instant = datetime.now(timezone.utc)
    return {"schema_version": PLAN_SCHEMA, "candidate_sha256": digest(candidate_manifest(ROOT)), "artifacts": pins,
        "host": host_identity(host), "case_id": case_id, "study": study, "assignments": assignments,
        "phase_caps": caps, "group_overhead_cap_seconds": group_overhead_cap, "elapsed_cap_seconds": outer, "hard_limits": HARD_LIMITS, "measured_recipe": recipe_pin,
        "control_run_id": "control-" + digest_bytes((host + "/" + case_id).encode())[:24],
        "control": "reserved_native_attempt_changed_basis_no_dispatch_no_application",
        "review_journals": len(assignment_groups(assignments)), "control_journals": 1,
        "frozen_at": instant.isoformat(), "expires_at": (instant + timedelta(seconds=7200)).isoformat(),
        "stdin": "closed", "scope": "all_six_claims_two_questions_global_policy_sources_computation"}


def validate_plan(plan, expected_plan):
    if type(plan) is not dict or plan.get("schema_version") != PLAN_SCHEMA or digest(plan) != expected_plan:
        raise ValueError("workload_plan_binding_invalid")
    expected = reference_plan(Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent,
        study=plan["study"], host=plan["host"]["host_id"], case_id=plan["case_id"],
        measured_recipe=plan["measured_recipe"]["path"] if plan["measured_recipe"] else None)
    if set(plan) != set(expected) or any(plan[key] != expected[key] for key in expected if key not in {"frozen_at", "expires_at"}):
        raise ValueError("workload_candidate_or_recipe_changed")
    frozen, expires = (datetime.fromisoformat(plan[key]) for key in ("frozen_at", "expires_at"))
    if frozen.tzinfo is None or expires.tzinfo is None or not frozen <= datetime.now(timezone.utc) < expires or expires - frozen != timedelta(seconds=7200):
        raise ValueError("workload_plan_expired_or_clock_invalid")
    return plan


def preflight(plan, expected_plan):
    validate_plan(plan, expected_plan)
    return {"status": "prepared_runtime_unqualified", "plan_sha256": expected_plan,
        "review_jobs": len(plan["assignments"]), "review_journals": plan["review_journals"],
        "control_journals": 1, "allocation_seconds": plan["elapsed_cap_seconds"],
        "cross_journal_compatibility": "requires_fresh_native_global_owner_reads",
        "qualification": False}


def _owner_read(root, runtime, run_ids, originals):
    snapshots, receipts, records = [], [], {}
    # Authenticate complete histories before obtaining any short current witness.
    for run_id in run_ids:
        rows, receipt, snapshot, _ = authenticated_journal(root, runtime, run_id, originals)
        records[run_id] = rows
        receipts.append(receipt)
        snapshots.append(snapshot)
    with runtime.context.activate(), Workspace.open(root) as workspace:
        strict = workspace.strict.check()
        originals.append(("owner-strict-current", strict))
        publication = workspace.strict.export()
        originals.append(("owner-publication-current", publication))
        current = []
        witness_expiries = []
        native_witnesses = []
        for run_id in run_ids:
            with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session:
                from evidence_wiki import execution_session as native
                from evidence_wiki import host_qualification
                with native._current_facts(session, operation="current"):
                    view = native._current_view(session)
                    originals.append((run_id + "-current", view))
                    witness = host_qualification(session)
                    originals.append((run_id + "-native-host-qualification", witness))
                    qualification = native.current_qualifications(session)
                    if (qualification["state"] != "qualified"
                            or qualification != view["qualification"]):
                        raise ValueError("workload_owner_current_unavailable")
                native_witnesses.append(witness)
                witness_expiries.append(datetime.fromisoformat(witness["observed_utc"]) + timedelta(seconds=30))
                current.append(view)
    return {"strict": strict, "publication": publication, "journal_receipts": receipts,
        "native_host_witnesses": native_witnesses, "native_current": current,
        "observer_recency_not_after": min(witness_expiries).isoformat(),
        "witness_semantics": "observer_recency_bound_not_native_calibration_witness",
        "native_calibration_witness": "unavailable_legacy_qualification_profile"}, snapshots, records



def owner_read_identity(observed):
    """Compare source/head/basis and whole semantic owner outputs across rounds."""
    semantic = {}
    for name in ("strict", "publication"):
        semantic[name] = {key: value for key, value in observed[name].items() if key != "evaluated_at"}
    semantic["journals"] = [{key: row[key] for key in ("run_id", "records_sha256", "head_ref", "source_sha256", "state_sha256")} for row in observed["journal_receipts"]]
    return digest(semantic)


def run_workload(plan, expected_plan, host, case_id, mode, directory):
    validate_plan(plan, expected_plan)
    if host != plan["host"]["host_id"] or case_id != plan["case_id"] or mode not in {"legacy", "selective"}:
        raise ValueError("workload_selection_invalid")
    directory = Path(directory).absolute()
    if directory.resolve() != directory or directory.exists() or directory.is_relative_to(ROOT):
        raise ValueError("workload_new_private_external_directory_required")
    directory.mkdir(mode=0o700)
    originals = OriginalResponses(directory / "observations")
    rows = copy.deepcopy(plan["assignments"])
    report = {"schema_version": RESULT_SCHEMA, "candidate_sha256": plan["candidate_sha256"], "plan_sha256": expected_plan,
        "host_id": host, "case_id": case_id, "study": plan["study"], "mode": mode, "terminal_outcome": "started_stop_unknown",
        "failure": None, "assignments": rows, "applied_jobs": None, "pair_qualified": False,
        "automatic_canonical_completion": False, "cross_journal_compatibility": "unobserved", "phase_elapsed": [],
        "job_elapsed": [unstarted_job_timing(row, plan["phase_caps"]["job"]) for row in rows],
        "journal_receipts": [], "original_response_pins": [], "native_journal_pins": [],
        "owner_reads": [], "global_closing_utc": None, "cleanup": None, "control": None,
        "current_gate_results": None, "strict_accepted_claim_ids": [], "question_accepted_ids": [], "work_attribution": [],
        "reference_dependency_changes": None, "control_accounting": "reserved_unstarted_no_review_credit",
        "retention": {"status": "unobserved"}, "elapsed_seconds": None}
    root = runtime = None
    opened_runs, snapshots = [], []
    final_close = earliest_witness = None
    artifacts = {}
    started = time.monotonic()
    def phase(label, cap, operation):
        tick = time.monotonic()
        try:
            with workload_deadline(cap):
                result = operation()
                if time.monotonic() - tick > cap:
                    raise _HostDeadline()
                return result
        finally:
            report["phase_elapsed"].append({"phase": label, "elapsed_seconds": time.monotonic() - tick, "cap_seconds": cap})
    def fail(error):
        report["failure"] = failure_document(error)
        report["terminal_outcome"] = "unavailable" if mode == "selective" else "failed"
        report["pair_qualified"] = False
    tracker = OwnedChildren(originals, expected_plan)
    tracker.open()
    try:
        if mode == "selective":
            raise ValueError("selective_owner_unavailable")
        remaining = (datetime.fromisoformat(plan["expires_at"]) - datetime.now(timezone.utc)).total_seconds()
        main_cap = plan["elapsed_cap_seconds"] - plan["phase_caps"]["cleanup"] - plan["phase_caps"]["global_close"]
        with workload_deadline(min(main_cap, remaining)):
            def setup():
                nonlocal root, runtime
                for name, pin in plan["artifacts"].items():
                    value, raw = read_pinned_artifact(pin)
                    artifacts[name] = value
                    retain(directory / "inputs" / name, raw, raw=True)
                realized_rows, selected_case = validate_inputs(artifacts, plan["study"], host, case_id)
                if realized_rows != rows:
                    raise ValueError("workload_assignment_invalid")
                fixture_input = artifacts["fresh-fixture-input.json"]["fixture"]
                root, runtime, source_rows, signing_keys, admitted_claims = provision(directory / "fixture", fixture_input, originals,
                    run_seconds=plan["elapsed_cap_seconds"], allocation_binding={"plan_sha256": expected_plan,
                        "candidate_sha256": plan["candidate_sha256"], "assignments": rows, "phase_caps": plan["phase_caps"],
                        "group_overhead_cap_seconds": plan["group_overhead_cap_seconds"], "control_run_id": plan["control_run_id"]})
                retain(directory / "realized-input-manifest.json", file_manifest(root))
                retain(directory / "native-id-translation.json", {name: {key: row[key] for key in ("source_id", "normalized_path", "revision")} for name, row in source_rows.items()})
                dependencies = reference_dependencies(root, runtime)
                originals.append(("baseline-reference-dependencies", dependencies))
                return fixture_input, selected_case, source_rows, signing_keys, admitted_claims, dependencies
            fixture, case, sources, keys, claims, baseline_dependencies = phase("setup", plan["phase_caps"]["setup"], setup)
            groups = assignment_groups(rows)
            for cycle in ("baseline", "first-change"):
                if cycle == "first-change":
                    opened_runs.append(plan["control_run_id"])
                    def change():
                        control = changed_basis_control(root, runtime, plan["control_run_id"], originals,
                            lambda: mutation(root, fixture, case, sources, keys, runtime, originals))
                        dependencies = reference_dependencies(root, runtime)
                        originals.append(("changed-reference-dependencies", dependencies))
                        changed = {claim for claim in baseline_dependencies if baseline_dependencies[claim]["material_sha256"] != dependencies[claim]["material_sha256"]}
                        if changed != set(case["mandatory_refresh_claims"]):
                            raise ValueError("workload_reference_mapping_discrepancy")
                        return control, dependencies, changed
                    report["control"], changed_dependencies, changed_claims = phase("mutation", plan["phase_caps"]["mutation"], change)
                    report["reference_dependency_changes"] = sorted(changed_claims)
                for key, assignments in groups.items():
                    if assignments[0]["cycle"] != cycle:
                        continue
                    run_id = assignments[0]["native_run_id"]
                    opened_runs.append(run_id)
                    def review_group(run_id=run_id, claim_id=assignments[0]["claim_id"]):
                        drive(root, runtime, host, run_id, originals, case_id=case_id, claim_ids={claim_id},
                            job_cap_seconds=plan["phase_caps"]["job"], job_elapsed=report["job_elapsed"])
                        return authenticated_journal(root, runtime, run_id, originals)
                    records, receipt, _, _ = phase(key, 2 * plan["phase_caps"]["job"] + plan["group_overhead_cap_seconds"], review_group)
                    report["journal_receipts"].append(receipt)
                    for assignment in assignments:
                        applied = [row for row in records if row["kind"] == "execution_applied"
                            and row["data"].get("operation") == "evaluate" and row["data"].get("claim_id") == assignment["claim_id"]
                            and row["data"].get("profile_ref") == assignment["config_ref"]]
                        if len(applied) != 1 or applied[0]["data"].get("passed") is not True:
                            raise ValueError("workload_native_review_application_missing")
                        assignment.update(attempt_id=applied[0]["data"]["attempt_id"], application_event_ref=applied[0]["event_ref"],
                            native_record_sha256=digest_record(applied[0]), journal_head_pin=receipt["head_ref"], journal_source_pin=receipt["source_sha256"])
                        timings = [timing for timing in report["job_elapsed"] if (timing["native_run_id"], timing["claim_id"], timing["config_ref"]) == (run_id, assignment["claim_id"], assignment["config_ref"])]
                        if (len(timings) != 1 or timings[0]["status"] != "applied" or timings[0]["native_passed"] is not True
                                or timings[0]["elapsed_seconds"] > plan["phase_caps"]["job"]
                                or any(timings[0][field] != assignment[field] for field in ("attempt_id", "application_event_ref", "native_record_sha256"))):
                            raise ValueError("workload_job_timing_binding_invalid")
                        if cycle == "first-change":
                            report["work_attribution"].append({"attempt_id": assignment["attempt_id"], "claim_id": assignment["claim_id"],
                                "reference_only": True, "reference_dependency_unchanged": assignment["claim_id"] not in changed_claims,
                                "baseline_reference_sha256": baseline_dependencies[assignment["claim_id"]]["material_sha256"],
                                "changed_reference_sha256": changed_dependencies[assignment["claim_id"]]["material_sha256"]})
                if plan["study"] == "full":
                    cycle_runs = [row[0]["native_run_id"] for row in groups.values() if row[0]["cycle"] == cycle]
                    def resolve_global(cycle_runs=cycle_runs, cycle=cycle):
                        from evidence_wiki import execution_session as native
                        with runtime.context.activate(), Workspace.open(root) as workspace:
                            with workspace.research.session(runtime.policy, run_id=cycle_runs[-1], execution=runtime.execution) as session:
                                for original in runtime.policy.to_dict()["scope"]["question_ids"]:
                                    native._resolve(session, original)
                                originals.append_records(cycle + "-global-resolution", runtime.execution.records(session))
                        return _owner_read(root, runtime, cycle_runs, originals)
                    observed, _, by_run = phase(cycle + "-global", plan["phase_caps"]["global_close"], resolve_global)
                    complete = [record for records in by_run.values() for record in records if record["kind"] == "execution_applied"]
                    if not completed_native_cycle(observed["strict"], complete, observed["publication"]):
                        raise ValueError("workload_cross_journal_scope_unsupported")
            review_runs = [group[0]["native_run_id"] for group in groups.values()]
            snapshots = []
            first_read_identity = None
            closing_start = None
            earliest_witness = None
            for index in range(2):
                observed, round_snapshots, _ = phase("owner-read-" + str(index), plan["phase_caps"]["owner_read"],
                    lambda: _owner_read(root, runtime, [*review_runs, plan["control_run_id"]], originals))
                if closing_start is None:
                    closing_start = time.monotonic()
                identity = owner_read_identity(observed)
                if first_read_identity is not None and first_read_identity != identity:
                    raise ValueError("workload_owner_two_pass_changed")
                first_read_identity = identity
                snapshots.extend(round_snapshots)
                report["owner_reads"].append(observed)
                expiry = datetime.fromisoformat(observed["observer_recency_not_after"])
                earliest_witness = expiry if earliest_witness is None else min(expiry, earliest_witness)
            def close():
                from tools.replay_qualification_native import verify_journal_closing
                verify_journal_closing(snapshots)
                if datetime.now(timezone.utc) >= earliest_witness:
                    raise ValueError("workload_original_witness_expired")
                report["global_closing_utc"] = datetime.now(timezone.utc).isoformat()
            final_close = close
            report["current_gate_results"] = observed
            report["strict_accepted_claim_ids"] = sorted(row["claim"]["id"] for row in observed["strict"]["claims"] if row["accepted"])
            report["question_accepted_ids"] = sorted(row["original_id"] for row in observed["publication"]["original_outcomes"] if row["accepted"])
            if plan["study"] == "pilot" and (all(row["accepted"] for row in observed["strict"]["claims"])
                    or observed["publication"]["verdict"] == "ship"):
                raise ValueError("workload_partial_scope_overclaimed")
            report["applied_jobs"] = len(rows)
            report["cross_journal_compatibility"] = "authenticated_partial_scope" if plan["study"] == "pilot" else "authenticated_full_scope"
            report["pair_qualified"] = plan["study"] == "full" and len(rows) == 24 and any(row["reference_dependency_unchanged"] for row in report["work_attribution"])
            report["terminal_outcome"] = "pilot_completed_unqualified" if plan["study"] == "pilot" else "completed"
    except (Exception, SystemExit, _HostDeadline) as error:
        fail(error)
    finally:
        # Safety cleanup still runs after an outer stop; it never extends a
        # successful allocation or current authority. Both owners share one cap.
        def cleanup():
            errors = []
            try:
                report["child_cleanup"] = tracker.close()
            except (Exception, SystemExit, _HostDeadline) as error:
                errors.append(error)
                report["child_cleanup"] = {"status": "unavailable", "failure": failure_document(error)}
            results = []
            if runtime is not None:
                for actual in runtime.execution.executor._attempts.values():
                    try:
                        stored = actual.attempt
                        observed = runtime.execution.executor.cancel(stored)
                        verified = runtime.execution.executor.authenticate(observed, stored)
                        originals.append((stored.attempt_id + "-cleanup", verified))
                        if verified["state"] == "running" or verified["final_accounting"] is not True:
                            raise ValueError("workload_cleanup_unavailable")
                        results.append(stored.attempt_id)
                    except (Exception, SystemExit, _HostDeadline) as error:
                        errors.append(error)
            if errors:
                raise ValueError("workload_cleanup_unavailable")
            return {"status": "confirmed", "attempt_ids": results}
        try:
            report["cleanup"] = phase("cleanup", plan["phase_caps"]["cleanup"], cleanup)
        except (Exception, SystemExit, _HostDeadline) as error:
            report["cleanup"] = {"status": "unavailable", "failure": failure_document(error)}
            fail(error)
    def global_close():
        nonlocal root
        history_failed = False
        if runtime is not None:
            for run_id in opened_runs:
                try:
                    _, receipt, _, _ = authenticated_journal(root, runtime, run_id, originals)
                    report["journal_receipts"].append(receipt)
                except (Exception, SystemExit, _HostDeadline) as error:
                    originals.append((run_id + "-history-refusal", failure_document(error)))
                    history_failed = True
        if root is None and (directory / "fixture/workspace").is_dir():
            root = directory / "fixture/workspace"
        if root is not None:
            report["native_journal_pins"], report["retention"] = collect_journals(root, directory)
            if report["retention"]["status"] != "complete":
                raise ValueError("workload_journal_collection_bound")
        validate_plan(plan, expected_plan)
        if history_failed:
            raise ValueError("workload_owner_current_unavailable")
        if report["terminal_outcome"] in {"completed", "pilot_completed_unqualified"}:
            final_close()
    try:
        phase("global-close", plan["phase_caps"]["global_close"], global_close)
    except (Exception, SystemExit, _HostDeadline) as error:
        fail(error)
        # A failed history read must not erase key-free raw originals. The raw
        # collector has its own unchanged 10-second/16MiB/64MiB bounds.
        if root is not None and report["retention"]["status"] == "unobserved":
            report["native_journal_pins"], report["retention"] = collect_journals(root, directory)
    report["original_response_pins"] = originals.pins
    report["elapsed_seconds"] = time.monotonic() - started
    if report["elapsed_seconds"] > plan["elapsed_cap_seconds"]:
        fail(TimeoutError("workload_outer_allocation_expired"))
    retain(directory / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "preflight", "run"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan")
    parser.add_argument("--host", choices=("direct", "scoped_mcp"), default="direct")
    parser.add_argument("--case", default="independent-claim-text")
    parser.add_argument("--study", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--measured-recipe", type=Path)
    parser.add_argument("--mode", choices=("legacy", "selective"), default="legacy")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "freeze":
            if args.bundle is None or args.output is None:
                raise ValueError("workload_freeze_arguments_required")
            path = args.output.absolute()
            if path.resolve() != path or path.is_relative_to(ROOT) or path.exists():
                raise ValueError("workload_new_private_external_directory_required")
            plan = reference_plan(args.bundle, study=args.study, host=args.host, case_id=args.case, measured_recipe=args.measured_recipe)
            retain(path, plan)
            result = {"plan_path": str(path), "plan_sha256": digest(plan), "runtime_qualification": "NOT RUN"}
        else:
            if args.plan is None or args.expected_plan is None:
                raise ValueError("workload_run_arguments_required")
            plan = _read(args.plan)
            if args.action == "preflight":
                result = preflight(plan, args.expected_plan)
            else:
                if args.directory is None:
                    raise ValueError("workload_run_arguments_required")
                report = run_workload(plan, args.expected_plan, args.host, args.case, args.mode, args.directory)
                result = {"report_path": str(args.directory / "report.json"), "report_sha256": digest(report), "terminal_outcome": report["terminal_outcome"]}
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0 if args.action != "run" or report["terminal_outcome"] in {"completed", "pilot_completed_unqualified"} else 2
    except (Exception, SystemExit, _HostDeadline) as error:
        print(json.dumps({"status": "refused", "failure": failure_document(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
