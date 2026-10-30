"""Observer policy binding preserves original windows and current owner facts."""

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_review_dependency_workload import measurement_recipe
from tests.test_review_dependency_workload import plan as _plan_fixture
from tools import run_review_dependency_workload as workload
from tools._observation_freshness import (
    observation_policy,
    observer_deadline,
    validate_observation_policy,
    validate_observer_window,
)
from tools._review_dependency_workload import OriginalResponses, digest, retain

plan = _plan_fixture


def observed(profile="batch"):
    policy = observation_policy(profile)
    clock = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    witnesses = [{"run_id": "run", "observed_utc": clock.isoformat(), "policy_id": "policy",
        "credentials": {"observed_at": clock.isoformat(), "authority_id": "trust", "services": [
            {"principal": "reviewer", "credential_state": "verified", "challenge_ref": "nonce"}]},
        "attempts": [{"context_ref": "input", "attestation_ref": "review", "lease_state": "current"}]}]
    return {"strict": {"basis_id": "basis", "claims": []}, "publication": {"verdict": "hold"},
        "journal_receipts": [{"run_id": "run", "records_sha256": "records", "head_ref": "head",
            "source_sha256": "source", "state_sha256": "state"}],
        "native_current": [{"run_id": "run", "observed_utc": clock.isoformat(), "basis_id": "basis",
            "policy_id": "policy", "authority": {"terminal_grants_available": True},
            "sources": {"inventory": "source"}, "qualification": {"state": "qualified"},
            "usage": {"seconds": 1}}], "native_host_witnesses": witnesses,
        "observation_policy": policy, "observation_policy_sha256": digest(policy),
        "observer_recency_not_after": observer_deadline(witnesses, policy),
        "witness_semantics": policy["witness_semantics"],
        "native_calibration_witness": "unavailable_legacy_qualification_profile"}


def test_closed_profiles_and_exact_expiry():
    first = observed()
    clock = datetime.fromisoformat(first["native_host_witnesses"][0]["observed_utc"])
    assert validate_observer_window(first, clock + timedelta(seconds=3599), observation_policy("batch"))
    for closing in (clock - timedelta(microseconds=1), clock + timedelta(seconds=3600)):
        with pytest.raises(ValueError):
            validate_observer_window(first, closing, observation_policy("batch"))
    interactive = observed("interactive")
    assert validate_observer_window(interactive, clock + timedelta(seconds=29), observation_policy("interactive"))
    with pytest.raises(ValueError):
        validate_observer_window(interactive, clock + timedelta(seconds=30), observation_policy("interactive"))


@pytest.mark.parametrize("change", [
    {"schema_version": "observer-recency-policy/v2"}, {"max_age_seconds": 7200},
    {"max_age_seconds": 3600.0}, {"max_age_seconds": True}, {"profile": "interactive"},
    {"original_window_renewal": "allowed"}, {"extra": True}, {"profile": []},
])
def test_policy_refuses_forged_versions_types_and_lifetimes(change):
    with pytest.raises(ValueError):
        validate_observation_policy(dict(observation_policy("batch"), **change))


def test_window_binding_refuses_forged_ttl_digest_and_cross_profile():
    first = observed()
    closing = datetime.fromisoformat(first["native_host_witnesses"][0]["observed_utc"])
    for change in ({"observation_policy_sha256": "forged"}, {"observer_recency_not_after": closing.isoformat()},
            {"observation_policy": observation_policy("interactive")}):
        with pytest.raises(ValueError):
            validate_observer_window(dict(first, **change), closing, observation_policy("batch"))


def test_new_plan_policy_and_prospective_allocation_preserve_legacy(plan):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    assert plan["schema_version"] == workload.PLAN_SCHEMA and plan["elapsed_cap_seconds"] == 5220
    assert "observation_policy" not in plan and plan["hard_limits"]["witness_seconds"] == 30
    for profile, cap in (("batch", 6660), ("interactive", 5220)):
        new = workload.reference_plan(bundle, observation_profile=profile)
        assert new["schema_version"] == workload.PLAN_SCHEMA_CURRENT
        assert new["observation_policy"] == observation_policy(profile)
        assert new["observation_policy_sha256"] == digest(new["observation_policy"])
        assert new["elapsed_cap_seconds"] == cap <= 7200
        assert new["phase_caps"]["job"] == 600
        assert new["source_protocol_limits"] == plan["hard_limits"]
        assert new["hard_limits"]["observer_recency_seconds"] == (3600 if profile == "batch" else 30)
        assert new["hard_limits"]["native_calibration_probe_lifetime_seconds"] == 30
        assert new["hard_limits"]["each_owner_pass_read_seconds"] == (600 if profile == "batch" else 120)
        assert "witness_seconds" not in new["hard_limits"]
        workload.validate_plan(new, digest(new))
        forged = copy.deepcopy(new)
        forged["observation_policy"]["max_age_seconds"] = 7200
        forged["observation_policy_sha256"] = digest(forged["observation_policy"])
        with pytest.raises(ValueError):
            workload.validate_plan(forged, digest(forged))


@pytest.mark.parametrize("surface,field,value", [
    ("native_current", "policy_id", "different-policy"),
    ("native_current", "authority", {"terminal_grants_available": False}),
    ("native_current", "sources", {"inventory": "changed-source"}),
    ("native_current", "qualification", {"state": "expired"}),
    ("native_host_witnesses", "policy_id", "different-policy"),
    ("native_host_witnesses", "credentials", {"authority_id": "changed-trust"}),
    ("native_host_witnesses", "attempts", [{"context_ref": "changed-input"}]),
    ("native_host_witnesses", "credentials", {"services": [{"principal": "changed-reviewer"}]}),
])
def test_current_owner_identity_refuses_changed_source_policy_grant_trust_and_credential(surface, field, value):
    first = observed()
    closing = copy.deepcopy(first)
    closing[surface][0][field] = value
    assert workload.owner_read_identity(first) != workload.owner_read_identity(closing)


def test_fresh_authentication_nonce_and_clock_do_not_renew_original_bound():
    first = observed()
    closing = copy.deepcopy(first)
    later = datetime.fromisoformat(first["native_host_witnesses"][0]["observed_utc"]) + timedelta(seconds=3601)
    witness = closing["native_host_witnesses"][0]
    witness["observed_utc"] = witness["credentials"]["observed_at"] = later.isoformat()
    witness["credentials"]["services"][0]["challenge_ref"] = "fresh-nonce"
    witness["attempts"][0]["lease_state"] = "expired"
    closing["native_current"][0]["observed_utc"] = later.isoformat()
    closing["native_current"][0]["usage"]["seconds"] = 3602
    closing["observer_recency_not_after"] = observer_deadline(closing["native_host_witnesses"], closing["observation_policy"])
    assert workload.owner_read_identity(first) == workload.owner_read_identity(closing)
    assert validate_observer_window(closing, later, closing["observation_policy"])
    with pytest.raises(ValueError, match="original_witness_expired"):
        validate_observer_window(first, later, first["observation_policy"])


def batch_measurement(plan, directory):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    new_plan = workload.reference_plan(bundle, observation_profile="batch")
    recipe, report = measurement_recipe(new_plan, directory)
    report["schema_version"] = workload.RESULT_SCHEMA_CURRENT
    policy = new_plan["observation_policy"]
    report.update(observation_policy=policy, observation_policy_sha256=digest(policy),
        hard_limits=new_plan["hard_limits"], source_protocol_limits=new_plan["source_protocol_limits"],
        current_gate_results={"owner_read_index": 1})
    for phase in report["phase_elapsed"]:
        if phase["phase"].startswith("owner-read-") or phase["phase"] == "global-close":
            phase["cap_seconds"] = 600
    for result in report["owner_reads"]:
        result.update(observation_policy=policy, observation_policy_sha256=digest(policy), native_current=[{"run_id": "run"}])
        result["native_host_witnesses"][0]["run_id"] = "run"
        result["observer_recency_not_after"] = observer_deadline(result["native_host_witnesses"], policy)
    read_originals = OriginalResponses(directory / "owner-reads")
    for result in report["owner_reads"]:
        read_originals.append(("owner-strict-current", result["strict"]))
        read_originals.append(("owner-publication-current", result["publication"]))
        read_originals.append(("run-current", result["native_current"][0]))
        read_originals.append(("run-native-host-qualification", result["native_host_witnesses"][0]))
    current = copy.deepcopy(report["owner_reads"][-1])
    originals = OriginalResponses(directory / "closing")
    allocation_originals = OriginalResponses(directory / "allocation")
    allocation_originals.append(("runtime-allocation", {
        "allocation_binding": {"plan_sha256": digest(new_plan), "candidate_sha256": new_plan["candidate_sha256"],
            "phase_caps": new_plan["phase_caps"], "observation_policy": policy, "observation_policy_sha256": digest(policy),
            "hard_limits": new_plan["hard_limits"], "source_protocol_limits": new_plan["source_protocol_limits"]},
        "authority_start": new_plan["frozen_at"], "authority_end": new_plan["expires_at"]}))
    originals.append(("owner-strict-current", current["strict"]))
    originals.append(("owner-publication-current", current["publication"]))
    originals.append(("run-current", current["native_current"][0]))
    originals.append(("run-native-host-qualification", current["native_host_witnesses"][0]))
    report.update(closing_current_results=current, closing_current_snapshots=[{"run_id": "run"}],
        closing_current_source_manifest={"source": "hash"}, closing_current_original_pins=list(originals.pins),
        closing_current_elapsed_seconds=1)
    originals.append(("closing-current-snapshots", report["closing_current_snapshots"]))
    originals.append(("closing-current-source-manifest", report["closing_current_source_manifest"]))
    report["original_response_pins"] += allocation_originals.pins + read_originals.pins + originals.pins
    recipe["pilot_report_pin"] = retain(directory / "batch-report.json", report)
    return recipe, report


def test_completed_batch_costs_require_original_closing_validation(plan, tmp_path):
    recipe, report = batch_measurement(plan, tmp_path / "measurement")
    worst, overhead = workload.completed_pilot_measurements(recipe, plan["candidate_sha256"])
    assert worst["job"] == 8 and overhead == 4
    for index, change in enumerate(({"closing_current_results": None}, {"closing_current_original_pins": []},
            {"closing_current_snapshots": []}, {"observation_policy_sha256": "forged"},
            {"current_gate_results": {"owner_read_index": True}}, {"current_gate_results": {"owner_read_index": 0}},
            {"current_gate_results": {"owner_read_index": 1, "extra": True}},
            {"current_gate_results": report["owner_reads"][1]})):
        altered = dict(report, **change)
        pin = retain(tmp_path / (str(index) + "-report.json"), altered)
        with pytest.raises(ValueError):
            workload.completed_pilot_measurements(dict(recipe, pilot_report_pin=pin), plan["candidate_sha256"])
