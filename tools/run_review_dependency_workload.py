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
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evidence_wiki import Workspace
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_journal_contracts import digest_record
from tools._execution_budget import (
    execution_policy as select_execution_policy,
)
from tools._execution_budget import (
    validate_execution_binding,
    validate_execution_window,
    validate_runtime_allocation,
)
from tools._observation_freshness import is_batch_policy, validate_observation_policy, validate_observer_window
from tools._observation_freshness import observation_policy as select_observation_policy
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
PLAN_SCHEMA_CURRENT = "review-dependency-workload-plan/v3"
RESULT_SCHEMA_CURRENT = "review-dependency-workload-observation/v3"
PLAN_SCHEMA_EXECUTION = "review-dependency-workload-plan/v4"
RESULT_SCHEMA_EXECUTION = "review-dependency-workload-observation/v4"
PLAN_SCHEMA_OBSERVATIONAL = "review-dependency-observational-pilot-plan/v1"
RESULT_SCHEMA_OBSERVATIONAL = "review-dependency-observational-pilot-report/v1"
PLAN_SCHEMA_COLLECTION = "review-dependency-workload-collection-plan/v1"
RESULT_SCHEMA_COLLECTION = "review-dependency-workload-collection-observation/v1"
PLAN_SCHEMA_OBSERVATIONAL_COLLECTION = "review-dependency-observational-pilot-plan/v2"
RESULT_SCHEMA_OBSERVATIONAL_COLLECTION = "review-dependency-observational-pilot-report/v2"
from tools import _review_control_ceiling as ceiling_policy

PLAN_SCHEMA_LONG = "review-dependency-observational-pilot-plan/v3"
PLAN_SCHEMA_OWNER = "review-dependency-observational-pilot-plan/v4"
RESULT_SCHEMA_LONG = "review-dependency-observational-pilot-report/v3"
RESULT_SCHEMA_OWNER = "review-dependency-observational-pilot-report/v4"
PLAN_SCHEMA_LONG_FORMAL = "review-dependency-workload-collection-plan/v2"
PLAN_SCHEMA_OWNER_FORMAL = "review-dependency-workload-collection-plan/v3"
RESULT_SCHEMA_LONG_FORMAL = "review-dependency-workload-collection-observation/v2"
RESULT_SCHEMA_OWNER_FORMAL = "review-dependency-workload-collection-observation/v3"
from tools._review_journal_artifacts import POLICY as JOURNAL_COLLECTION_POLICY
from tools._review_journal_artifacts import selected as collection_selected


def report_schema(plan):
    if plan.get("schema_version") in {PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER}:
        return ceiling_policy.version(RESULT_SCHEMA_LONG, plan)
    if plan.get("schema_version") in {PLAN_SCHEMA_LONG_FORMAL, PLAN_SCHEMA_OWNER_FORMAL}:
        return ceiling_policy.version(RESULT_SCHEMA_LONG_FORMAL, plan)
    if collection_selected(plan):
        return RESULT_SCHEMA_OBSERVATIONAL_COLLECTION if "service_policy" in plan else RESULT_SCHEMA_COLLECTION
    return RESULT_SCHEMA_OBSERVATIONAL if "service_policy" in plan else RESULT_SCHEMA_EXECUTION if "execution_policy" in plan else RESULT_SCHEMA_CURRENT if "observation_policy" in plan else RESULT_SCHEMA

ARTIFACTS = ("fresh-fixture-input.json", "pilot-job-inputs.json", "role-claim-journal-inputs.json", "measurement-protocol.json", "adapter-contracts.json", "acceptance-cases.json")
PILOT_CAPS = {"setup": 600, "job": 600, "mutation": 600, "owner_read": 120, "global_close": 120, "cleanup": 60}
BATCH_PILOT_CAPS = dict(PILOT_CAPS, owner_read=600, global_close=600)
HARD_LIMITS = {"each_owner_pass_read_seconds": 120, "job_and_MCP_seconds": 600,
    "native_artifact_payload_bytes": 4194304, "native_collection_nodes": 65536,
    "native_record_collection_bytes": 1048576, "native_record_depth": 16,
    "owner_frame_bytes": 2097152, "owner_raw_report_bytes": 1048576,
    "protected_authority_seconds": 7200, "raw_aggregate_retention_bytes": 67108864,
    "raw_journal_retention_bytes": 16777216, "raw_retention_entries": 4096,
    "raw_retention_files": 256, "raw_retention_seconds": 10, "witness_seconds": 30}


def phase_ceilings(policy=None, execution_policy=None):
    """Select prospective phase ceilings without changing credential lifetimes."""
    if policy is not None:
        policy = validate_observation_policy(policy)
    caps = dict(BATCH_PILOT_CAPS if policy is not None and is_batch_policy(policy) else PILOT_CAPS)
    if execution_policy is not None:
        from tools._execution_budget import validate_execution_policy
        execution_policy = validate_execution_policy(execution_policy)
        if policy is None or not is_batch_policy(policy):
            raise ValueError("execution_observation_profile_invalid")
        if execution_policy["profile"] == "four-hour":
            caps["global_close"] = 900
        elif execution_policy["profile"] == "six-hour":
            caps.update(owner_read=1200, global_close=1500)
        elif execution_policy["schema_version"] == "review-execution-budget-policy/v2":
            caps.update(owner_read=1200, global_close=1500)
    return caps



def service_policy(profile):
    """Closed observational service envelope; native worker limits stay unchanged."""
    if profile == ceiling_policy.OWNER_PROFILE:
        return {**service_policy(ceiling_policy.PROFILE), "schema_version": "review-observational-service-policy/v6", "profile": profile}
    if profile == ceiling_policy.PROFILE:
        return {**service_policy("control-observational"), "schema_version": "review-observational-service-policy/v5",
            "profile": profile, "setup_seconds": 900, "mutation_seconds": 3600}
    if profile in ("extended-observational", "control-observational"):
        return {**({"mutation_seconds": 1200} if profile == "control-observational" else {}),
                "schema_version": "review-observational-service-policy/v4" if profile == "control-observational" else "review-observational-service-policy/v3", "profile": profile,
                "service_seconds": 3600, "native_worker_seconds": 600, "qualification": "none",
                "application_lease": {"schema_version": "research-review-application-lease/v2", "seconds": 3600},
                "execution_profile_schema": "research-execution-profile/v17", "job_schema": "research-host-job/v6"}
    if type(profile) is not str or profile != "observational":
        raise ValueError("workload_service_policy_invalid")
    return {"schema_version": "review-observational-service-policy/v2", "profile": profile,
            "service_seconds": 1800, "native_worker_seconds": 600, "qualification": "none",
            "application_lease": {"schema_version": "research-review-application-lease/v1", "seconds": 1800},
            "execution_profile_schema": "research-execution-profile/v16", "job_schema": "research-host-job/v5"}


def validate_service_policy(policy, *, study, host, case_id, observation, execution):
    if (type(policy) is not dict or (not ceiling_policy.exact(policy, service_policy(policy.get("profile"))) if policy.get("profile") in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE} else policy != service_policy(policy.get("profile")))
            or type(policy.get("service_seconds")) is not int or type(policy.get("native_worker_seconds")) is not int
            or policy.get("profile") == "control-observational" and type(policy.get("mutation_seconds")) is not int
            or study != "pilot" or host != "direct" or case_id != "independent-claim-text"
            or observation is None or not is_batch_policy(observation)
            or execution is None or execution.get("profile") != ("sixteen-hour" if policy.get("profile") in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE} else "eight-hour" if policy.get("profile") in {"extended-observational", "control-observational"} else "six-hour")):
        raise ValueError("workload_service_policy_invalid")
    if policy["profile"] in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE}:
        from tools._execution_budget import validate_execution_policy
        validate_observation_policy(observation)
        validate_execution_policy(execution)
    return policy


def formal_application_policy(seconds):
    """Freeze an explicitly measured service bound independently of workers."""
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError("workload_application_policy_invalid")
    return {"schema_version": "review-formal-application-policy/v1", "service_seconds": seconds,
            "native_worker_seconds": 600,
            "application_lease": {"schema_version": "research-review-application-lease/v2", "seconds": seconds},
            "execution_profile_schema": "research-execution-profile/v17", "job_schema": "research-host-job/v6"}


def validate_formal_application_policy(policy, execution, caps):
    from tools._execution_budget import validate_execution_policy
    if (type(policy) is not dict or policy != formal_application_policy(policy.get("service_seconds"))
            or validate_execution_policy(execution)["schema_version"] != "review-execution-budget-policy/v2"
            or caps.get("job") != policy["service_seconds"]):
        raise ValueError("workload_application_policy_invalid")
    return policy


def formal_phase_ceilings(observation, execution, *, ceiling_binding=None):
    from tools._execution_budget import validate_execution_policy
    if validate_execution_policy(execution)["schema_version"] != "review-execution-budget-policy/v2":
        raise ValueError("workload_application_policy_invalid")
    from tools._execution_budget import validate_execution_scope
    validate_execution_scope(execution, ceiling_binding, observation)
    if ceiling_policy.validate(ceiling_binding):
        if not is_batch_policy(observation):
            raise ValueError("workload_phase_ceiling_policy_invalid")
        return dict(ceiling_policy.validate(ceiling_binding)["phase_ceiling_policy"]["formal_phase_ceilings"])
    return dict(phase_ceilings(observation, execution), job=3600, setup=1200, mutation=1200,
                owner_read=3600, global_close=10800)


def observational_phase_ceilings(policy, observation, execution):
    """Bind discovery caps to the exact closed service policy."""
    selected = validate_service_policy(policy, study="pilot", host="direct", case_id="independent-claim-text",
        observation=observation, execution=execution)
    if selected["profile"] in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE}:
        return dict(ceiling_policy.binding(selected["profile"])["phase_ceiling_policy"]["discovery_phase_caps"])
    caps = phase_ceilings(observation, execution)
    caps["job"] = selected["service_seconds"]
    if selected["profile"] == "control-observational":
        caps["mutation"] = selected["mutation_seconds"]
    return caps


def plan_phase_ceilings(plan):
    prospective = plan.get("schema_version") in {PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER}
    ceiling_policy.validate(plan, required=prospective)
    if (plan.get("service_policy", {}).get("profile") in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE}) != prospective:
        raise ValueError("workload_phase_ceiling_policy_invalid")
    if prospective and (not collection_selected(plan) or plan.get("service_policy", {}).get("profile") not in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE}):
        raise ValueError("workload_phase_ceiling_policy_invalid")
    if "service_policy" in plan:
        validate_service_policy(plan["service_policy"], study=plan["study"], host=plan["host"]["host_id"],
            case_id=plan["case_id"], observation=plan.get("observation_policy"), execution=plan.get("execution_policy"))
        return observational_phase_ceilings(plan["service_policy"], plan.get("observation_policy"), plan.get("execution_policy"))
    return phase_ceilings(plan.get("observation_policy"), plan.get("execution_policy"))


def validate_long_discovery_shape(plan):
    """Validate historical discovery shape without declaring authority current."""
    ceiling_policy.validate(plan, required=True)
    fields = {"schema_version", "candidate_sha256", "artifacts", "host", "case_id", "study", "assignments",
        "phase_caps", "group_overhead_cap_seconds", "elapsed_cap_seconds", "hard_limits", "measured_recipe",
        "control_run_id", "control", "review_journals", "control_journals", "frozen_at", "expires_at", "stdin", "scope",
        "observation_policy", "observation_policy_sha256", "source_protocol_limits", "execution_policy",
        "execution_policy_sha256", "service_policy", "journal_collection_policy", *ceiling_policy.FIELDS}
    caps = plan_phase_ceilings(plan)
    execution = validate_execution_binding(plan.get("execution_policy"), plan)
    validate_execution_window(execution, plan.get("frozen_at"), plan.get("expires_at"))
    expected = {"schema_version": ceiling_policy.version(PLAN_SCHEMA_LONG, plan), "phase_caps": caps, "study": "pilot", "case_id": "independent-claim-text",
        "host": host_identity("direct"), "source_protocol_limits": HARD_LIMITS, "journal_collection_policy": JOURNAL_COLLECTION_POLICY, "group_overhead_cap_seconds": 0,
        "elapsed_cap_seconds": allocation_seconds(caps, 6), "measured_recipe": None,
        "hard_limits": runtime_hard_limits(plan["observation_policy"], execution, phase_caps=caps, ceiling_binding=plan),
        "review_journals": 3, "control_journals": 1, "stdin": "closed",
        "scope": "all_six_claims_two_questions_global_policy_sources_computation",
        "control": "reserved_native_attempt_changed_basis_no_dispatch_no_application"}
    if set(plan) != fields or any(not ceiling_policy.exact(plan[key], value) for key, value in expected.items()):
        raise ValueError("workload_plan_binding_invalid")
    return plan

def runtime_hard_limits(policy, execution_policy=None, *, phase_caps=None, ceiling_binding=None):
    policy = validate_observation_policy(policy)
    selected_ceiling = ceiling_policy.validate(ceiling_binding)
    if execution_policy is not None:
        from tools._execution_budget import validate_execution_scope
        validate_execution_scope(execution_policy, ceiling_binding, policy)
    if selected_ceiling:
        from tools._execution_budget import validate_execution_policy
        if execution_policy is None or validate_execution_policy(execution_policy)["schema_version"] != "review-execution-budget-policy/v2" or not is_batch_policy(policy):
            raise ValueError("workload_phase_ceiling_policy_invalid")
        if "service_policy" in ceiling_binding:
            selected = validate_service_policy(ceiling_binding["service_policy"], study="pilot", host="direct",
                case_id="independent-claim-text", observation=policy, execution=execution_policy)
            if selected["profile"] != selected_ceiling["phase_ceiling_policy"]["profile"] or not ceiling_policy.exact(phase_caps, selected_ceiling["phase_ceiling_policy"]["discovery_phase_caps"]):
                raise ValueError("workload_phase_ceiling_policy_invalid")
    caps = phase_ceilings(policy, execution_policy)
    limits = {key: value for key, value in HARD_LIMITS.items() if key != "witness_seconds"}
    if execution_policy is not None:
        from tools._execution_budget import validate_execution_policy
        limits["protected_authority_seconds"] = validate_execution_policy(execution_policy)["authority_seconds"]
        if execution_policy["schema_version"] == "review-execution-budget-policy/v2":
            ceilings = formal_phase_ceilings(policy, execution_policy, ceiling_binding=ceiling_binding)
            if (type(phase_caps) is not dict or set(phase_caps) != set(ceilings)
                    or any(type(phase_caps[name]) is not int or not 1 <= phase_caps[name] <= ceilings[name] for name in ceilings)):
                raise ValueError("workload_extended_policy_required")
            caps = phase_caps
            del limits["job_and_MCP_seconds"]
            limits.update(native_worker_seconds=600, service_application_seconds=caps["job"],
                          scoped_mcp_application_seconds=caps["job"])
    return dict(limits, each_owner_pass_read_seconds=caps["owner_read"],
        global_close_seconds=caps["global_close"], observer_recency_seconds=policy["max_age_seconds"],
        native_calibration_probe_lifetime_seconds=30)


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
    """Read exact native bytes and explicit collection/artifact metadata only."""
    from tools._review_journal_artifacts import read_collection
    return read_collection(pin)


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


def validate_credential_originals(pins, expires_at):
    """Compare actual signed service outputs with their prospective fixed expiry."""
    values = {}
    for label in ("qualifications", "source-usage"):
        selected = [pin for pin in pins if pin["label"] == label]
        if len(selected) != 1:
            raise ValueError("execution_runtime_binding_invalid")
        values[label], _ = read_pinned_artifact(selected[0])
    qualifications = values["qualifications"]
    if (type(qualifications) is not list or len(qualifications) != 2
            or any(record.get("expires_utc") != expires_at for record in qualifications)):
        raise ValueError("execution_runtime_binding_invalid")
    receipts = values["source-usage"]
    if type(receipts) is not list or not receipts:
        raise ValueError("execution_runtime_binding_invalid")
    for receipt in receipts:
        command = receipt["command"]
        if command["authentication"]["expires_at"] != expires_at:
            raise ValueError("execution_runtime_binding_invalid")
        body = command["payload"]["body"]
        if command["payload"]["action"] == "deposit":
            if (body["grant"]["authentication"]["expires_at"] != expires_at
                    or body["grant"]["payload"]["expires_at"] != expires_at
                    or body["scrub"]["authentication"]["expires_at"] != expires_at):
                raise ValueError("execution_runtime_binding_invalid")


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
    return _completed_pilot_measurements(measured, candidate_sha256, expected_mode="legacy")


def _completed_pilot_measurements(measured, candidate_sha256, *, expected_mode):
    """Budget from exact admitted pilot originals; historical costs grant no review."""
    if type(measured) is not dict or set(measured) != {"pilot_report_pin", "pilot_plan_pin", "phase_caps", "observed_worst_seconds", "group_overhead_cap_seconds", "observed_group_overhead_seconds"}:
        raise ValueError("workload_pilot_measurement_invalid")
    report, _ = read_pinned_artifact(measured["pilot_report_pin"])
    prior_plan, _ = read_pinned_artifact(measured["pilot_plan_pin"])
    if (prior_plan.get("schema_version") in {PLAN_SCHEMA_OBSERVATIONAL, PLAN_SCHEMA_OBSERVATIONAL_COLLECTION, PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER}) != ("service_policy" in prior_plan):
        raise ValueError("workload_pilot_measurement_invalid")
    execution = validate_execution_binding(prior_plan.get("execution_policy"), prior_plan) if "execution_policy" in prior_plan else None
    policy = prior_plan.get("observation_policy") if "observation_policy" in prior_plan else None
    profile = policy["profile"] if policy is not None else None
    ceilings = plan_phase_ceilings(prior_plan)
    if "service_policy" in prior_plan and expected_mode != "selective":
        raise ValueError("workload_pilot_measurement_invalid")
    expected_prior = reference_plan(Path(prior_plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent, observation_profile=profile,
        execution_profile=execution["profile"] if execution is not None else None,
        service_profile=prior_plan["service_policy"]["profile"] if "service_policy" in prior_plan else None,
        journal_collection=collection_selected(prior_plan))
    if prior_plan.get("schema_version") in {PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER}:
        validate_long_discovery_shape(prior_plan)
        if any(not ceiling_policy.exact(prior_plan[key], expected_prior[key]) for key in expected_prior if key not in {"frozen_at", "expires_at"}):
            raise ValueError("workload_pilot_measurement_invalid")
        for key in ("service_policy", "execution_policy", "observation_policy", "hard_limits", "source_protocol_limits", "journal_collection_policy"):
            if not ceiling_policy.exact(report.get(key), prior_plan.get(key)):
                raise ValueError("workload_pilot_measurement_invalid")
    if set(prior_plan) != set(expected_prior) or any(prior_plan[key] != expected_prior[key] for key in expected_prior if key not in {"frozen_at", "expires_at"}):
        raise ValueError("workload_pilot_measurement_invalid")
    if report.get("journal_collection_policy") != prior_plan.get("journal_collection_policy"):
        raise ValueError("workload_pilot_measurement_invalid")
    if report.get("service_policy") != prior_plan.get("service_policy"):
        raise ValueError("workload_pilot_measurement_invalid")
    if (report.get("terminal_outcome") != "pilot_completed_unqualified" or report.get("study") != "pilot"
            or report.get("candidate_sha256") != candidate_sha256 or prior_plan.get("candidate_sha256") != candidate_sha256
            or digest(prior_plan) != report.get("plan_sha256") or prior_plan.get("schema_version") not in {PLAN_SCHEMA, PLAN_SCHEMA_CURRENT, PLAN_SCHEMA_EXECUTION, PLAN_SCHEMA_OBSERVATIONAL, PLAN_SCHEMA_COLLECTION, PLAN_SCHEMA_OBSERVATIONAL_COLLECTION, PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER, PLAN_SCHEMA_LONG_FORMAL, PLAN_SCHEMA_OWNER_FORMAL}
            or prior_plan.get("study") != "pilot" or prior_plan.get("phase_caps") != ceilings
            or prior_plan.get("elapsed_cap_seconds") != allocation_seconds(ceilings, 6) or prior_plan.get("group_overhead_cap_seconds") != 0
            or report.get("applied_jobs") != 6 or report.get("pair_qualified") is not False
            or len(report.get("assignments", [])) != 6 or len(report.get("job_elapsed", [])) != 6):
        raise ValueError("workload_pilot_measurement_invalid")
    if not ceiling_policy.exact(ceiling_policy.validate(report, required=prior_plan.get("schema_version") in {PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER}), ceiling_policy.validate(prior_plan)):
        raise ValueError("workload_pilot_measurement_invalid")
    if (report.get("schema_version") != report_schema(prior_plan) or report.get("failure") is not None
            or report.get("mode") != expected_mode or report.get("host_id") != "direct" or report.get("case_id") != "independent-claim-text"
            or report.get("automatic_canonical_completion") is not False or report.get("cleanup", {}).get("status") != "confirmed"
            or report.get("child_cleanup", {}).get("status") != "confirmed" or report.get("retention", {}).get("status") != "complete"
            or type(report.get("elapsed_seconds")) not in (int, float) or not math.isfinite(report["elapsed_seconds"])
            or not 0 < report["elapsed_seconds"] <= prior_plan["elapsed_cap_seconds"] or len(report.get("owner_reads", [])) != 2
            or report.get("control", {}).get("native_error_code") != "RESEARCH_BASIS_CHANGED"
            or report["control"].get("applied") is not False or report.get("control_accounting") != "reserved_unstarted_no_review_credit"
            or report.get("cross_journal_compatibility") != "authenticated_partial_scope"):
        raise ValueError("workload_pilot_measurement_invalid")
    from tools._review_journal_artifacts import RECEIPT as COLLECTION_RECEIPT
    from tools._review_journal_artifacts import verify_receipt
    receipt_rounds = [report.get("journal_receipts", [])] + [value.get("journal_receipts", []) for value in report["owner_reads"]]
    if report.get("closing_current_results"):
        receipt_rounds.append(report["closing_current_results"].get("journal_receipts", []))
    for receipt in (item for rows in receipt_rounds for item in rows):
        if (receipt.get("schema_version") == COLLECTION_RECEIPT) != collection_selected(prior_plan):
            raise ValueError("workload_pilot_measurement_invalid")
        verify_receipt(receipt, original_pins=report["original_response_pins"])
    first, second = report["owner_reads"]
    current_gate = report["current_gate_results"]
    gate_bound = (type(current_gate) is dict and set(current_gate) == {"owner_read_index"}
        and type(current_gate["owner_read_index"]) is int and current_gate["owner_read_index"] == 1) if policy is not None else current_gate == second
    if owner_read_identity(first) != owner_read_identity(second) or not gate_bound:
        raise ValueError("workload_pilot_measurement_invalid")
    closing = datetime.fromisoformat(report["global_closing_utc"])
    if closing.tzinfo is None:
        raise ValueError("workload_pilot_measurement_invalid")
    if execution is not None:
        validate_execution_window(execution, prior_plan["frozen_at"], prior_plan["expires_at"], instant=closing)
        if report.get("execution_policy") != execution or report.get("execution_policy_sha256") != digest(execution):
            raise ValueError("execution_policy_binding_invalid")
    if policy is not None:
        if report.get("observation_policy") != policy or report.get("observation_policy_sha256") != digest(policy):
            raise ValueError("workload_pilot_measurement_invalid")
        allocation_pins = [pin for pin in report["original_response_pins"] if pin["label"] == "runtime-allocation"]
        if len(allocation_pins) != 1:
            raise ValueError("workload_pilot_measurement_invalid")
        allocation, _ = read_pinned_artifact(allocation_pins[0])
        if execution is not None:
            validate_runtime_allocation(allocation, prior_plan, closing=closing)
            validate_credential_originals(report["original_response_pins"], prior_plan["expires_at"])
        binding = allocation["allocation_binding"]
        if (binding.get("plan_sha256") != digest(prior_plan)
                or binding.get("candidate_sha256") != candidate_sha256
                or binding.get("phase_caps") != prior_plan["phase_caps"]
                or binding.get("observation_policy") != policy
                or binding.get("observation_policy_sha256") != digest(policy)
                or binding.get("hard_limits") != prior_plan["hard_limits"]
                or binding.get("source_protocol_limits") != HARD_LIMITS
                or report.get("hard_limits") != prior_plan["hard_limits"]
                or report.get("source_protocol_limits") != HARD_LIMITS
                or not datetime.fromisoformat(prior_plan["frozen_at"]) <= closing < datetime.fromisoformat(prior_plan["expires_at"])
                or not datetime.fromisoformat(allocation["authority_start"]) <= closing < datetime.fromisoformat(allocation["authority_end"])):
            raise ValueError("workload_pilot_measurement_invalid")
        rounds = [first, second] + ([report["closing_current_results"]] if is_batch_policy(policy) and report.get("closing_current_results") else [])
        labels = {"owner-strict-current": [value["strict"] for value in rounds],
            "owner-publication-current": [value["publication"] for value in rounds]}
        for field, suffix in (("native_current", "-current"), ("native_host_witnesses", "-native-host-qualification")):
            for observed_round in rounds:
                for value in observed_round[field]:
                    labels.setdefault(value["run_id"] + suffix, []).append(value)
        for label, values in labels.items():
            originals = [pin for pin in report["original_response_pins"] if pin["label"] == label]
            if len(originals) != len(values) or any(read_pinned_artifact(pin)[0] != value for pin, value in zip(originals, values, strict=True)):
                raise ValueError("workload_pilot_measurement_invalid")
        if is_batch_policy(policy):
            current = report.get("closing_current_results")
            if not current or owner_read_identity(current) != owner_read_identity(second):
                raise ValueError("workload_pilot_measurement_invalid")
            validate_observer_window(current, closing, policy)
            if (not report.get("closing_current_snapshots") or not report.get("closing_current_source_manifest")
                    or not report.get("closing_current_original_pins")
                    or type(report.get("closing_current_elapsed_seconds")) not in (int, float)
                    or not 0 < report["closing_current_elapsed_seconds"] <= ceilings["owner_read"]):
                raise ValueError("workload_pilot_measurement_invalid")
            pins = report["closing_current_original_pins"]
            for pin in pins:
                if pin not in report["original_response_pins"]:
                    raise ValueError("workload_pilot_measurement_invalid")
                if pin["label"].endswith("-authenticated") or "-authenticated-page-" in pin["label"]:
                    value, metadata = read_pinned_native_records(pin)
                    if metadata["artifact_bytes"] != pin["size_bytes"]:
                        raise ValueError("workload_pilot_measurement_invalid")
                    for page in metadata["page_pins"]:
                        if not any(all(source.get(key) == page[key] for key in ("path", "raw_sha256", "size_bytes")) for source in pins):
                            raise ValueError("workload_pilot_measurement_invalid")
                    continue
                else:
                    value, raw = read_pinned_artifact(pin)
                if len(raw) != pin["size_bytes"]:
                    raise ValueError("workload_pilot_measurement_invalid")
            expected_labels = {"owner-strict-current": current["strict"], "owner-publication-current": current["publication"]}
            for view in current["native_current"]:
                expected_labels[view["run_id"] + "-current"] = view
            for witness in current["native_host_witnesses"]:
                expected_labels[witness["run_id"] + "-native-host-qualification"] = witness
            for label, value in expected_labels.items():
                matches = [pin for pin in pins if pin["label"] == label]
                if len(matches) != 1 or read_pinned_artifact(matches[0])[0] != value:
                    raise ValueError("workload_pilot_measurement_invalid")
            for label, value in (("closing-current-snapshots", report["closing_current_snapshots"]),
                    ("closing-current-source-manifest", report["closing_current_source_manifest"])):
                matches = [pin for pin in report["original_response_pins"] if pin["label"] == label]
                if len(matches) != 1 or read_pinned_artifact(matches[0])[0] != value:
                    raise ValueError("workload_pilot_measurement_invalid")
    for observed in (first, second):
        if policy is not None:
            validate_observer_window(observed, closing, policy)
            continue
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
        if (row["status"] != "applied" or row["native_passed"] is not True or row["cap_seconds"] != ceilings["job"]
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
        from tools._review_journal_artifacts import verify_receipt
        records, metadata = verify_receipt(receipt, original_pins=original_pins)
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
        expected_cap = 2 * ceilings["job"] if label in groups else ceilings["owner_read" if label.startswith("owner-read-") else label.replace("-", "_")]
        if (phase["cap_seconds"] != expected_cap or type(phase["elapsed_seconds"]) not in (int, float)
                or not math.isfinite(phase["elapsed_seconds"]) or not 0 < phase["elapsed_seconds"] <= expected_cap):
            raise ValueError("workload_pilot_measurement_invalid")
    worst = {name: phases[name.replace("_", "-")]["elapsed_seconds"] for name in ("setup", "mutation", "cleanup", "global_close")}
    worst["owner_read"] = max(phases["owner-read-0"]["elapsed_seconds"], phases["owner-read-1"]["elapsed_seconds"], report.get("closing_current_elapsed_seconds", 0) if expected_mode == "selective" else 0)
    worst["job"] = max(row["elapsed_seconds"] for row in timings.values())
    overheads = []
    for key, group in groups.items():
        job_seconds = sum(timings[(row["native_run_id"], row["claim_id"], row["config_ref"])]["elapsed_seconds"] for row in group)
        overhead = phases[key]["elapsed_seconds"] - job_seconds
        if overhead < 0:
            raise ValueError("workload_pilot_measurement_invalid")
        overheads.append(overhead)
    return worst, max(overheads)


def reference_plan(bundle, *, study="pilot", host="direct", case_id="independent-claim-text", measured_recipe=None, observation_profile=None, execution_profile=None, measurement_reader=None, service_profile=None, journal_collection=False):
    """Freeze exact candidate, assignments and prospective phase caps before work."""
    if type(journal_collection) is not bool:
        raise ValueError("workload_journal_collection_policy_invalid")
    if measurement_reader not in {None, "selective-complete-pilot-measurement/v1", "selective-complete-pilot-measurement/v2", "selective-complete-pilot-measurement/v3", "selective-complete-pilot-measurement/v4", "selective-complete-pilot-measurement/v5"}:
        raise ValueError("workload_measurement_reader_invalid")
    prospective = service_profile in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE} or measurement_reader in {"selective-complete-pilot-measurement/v4", "selective-complete-pilot-measurement/v5"}
    policy_binding = ceiling_policy.binding(ceiling_policy.OWNER_PROFILE if service_profile == ceiling_policy.OWNER_PROFILE or measurement_reader == "selective-complete-pilot-measurement/v5" else ceiling_policy.PROFILE) if prospective else {}
    if prospective and not journal_collection:
        raise ValueError("workload_journal_collection_policy_invalid")
    if measurement_reader is not None and study != "full":
        raise ValueError("workload_measurement_reader_invalid")
    bundle = Path(bundle).absolute()
    if bundle.resolve(strict=True) != bundle or study not in {"pilot", "full"}:
        raise ValueError("workload_bundle_not_canonical")
    pins = _inputs(bundle)
    artifacts = {name: read_pinned_artifact(pin)[0] for name, pin in pins.items()}
    assignments, case = validate_inputs(artifacts, study, host, case_id)
    policy = select_observation_policy(observation_profile) if observation_profile is not None else None
    execution = select_execution_policy(execution_profile) if execution_profile is not None else None
    if execution is not None and (policy is None or not is_batch_policy(policy)):
        raise ValueError("execution_observation_profile_invalid")
    authority_seconds = 7200 if execution is None else execution["authority_seconds"]
    ceilings = phase_ceilings(policy, execution)
    selected_service = None
    if service_profile is not None:
        selected_service = validate_service_policy(service_policy(service_profile), study=study, host=host,
            case_id=case_id, observation=policy, execution=execution)
        ceilings = observational_phase_ceilings(selected_service, policy, execution)
    extended_formal = measurement_reader in {"selective-complete-pilot-measurement/v2", "selective-complete-pilot-measurement/v3", "selective-complete-pilot-measurement/v4", "selective-complete-pilot-measurement/v5"}
    if execution is not None and execution["schema_version"] == "review-execution-budget-policy/v2":
        if not extended_formal and (selected_service is None or selected_service["profile"] not in {"extended-observational", "control-observational", ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE}):
            raise ValueError("workload_extended_policy_required")
    if extended_formal:
        if selected_service is not None:
            raise ValueError("workload_extended_policy_required")
        ceilings = formal_phase_ceilings(policy, execution, ceiling_binding=policy_binding)
    caps = dict(ceilings)
    group_overhead_cap = 0
    recipe_pin = None
    if study == "full":
        if measured_recipe is None:
            raise ValueError("workload_measured_allocation_required")
        path = Path(measured_recipe).absolute()
        recipe_pin = {"path": str(path), "raw_sha256": digest_bytes(path.read_bytes())}
        measured, _ = read_pinned_artifact(recipe_pin)
        ceiling_policy.validate(measured, required=prospective)
        if measured.get("schema_version") != measurement_reader and prospective:
            raise ValueError("workload_pilot_policy_mismatch")
        if collection_selected(measured) != journal_collection:
            raise ValueError("workload_pilot_policy_mismatch")
        caps = measured["phase_caps"]
        reader = completed_pilot_measurements
        if measurement_reader is not None:
            from tools._review_dependency_pilot import completed_pilot_measurements as reader
        if extended_formal:
            from tools._review_dependency_pilot import completed_extended_pilot_measurements as reader
        worst, observed_overhead = reader(measured, digest(candidate_manifest(ROOT)))
        prior_pilot, _ = read_pinned_artifact(measured["pilot_plan_pin"])
        if measurement_reader is not None:
            prior_pilot = prior_pilot["native_plan"]
        if prior_pilot.get("observation_policy") != policy:
            raise ValueError("workload_pilot_policy_mismatch")
        if (measured.get("formal_execution_policy") if extended_formal else prior_pilot.get("execution_policy")) != execution:
            raise ValueError("workload_pilot_policy_mismatch")
        if extended_formal:
            validate_formal_application_policy(measured["formal_application_policy"], execution, caps)
        group_overhead_cap = measured["group_overhead_cap_seconds"]
        if (measured["observed_worst_seconds"] != worst or measured["observed_group_overhead_seconds"] != observed_overhead
                or type(group_overhead_cap) is not int or not math.ceil(observed_overhead * 1.25) <= group_overhead_cap <= authority_seconds):
            raise ValueError("workload_measured_allocation_required")
        if (set(caps) != set(PILOT_CAPS) or set(worst) != set(PILOT_CAPS)
                or any(type(caps[key]) is not int or not 0 < caps[key] <= ceilings[key]
                    or type(worst[key]) not in (int, float) or not math.ceil(worst[key] * 1.25) <= caps[key] for key in caps)):
            raise ValueError("workload_measured_allocation_required")
    outer = allocation_seconds(caps, len(assignments)) + (2 * caps["global_close"] if study == "full" else 0) + len(assignment_groups(assignments)) * group_overhead_cap
    if outer > authority_seconds or study == "pilot" and outer != allocation_seconds(ceilings, 6):
        raise ValueError("workload_allocation_exceeds_authority")
    instant = datetime.now(timezone.utc)
    plan = {"schema_version": PLAN_SCHEMA_OBSERVATIONAL if selected_service is not None else PLAN_SCHEMA_EXECUTION if execution is not None else PLAN_SCHEMA_CURRENT if policy is not None else PLAN_SCHEMA, "candidate_sha256": digest(candidate_manifest(ROOT)), "artifacts": pins,
        "host": host_identity(host), "case_id": case_id, "study": study, "assignments": assignments,
        "phase_caps": caps, "group_overhead_cap_seconds": group_overhead_cap, "elapsed_cap_seconds": outer, "hard_limits": HARD_LIMITS, "measured_recipe": recipe_pin,
        "control_run_id": "control-" + digest_bytes((host + "/" + case_id).encode())[:24],
        "control": "reserved_native_attempt_changed_basis_no_dispatch_no_application",
        "review_journals": len(assignment_groups(assignments)), "control_journals": 1,
        "frozen_at": instant.isoformat(), "expires_at": (instant + timedelta(seconds=authority_seconds)).isoformat(),
        "stdin": "closed", "scope": "all_six_claims_two_questions_global_policy_sources_computation"}
    if policy is not None:
        plan.update(observation_policy=policy, observation_policy_sha256=digest(policy),
            source_protocol_limits=HARD_LIMITS, hard_limits=runtime_hard_limits(policy, execution, phase_caps=caps, ceiling_binding={**policy_binding, **({"service_policy": selected_service} if prospective and selected_service is not None else {})}))
    if execution is not None:
        plan.update(execution_policy=execution, execution_policy_sha256=digest(execution))
    if selected_service is not None:
        plan["service_policy"] = selected_service
    if journal_collection:
        plan.update(schema_version=PLAN_SCHEMA_OBSERVATIONAL_COLLECTION if selected_service is not None else PLAN_SCHEMA_COLLECTION,
            journal_collection_policy=dict(JOURNAL_COLLECTION_POLICY))
    if prospective:
        plan.update(policy_binding, schema_version=ceiling_policy.version(PLAN_SCHEMA_LONG if selected_service is not None else PLAN_SCHEMA_LONG_FORMAL, policy_binding))
    if extended_formal:
        plan["formal_application_policy"] = measured["formal_application_policy"]
        plan["measurement_reader"] = measurement_reader
    return plan


def validate_plan(plan, expected_plan):
    if type(plan) is not dict or plan.get("schema_version") not in {PLAN_SCHEMA, PLAN_SCHEMA_CURRENT, PLAN_SCHEMA_EXECUTION, PLAN_SCHEMA_OBSERVATIONAL, PLAN_SCHEMA_COLLECTION, PLAN_SCHEMA_OBSERVATIONAL_COLLECTION, PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER, PLAN_SCHEMA_LONG_FORMAL, PLAN_SCHEMA_OWNER_FORMAL} or digest(plan) != expected_plan:
        raise ValueError("workload_plan_binding_invalid")
    ceiling_policy.validate(plan, required=plan["schema_version"] in {PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER, PLAN_SCHEMA_LONG_FORMAL, PLAN_SCHEMA_OWNER_FORMAL})
    if (plan["schema_version"] in {PLAN_SCHEMA_OBSERVATIONAL, PLAN_SCHEMA_OBSERVATIONAL_COLLECTION, PLAN_SCHEMA_LONG, PLAN_SCHEMA_OWNER}) != ("service_policy" in plan):
        raise ValueError("workload_plan_binding_invalid")
    execution = validate_execution_binding(plan.get("execution_policy"), plan) if "execution_policy" in plan else None
    expected = reference_plan(Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent,
        study=plan["study"], host=plan["host"]["host_id"], case_id=plan["case_id"],
        measured_recipe=plan["measured_recipe"]["path"] if plan["measured_recipe"] else None,
        measurement_reader=plan.get("measurement_reader"),
        observation_profile=plan["observation_policy"]["profile"] if "observation_policy" in plan else None,
        execution_profile=execution["profile"] if execution is not None else None,
        service_profile=plan["service_policy"]["profile"] if "service_policy" in plan else None,
        journal_collection=collection_selected(plan))
    if set(plan) != set(expected) or any(not ceiling_policy.exact(plan[key], expected[key]) if ceiling_policy.validate(plan) else plan[key] != expected[key] for key in expected if key not in {"frozen_at", "expires_at"}):
        raise ValueError("workload_candidate_or_recipe_changed")
    frozen, expires = (datetime.fromisoformat(plan[key]) for key in ("frozen_at", "expires_at"))
    if frozen.tzinfo is None or expires.tzinfo is None or not frozen <= datetime.now(timezone.utc) < expires or expires - frozen != timedelta(seconds=7200 if execution is None else execution["authority_seconds"]):
        raise ValueError("workload_plan_expired_or_clock_invalid")
    return plan


def preflight(plan, expected_plan):
    validate_plan(plan, expected_plan)
    return {"status": "prepared_runtime_unqualified", "plan_sha256": expected_plan,
        "review_jobs": len(plan["assignments"]), "review_journals": plan["review_journals"],
        "control_journals": 1, "allocation_seconds": plan["elapsed_cap_seconds"],
        "cross_journal_compatibility": "requires_fresh_native_global_owner_reads",
        "qualification": False}


def _owner_read(root, runtime, run_ids, originals, observation_policy=None, *, selective_run_id=None):
    if observation_policy is not None:
        validate_observation_policy(observation_policy)
    snapshots, receipts, records = [], [], {}
    # Authenticate complete histories before obtaining any short current witness.
    for run_id in run_ids:
        rows, receipt, snapshot, _ = authenticated_journal(root, runtime, run_id, originals)
        records[run_id] = rows
        receipts.append(receipt)
        snapshots.append(snapshot)
    with runtime.context.activate(), Workspace.open(root) as workspace:
        with ExitStack() as stack:
            if selective_run_id is not None:
                from evidence_wiki._review_dependency_inputs import bind_session
                selected_session = stack.enter_context(workspace.research.session(runtime.policy, run_id=selective_run_id, execution=runtime.execution))
                stack.enter_context(bind_session(selected_session))
            strict = workspace.strict.check()
            originals.append(("owner-strict-current", strict))
            publication = workspace.strict.export()
            originals.append(("owner-publication-current", publication))
        current = []
        witness_expiries = []
        native_witnesses = []
        for run_id in run_ids:
            with workspace.research.session(runtime.policy, run_id=run_id, execution=runtime.execution) as session, ExitStack() as stack:
                if selective_run_id is not None:
                    stack.enter_context(bind_session(session))
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
                witness_expiries.append(datetime.fromisoformat(witness["observed_utc"]) + timedelta(
                    seconds=30 if observation_policy is None else observation_policy["max_age_seconds"]))
                current.append(view)
    observed = {"strict": strict, "publication": publication, "journal_receipts": receipts,
        "native_host_witnesses": native_witnesses, "native_current": current,
        "observer_recency_not_after": min(witness_expiries).isoformat(),
        "witness_semantics": "observer_recency_bound_not_native_calibration_witness",
        "native_calibration_witness": "unavailable_legacy_qualification_profile"}
    if observation_policy is not None:
        observed.update(observation_policy=observation_policy, observation_policy_sha256=digest(observation_policy))
    return observed, snapshots, records



def owner_read_identity(observed):
    """Compare source/head/basis and whole semantic owner outputs across rounds."""
    semantic = {}
    for name in ("strict", "publication"):
        semantic[name] = {key: value for key, value in observed[name].items() if key != "evaluated_at"}
    semantic["journals"] = [{key: row[key] for key in (("run_id", "records_sha256", "records_bytes", "record_count", "head_ref", "source_sha256", "state_sha256")
        if row.get("schema_version") == "review-journal-collection-owner-receipt/v1" else ("run_id", "records_sha256", "head_ref", "source_sha256", "state_sha256"))}
        for row in observed["journal_receipts"]]
    if "observation_policy" in observed:
        semantic["observation_policy"] = validate_observation_policy(observed["observation_policy"])
        semantic["native_current"] = copy.deepcopy(observed["native_current"])
        for current in semantic["native_current"]:
            current.pop("observed_utc", None)
            current.get("canonical_release", {}).get("publication", {}).pop("evaluated_at", None)
            current.get("usage", {}).pop("seconds", None)
        semantic["native_host_witnesses"] = copy.deepcopy(observed["native_host_witnesses"])
        for witness in semantic["native_host_witnesses"]:
            witness.pop("observed_utc", None)
            witness.get("credentials", {}).pop("observed_at", None)
            # Each live credential authentication signs a fresh nonce. Keep
            # all credential identities and verified states, not its nonce.
            for service in witness.get("credentials", {}).get("services", []):
                service.pop("challenge_ref", None)
            # Historical dispatch leases expire independently of retained
            # signed review validity, which the actual native owner checks.
            for attempt in witness.get("attempts", []):
                attempt.pop("lease_state", None)
    return digest(semantic)


def run_workload(plan, expected_plan, host, case_id, mode, directory, *, _selective_pilot=False):
    validate_plan(plan, expected_plan)
    if host != plan["host"]["host_id"] or case_id != plan["case_id"] or mode not in {"legacy", "selective"}:
        raise ValueError("workload_selection_invalid")
    if "service_policy" in plan and not _selective_pilot:
        raise ValueError("workload_observational_pilot_only")
    if _selective_pilot and (mode != "selective" or plan["study"] != "pilot" or (plan.get("observation_policy") is None or not is_batch_policy(plan["observation_policy"]))):
        raise ValueError("workload_selective_pilot_invalid")
    directory = Path(directory).absolute()
    if directory.resolve() != directory or directory.exists() or directory.is_relative_to(ROOT):
        raise ValueError("workload_new_private_external_directory_required")
    directory.mkdir(mode=0o700)
    originals = OriginalResponses(directory / "observations", journal_collection=collection_selected(plan))
    rows = copy.deepcopy(plan["assignments"])
    policy = plan.get("observation_policy")
    execution_policy = plan.get("execution_policy")
    batch = policy is not None and is_batch_policy(policy)
    report = {"schema_version": report_schema(plan), "candidate_sha256": plan["candidate_sha256"], "plan_sha256": expected_plan,
        "host_id": host, "case_id": case_id, "study": plan["study"], "mode": mode, "terminal_outcome": "started_stop_unknown",
        "failure": None, "assignments": rows, "applied_jobs": None, "pair_qualified": False,
        "automatic_canonical_completion": False, "cross_journal_compatibility": "unobserved", "phase_elapsed": [],
        "job_elapsed": [unstarted_job_timing(row, plan["phase_caps"]["job"]) for row in rows],
        "journal_receipts": [], "original_response_pins": [], "native_journal_pins": [],
        "owner_reads": [], "global_closing_utc": None, "cleanup": None, "control": None,
        "current_gate_results": None, "strict_accepted_claim_ids": [], "question_accepted_ids": [], "work_attribution": [],
        "reference_dependency_changes": None, "control_accounting": "reserved_unstarted_no_review_credit",
        "retention": {"status": "unobserved"}, "elapsed_seconds": None}
    report.update(ceiling_policy.validate(plan))
    if "service_policy" in plan:
        report["service_policy"] = plan["service_policy"]
    if policy is not None:
        report.update(observation_policy=policy, observation_policy_sha256=digest(policy),
            hard_limits=plan["hard_limits"], source_protocol_limits=plan["source_protocol_limits"],
            closing_current_results=None, closing_current_snapshots=[], closing_current_source_manifest=None,
            closing_current_original_pins=[])
    if execution_policy is not None:
        report.update(execution_policy=execution_policy, execution_policy_sha256=digest(execution_policy))
    root = runtime = None
    if collection_selected(plan):
        report["journal_collection_policy"] = dict(JOURNAL_COLLECTION_POLICY)
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
        failure = failure_document(error)
        if report["failure"] is None:
            report["failure"] = failure
        else:
            originals.append(("secondary-failure", failure))
        report["terminal_outcome"] = "unavailable" if mode == "selective" else "failed"
        report["pair_qualified"] = False
    tracker = OwnedChildren(originals, expected_plan)
    tracker.open()
    try:
        if mode == "selective" and not _selective_pilot:
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
                    review_mode="selective_reuse" if _selective_pilot else None,
                    mcp=host == "scoped_mcp", children=tracker, run_seconds=plan["elapsed_cap_seconds"], allocation_binding={"plan_sha256": expected_plan,
                        "candidate_sha256": plan["candidate_sha256"], "assignments": rows, "phase_caps": plan["phase_caps"],
                        **ceiling_policy.validate(plan),
                        **({"journal_collection_policy": plan["journal_collection_policy"]} if collection_selected(plan) else {}),
                        "group_overhead_cap_seconds": plan["group_overhead_cap_seconds"], "control_run_id": plan["control_run_id"],
                        **({"service_policy": plan["service_policy"], "service_selection": {"study": plan["study"], "host": host, "case_id": case_id, "mode": mode}} if "service_policy" in plan else {}),
                        **({"observation_policy": policy, "observation_policy_sha256": digest(policy),
                            "hard_limits": plan["hard_limits"], "source_protocol_limits": plan["source_protocol_limits"]}
                            if policy is not None else {}),
                        **({"execution_policy": execution_policy, "execution_policy_sha256": digest(execution_policy),
                            "authority_frozen_at": plan["frozen_at"], "authority_expires_at": plan["expires_at"]}
                            if execution_policy is not None else {})},
                    **({"execution_policy": execution_policy} if execution_policy is not None else {}))
                if execution_policy is not None:
                    allocation_pins = [pin for pin in originals.pins if pin["label"] == "runtime-allocation"]
                    if len(allocation_pins) != 1:
                        raise ValueError("workload_artifact_binding_invalid")
                    allocation, _ = read_pinned_artifact(allocation_pins[0])
                    validate_runtime_allocation(allocation, plan)
                retain(directory / "realized-input-manifest.json", file_manifest(root))
                retain(directory / "native-id-translation.json", {name: {key: row[key] for key in ("source_id", "normalized_path", "revision")} for name, row in source_rows.items()})
                dependencies = reference_dependencies(root, runtime)
                originals.append(("baseline-reference-dependencies", dependencies))
                return fixture_input, selected_case, source_rows, signing_keys, admitted_claims, dependencies
            fixture, case, sources, keys, claims, baseline_dependencies = phase("setup", plan["phase_caps"]["setup"], setup)
            tracker.confirm_completed()
            groups = assignment_groups(rows)
            for cycle in ("baseline", "first-change"):
                if cycle == "first-change":
                    opened_runs.append(plan["control_run_id"])
                    def change():
                        dispatch_before = sorted(runtime.execution.executor._attempts) if _selective_pilot else None
                        control = changed_basis_control(root, runtime, plan["control_run_id"], originals,
                            lambda: mutation(root, fixture, case, sources, keys, runtime, originals))
                        if _selective_pilot:
                            dispatch_after = sorted(runtime.execution.executor._attempts)
                            if dispatch_before != dispatch_after:
                                raise ValueError("workload_control_dispatched")
                            control["dispatch_original_pin"] = originals.append(("selective-control-no-dispatch", {
                                "attempt_id": control["attempt_id"], "before": dispatch_before, "after": dispatch_after}))
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
                    def review_group(run_id=run_id, claim_id=assignments[0]["claim_id"], assignments=assignments):
                        driver = drive
                        selection = {"claim_ids": {claim_id}}
                        if _selective_pilot:
                            from tools._review_dependency_pilot import service_obligations
                            from tools._review_dependency_workload import drive_selective_services
                            driver = drive_selective_services
                            selection = {"obligations": service_obligations(assignments)}
                        driver(root, runtime, host, run_id, originals, case_id=case_id, **selection,
                            job_cap_seconds=plan["phase_caps"]["job"], job_elapsed=report["job_elapsed"], children=tracker)
                        tracker.confirm_completed()
                        return authenticated_journal(root, runtime, run_id, originals)
                    records, receipt, _, _ = phase(key, 2 * plan["phase_caps"]["job"] + plan["group_overhead_cap_seconds"], review_group)
                    tracker.confirm_completed()
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
                        return _owner_read(root, runtime, cycle_runs, originals, observation_policy=policy)
                    observed, _, by_run = phase(cycle + "-global", plan["phase_caps"]["global_close"], resolve_global)
                    tracker.confirm_completed()
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
                    lambda: _owner_read(root, runtime, [*review_runs, plan["control_run_id"]], originals, **({"observation_policy": policy} if policy is not None else {}),
                        **({"selective_run_id": review_runs[-1]} if _selective_pilot else {})))
                tracker.confirm_completed()
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
                if policy is not None:
                    if batch:
                        if file_manifest(root) != report["closing_current_source_manifest"]:
                            raise ValueError("workload_closing_source_changed")
                        for pin in originals.pins:
                            if digest_bytes(Path(pin["path"]).read_bytes()) != pin["raw_sha256"]:
                                raise ValueError("workload_artifact_binding_invalid")
                        for pin in report["native_journal_pins"]:
                            raw = Path(pin["original_path"]).read_bytes()
                            if digest_bytes(raw) != pin["raw_sha256"]:
                                raise ValueError("workload_closing_source_changed")
                        allocation_pins = [pin for pin in originals.pins if pin["label"] == "runtime-allocation"]
                        if len(allocation_pins) != 1:
                            raise ValueError("workload_artifact_binding_invalid")
                        allocation, _ = read_pinned_artifact(allocation_pins[0])
                        if execution_policy is not None:
                            validate_runtime_allocation(allocation, plan)
                            validate_credential_originals(originals.pins, plan["expires_at"])
                instant = datetime.now(timezone.utc)
                if execution_policy is not None:
                    validate_runtime_allocation(allocation, plan, closing=instant)
                if policy is not None:
                    for prior in report["owner_reads"]:
                        validate_observer_window(prior, instant, policy)
                    if batch:
                        validate_observer_window(report["closing_current_results"], instant, policy)
                        if (not datetime.fromisoformat(plan["frozen_at"]) <= instant < datetime.fromisoformat(plan["expires_at"])
                                or not datetime.fromisoformat(allocation["authority_start"]) <= instant < datetime.fromisoformat(allocation["authority_end"])):
                            raise ValueError("workload_plan_expired_or_clock_invalid")
                if instant >= earliest_witness:
                    raise ValueError("workload_original_witness_expired")
                report["global_closing_utc"] = instant.isoformat()
            final_close = close
            report["current_gate_results"] = {"owner_read_index": 1} if policy is not None else observed
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
        if batch and report["terminal_outcome"] in {"completed", "pilot_completed_unqualified"}:
            start_pin = len(originals.pins)
            tick = time.monotonic()
            with workload_deadline(plan["phase_caps"]["owner_read"]):
                current, closing_snapshots, _ = _owner_read(root, runtime, [*review_runs, plan["control_run_id"]], originals, observation_policy=policy,
                    **({"selective_run_id": review_runs[-1]} if _selective_pilot else {}))
            tracker.confirm_completed()
            report["closing_current_elapsed_seconds"] = time.monotonic() - tick
            if report["closing_current_elapsed_seconds"] > plan["phase_caps"]["owner_read"]:
                raise _HostDeadline()
            report["closing_current_results"] = current
            report["closing_current_snapshots"] = [{key: snapshot[key] for key in
                ("run_id", "source_path", "source_sha256", "state_path", "state_sha256")}
                for snapshot in closing_snapshots]
            report["closing_current_original_pins"] = originals.pins[start_pin:]
            originals.append(("closing-current-snapshots", report["closing_current_snapshots"]))
            report["closing_current_source_manifest"] = file_manifest(root)
            originals.append(("closing-current-source-manifest", report["closing_current_source_manifest"]))
            if any(owner_read_identity(prior) != owner_read_identity(current) for prior in report["owner_reads"]):
                raise ValueError("workload_closing_current_changed")
            snapshots.extend(closing_snapshots)
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



def reference_plan_v5(bundle, pair_spec_pin, producer_manifest_pin, **options):
    from tools._review_dependency_runner import reference_plan as build
    return build(bundle, pair_spec_pin, producer_manifest_pin, **options)


def preflight_v5(plan, expected_plan):
    from tools._review_dependency_runner import preflight as check
    return check(plan, expected_plan)


def run_workload_v5(plan, expected_plan, directory, *, plan_pin):
    from tools._review_dependency_runner import run
    return run(plan, expected_plan, directory, plan_pin=plan_pin)


def _file_pin(path):
    path = Path(path).absolute()
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("workload_artifact_path_invalid")
    return {"path": str(path), "raw_sha256": digest_bytes(path.read_bytes()), "size_bytes": path.stat().st_size}


def _new_action(args):
    from tools import _review_dependency_accounting as accounting
    from tools._review_dependency_runner import run_pair
    if args.action == "freeze-v5":
        if any(value is None for value in (args.bundle, args.output, args.pair_spec, args.producer_manifest, args.directory, args.runtime_directory, args.measured_recipe)) or args.mode not in {"legacy_full", "selective_reuse"}:
            raise ValueError("workload_freeze_arguments_required")
        plan = reference_plan_v5(args.bundle, _file_pin(args.pair_spec), _file_pin(args.producer_manifest), mode=args.mode, directory=args.directory, runtime_directory=args.runtime_directory,
            measured_recipe=args.measured_recipe, observation_profile=args.observation_profile, execution_profile=args.execution_profile or "six-hour", pair_closing=args.pair_closing, journal_collection=args.journal_collection)
        retain(args.output.absolute(), plan)
        return {"plan_path": str(args.output.absolute()), "plan_sha256": digest(plan), "plan_id": accounting.document_id(plan), "qualification": False}, True
    if args.plan is None:
        raise ValueError("workload_run_arguments_required")
    plan = accounting.decode("Plan", args.plan.read_bytes())
    if args.action == "preflight-v5":
        if args.expected_plan is None:
            raise ValueError("workload_run_arguments_required")
        return preflight_v5(plan, args.expected_plan), True
    if args.action == "run-v5":
        if args.expected_plan is None or args.directory is None:
            raise ValueError("workload_run_arguments_required")
        report = run_workload_v5(plan, args.expected_plan, args.directory, plan_pin=_file_pin(args.plan))
        return {"report_path": str(args.directory / "report.json"), "report_sha256": digest(report), "terminal_outcome": report["terminal_outcome"]}, report["terminal_outcome"] == "completed"
    if args.action == "pair-v5":
        if args.selective_plan is None or args.directory is None:
            raise ValueError("workload_pair_arguments_required")
        selective = accounting.decode("Plan", args.selective_plan.read_bytes())
        result = run_pair(plan, selective, legacy_plan_pin=_file_pin(args.plan), selective_plan_pin=_file_pin(args.selective_plan), directory=args.directory,
            prospective_case_results_pin=None if args.prospective_cases is None else _file_pin(args.prospective_cases))
        return {"differential_path": str(args.directory / "differential.json"), "outcome": result["outcome"], "qualification": False}, result["outcome"] == "pair_pass"
    raise ValueError("workload_action_invalid")

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "preflight", "run", "freeze-v5", "preflight-v5", "run-v5", "pair-v5"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--expected-plan")
    parser.add_argument("--host", choices=("direct", "scoped_mcp"), default="direct")
    parser.add_argument("--case", default="independent-claim-text")
    parser.add_argument("--study", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--measured-recipe", type=Path)
    parser.add_argument("--observation-profile", choices=("interactive", "batch", "batch-extended"), default="batch")
    parser.add_argument("--execution-profile", choices=("two-hour", "four-hour", "six-hour", "eight-hour", "sixteen-hour", "twenty-four-hour", "forty-eight-hour"))
    parser.add_argument("--pair-closing", action="store_true")
    parser.add_argument("--journal-collection", action="store_true")
    parser.add_argument("--mode", choices=("legacy", "selective", "legacy_full", "selective_reuse"), default="legacy")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--runtime-directory", type=Path)
    parser.add_argument("--pair-spec", type=Path)
    parser.add_argument("--producer-manifest", type=Path)
    parser.add_argument("--selective-plan", type=Path)
    parser.add_argument("--prospective-cases", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action.endswith("-v5"):
            result, success = _new_action(args)
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 0 if success else 2
        if args.action == "freeze":
            if args.bundle is None or args.output is None:
                raise ValueError("workload_freeze_arguments_required")
            path = args.output.absolute()
            if path.resolve() != path or path.is_relative_to(ROOT) or path.exists():
                raise ValueError("workload_new_private_external_directory_required")
            plan = reference_plan(args.bundle, study=args.study, host=args.host, case_id=args.case, measured_recipe=args.measured_recipe, observation_profile=args.observation_profile, execution_profile=args.execution_profile, journal_collection=args.journal_collection)
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
