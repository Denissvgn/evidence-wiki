"""Explicit extended observation preserves closing evidence and authority limits."""

from pathlib import Path

import pytest

from tests.test_review_closure_diagnostic import harness as harness
from tests.test_review_closure_diagnostic import run
from tests.test_review_dependency_workload import plan as plan
from tools import _review_control_ceiling as ceiling
from tools import _review_dependency_pilot as pilot
from tools import run_review_closure_diagnostic as diagnostic
from tools import run_review_dependency_workload as native
from tools._review_dependency_workload import digest


def test_extended_owner_plan_keeps_native_and_service_authority_separate(plan):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    envelope = pilot.reference_plan(bundle, observation_profile="batch-extended",
        execution_profile="sixteen-hour", service_profile=ceiling.OWNER_PROFILE,
        journal_collection=True)
    selected = envelope["native_plan"]
    assert selected["observation_policy"]["max_age_seconds"] == 7200
    assert selected["observation_policy_sha256"] == digest(selected["observation_policy"])
    assert selected["execution_policy"]["authority_seconds"] == 57600
    assert selected["journal_collection_policy"]
    limits = selected["hard_limits"]
    assert limits["native_worker_seconds"] == 600
    assert limits["service_application_seconds"] == limits["scoped_mcp_application_seconds"] == 3600
    assert limits["each_owner_pass_read_seconds"] == selected["phase_caps"]["owner_read"] == 3600
    assert limits["global_close_seconds"] == selected["phase_caps"]["global_close"] == 10800
    assert limits["observer_recency_seconds"] == 7200
    assert selected["service_policy"]["application_lease"]["seconds"] == 3600
    assert selected["source_protocol_limits"] == native.HARD_LIMITS
    assert native.HARD_LIMITS["job_and_MCP_seconds"] == 600
    assert native.HARD_LIMITS["witness_seconds"] == 30
    assert native.validate_plan(selected, digest(selected)) == selected
    assert pilot.validate_plan(envelope, digest(envelope), current=False) == envelope


def _diagnostic_plan(harness, profile, age=4500):
    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile=profile)
    harness.state.update(allocation_seconds=2040, witness_ages={1: age, 2: 20, 3: 0})


def _assert_fresh_closing_and_retention(harness, report):
    assert harness.state["read_count"] == 3
    assert len(report["owner_reads"]) == 2
    assert report["closing_owner_read"] is not None
    assert harness.events.index("owner-read-2") < harness.events.index("child-close")
    assert harness.events.index("child-close") < harness.events.index("owner-read-3")
    assert report["retention"]["status"] == "complete"
    labels = {pin["label"] for pin in report["original_response_pins"]}
    assert {"owner-round-1", "owner-round-2", "owner-round-3", "closing-owner-read"} <= labels
    assert report["review_jobs"] == report["review_attempts"] == report["applied_jobs"] == report["acceptance_credits"] == 0
    assert report["pair_qualified"] is report["pilot_completed"] is False


@pytest.mark.parametrize("profile,outcome", [
    ("batch", "diagnostic_failed_unqualified"),
    ("batch-extended", "diagnostic_completed_unqualified"),
])
def test_extended_diagnostic_accepts_older_original_only_with_explicit_selection(harness, profile, outcome):
    _diagnostic_plan(harness, profile)
    report = run(harness)
    assert report["terminal_outcome"] == outcome
    _assert_fresh_closing_and_retention(harness, report)
    assert report["observation_policy"] == harness.plan["observation_policy"]
    assert report["first_witness"]["closing_latency_seconds"] >= 4500
    if profile == "batch":
        assert report["failure"]["reason"] == "workload_original_witness_expired"
    else:
        assert report["first_witness"]["closing_latency_seconds"] < 7200
        assert report["first_witness"]["closed_within_original_bound"] is True
        assert harness.events.index("owner-read-3") < harness.events.index("verify-closing")


@pytest.mark.parametrize("age", [7200, 7201])
def test_extended_original_expiry_survives_fresh_closing(harness, age):
    _diagnostic_plan(harness, "batch-extended", age)
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == "workload_original_witness_expired"
    assert report["first_witness"]["closed_within_original_bound"] is False
    _assert_fresh_closing_and_retention(harness, report)


@pytest.mark.parametrize("change,reason", [
    ("changed_closing_identity", "closure_owner_closing_changed"),
    ("changed_closing_policy", "observer_policy_binding_invalid"),
    ("closing_candidate_change", "workload_candidate_or_recipe_changed"),
])
def test_extended_bound_does_not_hide_changed_closing_evidence(harness, change, reason):
    _diagnostic_plan(harness, "batch-extended")
    harness.state[change] = True
    report = run(harness)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == reason
    _assert_fresh_closing_and_retention(harness, report)
