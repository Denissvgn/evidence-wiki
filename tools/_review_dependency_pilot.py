"""Fresh selective cohort measurements, separate from historical legacy admission."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from tools import _review_dependency_accounting as accounting
from tools import run_review_dependency_workload as legacy
from tools._observation_freshness import is_batch_policy, validate_observation_policy
from tools._review_dependency_workload import digest, retain

PLAN = "selective-complete-pilot-plan/v1"
REPORT = "selective-complete-pilot-report/v1"
RECIPE = "selective-complete-pilot-measurement/v1"
EXTENDED_RECIPE = "selective-complete-pilot-measurement/v2"
COLLECTION_EXTENDED_RECIPE = "selective-complete-pilot-measurement/v3"
OBSERVATIONAL_PLAN = "selective-observational-pilot-plan/v1"
OBSERVATIONAL_REPORT = "selective-observational-pilot-report/v1"
OBSERVATIONAL_MEASUREMENT = "selective-observational-pilot-measurement/v1"
COLLECTION_OBSERVATIONAL_MEASUREMENT = "selective-observational-pilot-measurement/v2"
COLLECTION_PLAN = "selective-complete-pilot-plan/v2"
COLLECTION_REPORT = "selective-complete-pilot-report/v2"
COLLECTION_OBSERVATIONAL_PLAN = "selective-observational-pilot-plan/v2"
COLLECTION_OBSERVATIONAL_REPORT = "selective-observational-pilot-report/v2"
LONG_PLAN = "selective-observational-pilot-plan/v3"
OWNER_PLAN = "selective-observational-pilot-plan/v4"
LONG_REPORT = "selective-observational-pilot-report/v3"
OWNER_REPORT = "selective-observational-pilot-report/v4"
LONG_MEASUREMENT = "selective-observational-pilot-measurement/v3"
OWNER_MEASUREMENT = "selective-observational-pilot-measurement/v4"
LONG_RECIPE = "selective-complete-pilot-measurement/v4"
OWNER_RECIPE = "selective-complete-pilot-measurement/v5"
from tools import _review_control_ceiling as ceiling_policy
from tools._review_journal_artifacts import POLICY as JOURNAL_COLLECTION_POLICY
from tools._review_journal_artifacts import selected as collection_selected


def _report_schema(plan):
    if plan["schema_version"] in {LONG_PLAN, OWNER_PLAN}:
        return ceiling_policy.version(LONG_REPORT, plan)
    if collection_selected(plan["native_plan"]):
        return COLLECTION_OBSERVATIONAL_REPORT if "service_policy" in plan["native_plan"] else COLLECTION_REPORT
    return OBSERVATIONAL_REPORT if "service_policy" in plan["native_plan"] else REPORT



def service_obligations(assignments):
    """Freeze only the fresh native services selected for this pilot group."""
    result = []
    for row in assignments:
        coordinate = {key: row[key] for key in ("host_id", "case_id", "cycle", "claim_id", "config_ref")}
        baseline = {**coordinate, "cycle": "baseline"}
        result.append(accounting.validate("Obligation", {
            "obligation_id": accounting.identity("review-dependency-service-obligation/v1", coordinate),
            "coordinate": coordinate, "original_id": row["original_id"],
            **{key: row[key] for key in ("execution_role", "task_role", "controller_ref", "journal_key", "native_run_id")},
            "required": True, "baseline_obligation_id": None if row["cycle"] == "baseline" else
                accounting.identity("review-dependency-service-obligation/v1", baseline)}))
    return result


def reference_plan(bundle, *, execution_profile=None, service_profile=None, journal_collection=False, observation_profile="batch"):
    """Use original ceilings unless the caller explicitly selects longer authority."""
    native = legacy.reference_plan(bundle, observation_profile=observation_profile, execution_profile=execution_profile, service_profile=service_profile, journal_collection=journal_collection)
    return {**ceiling_policy.validate(native), "schema_version": ceiling_policy.version(LONG_PLAN, native) if service_profile in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE} else (COLLECTION_OBSERVATIONAL_PLAN if service_profile is not None else COLLECTION_PLAN) if journal_collection else OBSERVATIONAL_PLAN if service_profile is not None else PLAN, "mode": "selective_reuse", "native_plan": native,
            "native_plan_sha256": digest(native), "candidate_sha256": native["candidate_sha256"],
            "obligations": service_obligations(native["assignments"])}


def validate_plan(plan, expected_plan, *, current=True):
    prospective = type(plan) is dict and plan.get("schema_version") in {LONG_PLAN, OWNER_PLAN}
    policy_binding = ceiling_policy.validate(plan, required=prospective)
    if (type(plan) is not dict or set(plan) != {"schema_version", "mode", "native_plan", "native_plan_sha256", "candidate_sha256", "obligations"} | set(policy_binding)
            or plan["schema_version"] not in {PLAN, OBSERVATIONAL_PLAN, COLLECTION_PLAN, COLLECTION_OBSERVATIONAL_PLAN, LONG_PLAN, OWNER_PLAN} or plan["mode"] != "selective_reuse" or digest(plan) != expected_plan):
        raise ValueError("selective_pilot_plan_binding_invalid")
    native = plan["native_plan"]
    observational = plan["schema_version"] in {OBSERVATIONAL_PLAN, COLLECTION_OBSERVATIONAL_PLAN, LONG_PLAN, OWNER_PLAN}
    if (native.get("schema_version") in {legacy.PLAN_SCHEMA_LONG, legacy.PLAN_SCHEMA_OWNER}) != prospective or not ceiling_policy.exact(ceiling_policy.validate(native, required=prospective), policy_binding):
        raise ValueError("selective_pilot_scope_invalid")
    collection = collection_selected(native)
    if collection != (plan["schema_version"] in {COLLECTION_PLAN, COLLECTION_OBSERVATIONAL_PLAN, LONG_PLAN, OWNER_PLAN}):
        raise ValueError("selective_pilot_scope_invalid")
    if observational != (native.get("schema_version") in {legacy.PLAN_SCHEMA_OBSERVATIONAL, legacy.PLAN_SCHEMA_OBSERVATIONAL_COLLECTION, legacy.PLAN_SCHEMA_LONG, legacy.PLAN_SCHEMA_OWNER}) or observational != ("service_policy" in native):
        raise ValueError("selective_pilot_scope_invalid")
    if prospective:
        legacy.validate_long_discovery_shape(native)
    if observational:
        legacy.plan_phase_ceilings(native)
    if (digest(native) != plan["native_plan_sha256"] or native["candidate_sha256"] != plan["candidate_sha256"]
            or native["study"] != "pilot" or not is_batch_policy(native["observation_policy"])
            or plan["obligations"] != service_obligations(native["assignments"])):
        raise ValueError("selective_pilot_scope_invalid")
    if current:
        legacy.validate_plan(native, plan["native_plan_sha256"])
    return plan


def measured_observation_policy(recipe):
    """Recover the exact observer declaration from the pinned original pilot."""
    try:
        original, _ = legacy.read_pinned_artifact(recipe["pilot_plan_pin"])
        if "native_plan" in original:
            validate_plan(original, digest(original), current=False)
            native = original["native_plan"]
        else:
            native = original
        policy = validate_observation_policy(native["observation_policy"])
        if (not is_batch_policy(policy) or native.get("study") != "pilot"
                or native.get("observation_policy_sha256") != digest(policy)
                or recipe["candidate_sha256"] != native["candidate_sha256"]):
            raise ValueError("selective_pilot_observation_policy_invalid")
    except (KeyError, TypeError) as error:
        raise ValueError("selective_pilot_observation_policy_invalid") from error
    return policy


def run(plan, expected_plan, directory):
    """Run actual owners with guarded workers and retain distinct report bytes."""
    validate_plan(plan, expected_plan)
    directory = Path(directory).absolute()
    native = plan["native_plan"]
    result = legacy.run_workload(native, plan["native_plan_sha256"], "direct", "independent-claim-text",
                                 "selective", directory, _selective_pilot=True)
    retain(directory / "native-pilot-plan.json", native)
    report = {**ceiling_policy.validate(plan), "schema_version": _report_schema(plan), "plan_sha256": expected_plan, "candidate_sha256": plan["candidate_sha256"],
              "native_report_pin": _pin(directory / "report.json"), "mode": "selective_reuse",
              "terminal_outcome": result["terminal_outcome"], "pair_qualified": False}
    retain(directory / "selective-pilot-report.json", report)
    return report


def _pin(path):
    path = Path(path).absolute()
    return {"path": str(path), "raw_sha256": legacy.digest_bytes(path.read_bytes())}


def _control_original_pin(control_pin, original_pins):
    """Join the retained control by exact identity and its producer label."""
    fields = {"path", "raw_sha256", "size_bytes"}
    label = "selective-control-no-dispatch"
    if (type(control_pin) is not dict or set(control_pin) not in (fields, fields | {"label"})
            or ("label" in control_pin and control_pin["label"] != label)
            or type(control_pin.get("path")) is not str
            or type(control_pin.get("raw_sha256")) is not str
            or type(control_pin.get("size_bytes")) is not int or control_pin["size_bytes"] < 0
            or type(original_pins) is not list):
        raise ValueError("selective_pilot_control_custody_invalid")
    identity = {key: control_pin[key] for key in fields}
    matches = [row for row in original_pins if type(row) is dict
               and all(type(row.get(key)) is type(value) and row.get(key) == value
                       for key, value in identity.items())]
    labeled = [row for row in original_pins if type(row) is dict and row.get("label") == label]
    if (len(matches) != 1 or len(labeled) != 1 or matches[0] is not labeled[0]
            or set(matches[0]) != fields | {"label"}):
        raise ValueError("selective_pilot_control_custody_invalid")
    return control_pin


def _originals(plan_pin, report_pin, candidate_sha256):
    plan, _ = legacy.read_pinned_artifact(plan_pin)
    report, _ = legacy.read_pinned_artifact(report_pin)
    validate_plan(plan, digest(plan), current=False)
    policy_binding = ceiling_policy.validate(plan)
    ceiling_policy.validate(report, required=plan["schema_version"] in {LONG_PLAN, OWNER_PLAN})
    if (type(report) is not dict or set(report) != {"schema_version", "plan_sha256", "candidate_sha256", "native_report_pin", "mode", "terminal_outcome", "pair_qualified"} | set(policy_binding)
            or report["schema_version"] != _report_schema(plan) or report["plan_sha256"] != digest(plan)
            or report["candidate_sha256"] != candidate_sha256 or plan["candidate_sha256"] != candidate_sha256
            or report["mode"] != "selective_reuse" or report["pair_qualified"] is not False
            or report["terminal_outcome"] != "pilot_completed_unqualified"):
        raise ValueError("selective_pilot_report_binding_invalid")
    native_report, _ = legacy.read_pinned_artifact(report["native_report_pin"])
    control_pin = native_report.get("control", {}).get("dispatch_original_pin")
    _control_original_pin(control_pin, native_report.get("original_response_pins"))
    control, control_raw = legacy.read_pinned_artifact(control_pin)
    if len(control_raw) != control_pin["size_bytes"]:
        raise ValueError("selective_pilot_control_custody_invalid")
    if (control.get("attempt_id") != native_report["control"]["attempt_id"]
            or type(control.get("before")) is not list or control["before"] != control.get("after")):
        raise ValueError("selective_pilot_control_dispatched")
    native_plan = plan["native_plan"]
    # The shared historical validator consumes the actual unchanged native plan.
    # A separate pinned copy is required; the envelope never impersonates it.
    native_plan_pin = _pin(Path(report["native_report_pin"]["path"]).parent / "native-pilot-plan.json")
    if legacy.read_pinned_artifact(native_plan_pin)[0] != native_plan:
        raise ValueError("selective_pilot_native_plan_binding_invalid")
    measured = {"pilot_plan_pin": native_plan_pin, "pilot_report_pin": report["native_report_pin"],
                "phase_caps": {}, "observed_worst_seconds": {}, "group_overhead_cap_seconds": 0,
                "observed_group_overhead_seconds": 0}
    worst, overhead = legacy._completed_pilot_measurements(measured, candidate_sha256, expected_mode="selective")
    receipts = {row["run_id"]: row for row in native_report["journal_receipts"]}
    from tools._review_journal_artifacts import verify_receipt
    for receipt in receipts.values():
        verify_receipt(receipt, original_pins=native_report["original_response_pins"])
    expected_attempts = {row["attempt_id"] for row in native_report["assignments"]}
    actual_attempts = []
    for run_id in {row["native_run_id"] for row in native_plan["assignments"]} | {native_plan["control_run_id"]}:
        records, _ = legacy.read_pinned_native_records(receipts[run_id]["original_pin"])
        applied = [row["data"]["attempt_id"] for row in records
                   if row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"]
        if run_id == native_plan["control_run_id"] and applied:
            raise ValueError("selective_pilot_control_applied")
        actual_attempts.extend(applied)
    if len(actual_attempts) != 6 or set(actual_attempts) != expected_attempts:
        raise ValueError("selective_pilot_extra_application")
    return plan, native_report, worst, overhead



def observational_measurements(plan_pin, report_pin):
    """Retain complete authenticated costs without conferring formal admission."""
    plan, _ = legacy.read_pinned_artifact(plan_pin)
    original, _report, worst, overhead = _originals(plan_pin, report_pin, plan["candidate_sha256"])
    if original["schema_version"] not in {OBSERVATIONAL_PLAN, COLLECTION_OBSERVATIONAL_PLAN, LONG_PLAN, OWNER_PLAN}:
        raise ValueError("selective_observational_plan_required")
    native = original["native_plan"]
    formal = (legacy.formal_phase_ceilings(native.get("observation_policy"), native.get("execution_policy"), ceiling_binding=native)
              if native["service_policy"]["profile"] in {"control-observational", ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE} else
              legacy.phase_ceilings(native.get("observation_policy"), native.get("execution_policy")))
    minima = {name: max(1, math.ceil(value * 1.25)) for name, value in worst.items()}
    exceeded = sorted(name for name in formal if minima[name] > formal[name])
    return {**ceiling_policy.validate(native), "schema_version": ceiling_policy.version(LONG_MEASUREMENT, native) if original["schema_version"] in {LONG_PLAN, OWNER_PLAN} else COLLECTION_OBSERVATIONAL_MEASUREMENT if collection_selected(native) else OBSERVATIONAL_MEASUREMENT,
            **({"journal_collection_policy": dict(JOURNAL_COLLECTION_POLICY)} if collection_selected(native) else {}), "pilot_plan_pin": plan_pin, "pilot_report_pin": report_pin,
            "candidate_sha256": plan["candidate_sha256"], "observed_worst_seconds": worst,
            "observed_group_overhead_seconds": overhead, "minimum_phase_caps": minima,
            "minimum_group_overhead_cap_seconds": math.ceil(overhead * 1.25), "formal_phase_ceilings": formal,
            "formal_recipe_status": "refused_phase_ceilings" if exceeded else "eligible_for_explicit_recipe_validation",
            "exceeded_formal_phases": exceeded, "pair_qualified": False}

def measurement_recipe(plan_pin, report_pin, *, phase_caps=None, group_overhead_cap_seconds=None):
    """Choose bounded prospective caps above authenticated observed costs."""
    plan, _ = legacy.read_pinned_artifact(plan_pin)
    original_plan, _report, worst, overhead = _originals(plan_pin, report_pin, plan["candidate_sha256"])
    native = original_plan["native_plan"]
    if collection_selected(native):
        raise ValueError("selective_extended_pilot_required")
    ceilings = legacy.phase_ceilings(native.get("observation_policy"), native.get("execution_policy"))
    minima = {name: max(1, math.ceil(seconds * 1.25)) for name, seconds in worst.items()}
    selected = minima if phase_caps is None else phase_caps
    authority_seconds = (datetime.fromisoformat(native["expires_at"]) - datetime.fromisoformat(native["frozen_at"])).total_seconds()
    minimum_overhead = math.ceil(overhead * 1.25)
    selected_overhead = minimum_overhead if group_overhead_cap_seconds is None else group_overhead_cap_seconds
    if (type(selected) is not dict or set(selected) != set(ceilings) or set(minima) != set(ceilings)
            or any(type(selected[name]) is not int or not minima[name] <= selected[name] <= ceilings[name] for name in ceilings)
            or type(selected_overhead) is not int or not minimum_overhead <= selected_overhead <= authority_seconds):
        raise ValueError("selective_pilot_allocation_invalid")
    return {"schema_version": RECIPE, "pilot_plan_pin": plan_pin, "pilot_report_pin": report_pin,
            "candidate_sha256": plan["candidate_sha256"], "observed_worst_seconds": worst,
            "observed_group_overhead_seconds": overhead, "phase_caps": dict(selected),
            "group_overhead_cap_seconds": selected_overhead}


def completed_pilot_measurements(measured, candidate_sha256):
    """Validate declared bounds against originals; full admission checks total fit."""
    fields = {"schema_version", "pilot_plan_pin", "pilot_report_pin", "candidate_sha256",
              "observed_worst_seconds", "observed_group_overhead_seconds", "phase_caps", "group_overhead_cap_seconds"}
    if (type(measured) is not dict or set(measured) != fields or measured.get("schema_version") != RECIPE
            or measured.get("candidate_sha256") != candidate_sha256):
        raise ValueError("selective_pilot_measurement_invalid")
    expected = measurement_recipe(measured["pilot_plan_pin"], measured["pilot_report_pin"],
                                  phase_caps=measured["phase_caps"],
                                  group_overhead_cap_seconds=measured["group_overhead_cap_seconds"])
    if (not ceiling_policy.exact(measured, expected) if measured.get("schema_version") in {LONG_RECIPE, OWNER_RECIPE} else measured != expected):
        raise ValueError("selective_pilot_measurement_invalid")
    return measured["observed_worst_seconds"], measured["observed_group_overhead_seconds"]


def extended_measurement_recipe(plan_pin, report_pin, *, execution_profile, phase_caps=None, group_overhead_cap_seconds=None):
    """Bind complete measurements to a separate prospective formal authority."""
    from tools._execution_budget import execution_policy
    plan, _ = legacy.read_pinned_artifact(plan_pin)
    original, _report, worst, overhead = _originals(plan_pin, report_pin, plan["candidate_sha256"])
    native = original["native_plan"]
    if (original["schema_version"] not in {OBSERVATIONAL_PLAN, COLLECTION_OBSERVATIONAL_PLAN, LONG_PLAN, OWNER_PLAN}
            or native.get("service_policy") not in [legacy.service_policy(profile) for profile in ("extended-observational", "control-observational", ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE)]):
        raise ValueError("selective_extended_pilot_required")
    prospective = original["schema_version"] in {LONG_PLAN, OWNER_PLAN}
    policy_binding = ceiling_policy.validate(native, required=prospective)
    if prospective != (native["service_policy"]["profile"] in {ceiling_policy.PROFILE, ceiling_policy.OWNER_PROFILE}) or prospective and not collection_selected(native):
        raise ValueError("selective_extended_pilot_required")
    producer_id = _report["closing_current_results"]["strict"]["review_producer_id"]
    if type(producer_id) is not str or not producer_id.startswith("sha256:") or len(producer_id) != 71:
        raise ValueError("selective_pilot_producer_binding_invalid")
    execution = execution_policy(execution_profile)
    ceilings = legacy.formal_phase_ceilings(native["observation_policy"], execution, ceiling_binding=policy_binding)
    minima = {name: max(1, math.ceil(seconds * 1.25)) for name, seconds in worst.items()}
    selected = minima if phase_caps is None else phase_caps
    minimum_overhead = math.ceil(overhead * 1.25)
    selected_overhead = minimum_overhead if group_overhead_cap_seconds is None else group_overhead_cap_seconds
    if (type(selected) is not dict or set(selected) != set(ceilings) or set(minima) != set(ceilings)
            or any(type(selected[name]) is not int or not minima[name] <= selected[name] <= ceilings[name] for name in ceilings)
            or type(selected_overhead) is not int or not minimum_overhead <= selected_overhead <= execution["authority_seconds"]):
        raise ValueError("selective_pilot_allocation_invalid")
    return {**policy_binding, "schema_version": ceiling_policy.version(LONG_RECIPE, native) if prospective else COLLECTION_EXTENDED_RECIPE if collection_selected(native) else EXTENDED_RECIPE,
            **({"journal_collection_policy": dict(JOURNAL_COLLECTION_POLICY)} if collection_selected(native) else {}), "pilot_plan_pin": plan_pin, "pilot_report_pin": report_pin,
            "candidate_sha256": plan["candidate_sha256"], "observed_worst_seconds": worst,
            "observed_group_overhead_seconds": overhead, "phase_caps": dict(selected),
            "group_overhead_cap_seconds": selected_overhead,
            "measurement_execution_policy": native["execution_policy"], "formal_execution_policy": execution,
            "review_producer_id": producer_id,
            "formal_application_policy": legacy.formal_application_policy(selected["job"])}


def completed_extended_pilot_measurements(measured, candidate_sha256):
    """Reconstruct the declared recipe only from complete original evidence."""
    if (type(measured) is not dict or measured.get("schema_version") not in {EXTENDED_RECIPE, COLLECTION_EXTENDED_RECIPE, LONG_RECIPE, OWNER_RECIPE}
            or measured.get("candidate_sha256") != candidate_sha256):
        raise ValueError("selective_pilot_measurement_invalid")
    ceiling_policy.validate(measured, required=measured["schema_version"] in {LONG_RECIPE, OWNER_RECIPE})
    try:
        expected = extended_measurement_recipe(measured["pilot_plan_pin"], measured["pilot_report_pin"],
            execution_profile=measured["formal_execution_policy"]["profile"], phase_caps=measured["phase_caps"],
            group_overhead_cap_seconds=measured["group_overhead_cap_seconds"])
    except (KeyError, TypeError):
        raise ValueError("selective_pilot_measurement_invalid") from None
    if (not ceiling_policy.exact(measured, expected) if measured.get("schema_version") in {LONG_RECIPE, OWNER_RECIPE} else measured != expected):
        raise ValueError("selective_pilot_measurement_invalid")
    return measured["observed_worst_seconds"], measured["observed_group_overhead_seconds"]
