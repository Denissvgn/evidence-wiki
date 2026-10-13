"""Prospective execution authority cannot renew or borrow observer freshness."""

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_review_closure_diagnostic import harness as _harness_fixture
from tests.test_review_dependency_workload import plan as _plan_fixture
from tools import run_review_closure_diagnostic as diagnostic
from tools import run_review_dependency_workload as workload
from tools._execution_budget import (
    credential_expiries,
    execution_policy,
    provisioning_window,
    validate_execution_policy,
    validate_runtime_allocation,
)
from tools._review_dependency_workload import digest, provision

harness = _harness_fixture
plan = _plan_fixture


@pytest.mark.parametrize("profile,seconds", [("two-hour", 7200), ("four-hour", 14400)])
def test_explicit_plan_binds_separate_execution_and_observation_limits(plan, profile, seconds):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    selected = workload.reference_plan(bundle, observation_profile="batch", execution_profile=profile)
    assert selected["schema_version"] == "review-dependency-workload-plan/v4"
    assert selected["execution_policy_sha256"] == digest(execution_policy(profile))
    assert selected["hard_limits"]["protected_authority_seconds"] == seconds
    assert selected["source_protocol_limits"] == workload.HARD_LIMITS
    assert selected["source_protocol_limits"]["protected_authority_seconds"] == 7200
    assert selected["hard_limits"]["observer_recency_seconds"] == 3600
    assert selected["hard_limits"]["native_calibration_probe_lifetime_seconds"] == 30
    close = 900 if profile == "four-hour" else 600
    assert selected["phase_caps"]["global_close"] == selected["hard_limits"]["global_close_seconds"] == close
    assert selected["elapsed_cap_seconds"] == (6960 if profile == "four-hour" else 6660)
    assert datetime.fromisoformat(selected["expires_at"]) - datetime.fromisoformat(selected["frozen_at"]) == timedelta(seconds=seconds)
    assert workload.validate_plan(selected, digest(selected)) == selected
    assert workload.reference_plan(bundle)["schema_version"] == "review-dependency-workload-plan/v2"
    assert workload.reference_plan(bundle, observation_profile="batch")["schema_version"] == "review-dependency-workload-plan/v3"


@pytest.mark.parametrize("profile,global_cap,total", [(None, 600, 2040), ("two-hour", 600, 2460), ("four-hour", 900, 2760)])
def test_diagnostic_closing_ceiling_changes_only_for_explicit_four_hour(harness, profile, global_cap, total):
    selected = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile=profile)
    assert selected["phase_caps"]["global_close"] == selected["hard_limits"]["global_close_seconds"] == global_cap
    assert selected["elapsed_cap_seconds"] == total
    assert selected["phase_caps"]["owner_read"] == selected["hard_limits"]["each_owner_pass_read_seconds"] == 600
    assert selected["hard_limits"]["observer_recency_seconds"] == 3600
    assert selected["hard_limits"]["native_calibration_probe_lifetime_seconds"] == 30
    assert selected["source_protocol_limits"] == workload.HARD_LIMITS


@pytest.mark.parametrize("cap", [901, 900.0, True])
def test_diagnostic_four_hour_closing_ceiling_remains_bounded_and_typed(harness, cap):
    caps = dict(diagnostic.EXECUTION_PHASE_CAPS, global_close=cap)
    with pytest.raises(ValueError, match="closure_allocation_invalid"):
        diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="four-hour", phase_caps=caps)
    assert harness.events == []


@pytest.mark.parametrize("change", ["boolean", "float", "unknown", "duration", "renewal", "profile", "schema"])
def test_closed_execution_policy_refuses_self_asserted_grants(change):
    policy = execution_policy("four-hour")
    if change == "boolean":
        policy["authority_seconds"] = True
    elif change == "float":
        policy["credential_lifetime_seconds"] = 14400.0
    elif change == "unknown":
        policy["approval"] = True
    elif change == "duration":
        policy["authority_seconds"] += 1
    else:
        policy[change] = "unapproved"
    with pytest.raises(ValueError, match="execution_policy_invalid"):
        validate_execution_policy(policy)


@pytest.mark.parametrize("observer", [None, "interactive"])
def test_execution_route_requires_batch_before_provisioning(plan, observer):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    with pytest.raises(ValueError, match="execution_observation_profile_invalid"):
        workload.reference_plan(bundle, execution_profile="four-hour", observation_profile=observer)


@pytest.mark.parametrize("observer", [None, "interactive"])
def test_diagnostic_execution_requires_batch_before_children(harness, observer):
    with pytest.raises(ValueError, match="execution_observation_profile_invalid"):
        diagnostic.reference_plan(harness.bundle, execution_profile="four-hour", observation_profile=observer)
    assert harness.events == []


@pytest.fixture
def execution_plan(plan):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    return workload.reference_plan(bundle, observation_profile="batch", execution_profile="four-hour")


def allocation_document(plan):
    clock = datetime.now(timezone.utc).isoformat()
    binding = {key: plan[key] for key in ("candidate_sha256", "phase_caps", "hard_limits", "source_protocol_limits",
        "execution_policy", "execution_policy_sha256", "observation_policy", "observation_policy_sha256")}
    binding.update(plan_sha256=digest(plan), authority_frozen_at=plan["frozen_at"], authority_expires_at=plan["expires_at"])
    binding.update((key, plan[key]) for key in ("assignments", "group_overhead_cap_seconds", "control_run_id"))
    return {"schema_version": "review-workload-runtime-allocation/v2", "allocation_binding": binding,
        "execution_policy": plan["execution_policy"], "execution_policy_sha256": plan["execution_policy_sha256"],
        "credential_expiries": credential_expiries(plan["expires_at"]), "authority_start": clock,
        "authority_end": plan["expires_at"], "issued_before_worker_outputs": clock,
        "run_policy": {"budgets": {"seconds": plan["elapsed_cap_seconds"]}}}


@pytest.mark.parametrize("change", ["policy_digest", "candidate", "plan", "frozen", "end", "credential", "start", "issued", "schema", "run_seconds"])
def test_actual_runtime_allocation_refuses_forged_or_incoherent_expiry(execution_plan, change):
    allocation = allocation_document(execution_plan)
    assert validate_runtime_allocation(allocation, execution_plan) == allocation
    altered = copy.deepcopy(allocation)
    if change in {"candidate", "plan", "frozen"}:
        key = {"candidate": "candidate_sha256", "plan": "plan_sha256", "frozen": "authority_frozen_at"}[change]
        altered["allocation_binding"][key] = "changed"
    elif change == "credential":
        altered["credential_expiries"]["reference_qualification"] = "9999-12-31T00:00:00+00:00"
    elif change in {"start", "issued"}:
        altered["authority_start" if change == "start" else "issued_before_worker_outputs"] = (
            datetime.fromisoformat(execution_plan["frozen_at"]) - timedelta(seconds=1)).isoformat()
    elif change == "run_seconds":
        altered["run_policy"]["budgets"]["seconds"] += 1
    else:
        altered[{"policy_digest": "execution_policy_sha256", "end": "authority_end", "schema": "schema_version"}[change]] = "changed"
    with pytest.raises(ValueError, match="execution_runtime_binding_invalid|execution_authority_clock_invalid"):
        validate_runtime_allocation(altered, execution_plan)


def test_shared_expiry_is_fixed_and_exclusive(execution_plan):
    allocation = allocation_document(execution_plan)
    start, end = provisioning_window(execution_plan["execution_policy"], allocation["allocation_binding"], execution_plan["elapsed_cap_seconds"])
    assert end == execution_plan["expires_at"]
    assert datetime.fromisoformat(start) >= datetime.fromisoformat(execution_plan["frozen_at"])
    expiry = datetime.fromisoformat(end)
    assert validate_runtime_allocation(allocation, execution_plan, closing=expiry - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="execution_authority_clock_invalid"):
        validate_runtime_allocation(allocation, execution_plan, closing=expiry)


@pytest.mark.parametrize("change", ["digest", "observer", "expiry", "allocation", "limits", "source"])
def test_bad_provisioning_authority_refuses_before_filesystem_writes(execution_plan, tmp_path, change):
    binding = allocation_document(execution_plan)["allocation_binding"]
    if change == "digest":
        binding["execution_policy_sha256"] = "changed"
    elif change == "observer":
        binding["observation_policy"]["profile"] = "interactive"
    elif change == "expiry":
        binding["authority_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=14401)).isoformat()
    elif change == "limits":
        binding["hard_limits"]["protected_authority_seconds"] = 14401
    elif change == "source":
        binding["source_protocol_limits"] = dict(binding["source_protocol_limits"], protected_authority_seconds=14400)
    base = tmp_path / "provision"
    seconds = True if change == "allocation" else execution_plan["elapsed_cap_seconds"]
    with pytest.raises(ValueError):
        provision(base, {}, [], run_seconds=seconds, allocation_binding=binding, execution_policy=execution_plan["execution_policy"])
    assert not base.exists()


def test_diagnostic_capacity_plan_closes_thirteen_journals_without_review_credit(harness):
    harness.plan = diagnostic.reference_plan(harness.bundle, journal_count=13,
        observation_profile="batch", execution_profile="four-hour")
    harness.state["allocation_seconds"] = 2760
    assert harness.plan["schema_version"] == "review-closure-diagnostic-plan/v3"
    assert harness.plan["phase_caps"] == {"setup": 600, "owner_read": 600, "cleanup": 60, "global_close": 900}
    assert harness.plan["elapsed_cap_seconds"] == 2760
    report = diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)
    assert report["terminal_outcome"] == "diagnostic_completed_unqualified"
    assert report["schema_version"] == "review-closure-diagnostic-observation/v3"
    assert report["execution_policy"] == harness.plan["execution_policy"]
    assert report["review_attempts"] == report["applied_jobs"] == report["acceptance_credits"] == 0
    assert len(report["journal_receipts"]) == 13
    assert harness.state["read_count"] == 3


def test_relabelled_legacy_plan_cannot_gain_execution_authority(plan):
    altered = copy.deepcopy(plan)
    altered["schema_version"] = workload.PLAN_SCHEMA_EXECUTION
    altered["execution_policy"] = execution_policy("four-hour")
    altered["execution_policy_sha256"] = digest(altered["execution_policy"])
    with pytest.raises(ValueError):
        workload.validate_plan(altered, digest(altered))


@pytest.mark.parametrize("prior_profile", [None, "two-hour"])
def test_full_execution_plan_refuses_other_pilot_policy(plan, tmp_path, monkeypatch, prior_profile):
    import json

    from tools._review_dependency_workload import retain

    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    prior = workload.reference_plan(bundle, observation_profile="batch", execution_profile=prior_profile)
    worst = dict.fromkeys(workload.PILOT_CAPS, 1)
    monkeypatch.setattr(workload, "completed_pilot_measurements", lambda *_: (worst, 0))
    recipe = {"pilot_plan_pin": retain(tmp_path / "prior-plan.json", prior),
        "phase_caps": dict(workload.phase_ceilings(prior["observation_policy"], prior.get("execution_policy")), job=350), "observed_worst_seconds": worst,
        "group_overhead_cap_seconds": 0, "observed_group_overhead_seconds": 0}
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    with pytest.raises(ValueError, match="workload_pilot_policy_mismatch"):
        workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile="four-hour", measured_recipe=path)


@pytest.mark.parametrize("profile,allowed", [("two-hour", False), ("four-hour", True)])
def test_full_measured_caps_are_bounded_by_explicit_authority(plan, tmp_path, monkeypatch, profile, allowed):
    import json

    from tools._review_dependency_workload import retain

    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    prior = workload.reference_plan(bundle, observation_profile="batch", execution_profile=profile)
    worst = dict.fromkeys(workload.PILOT_CAPS, 1)
    monkeypatch.setattr(workload, "completed_pilot_measurements", lambda *_: (worst, 0))
    recipe = {"pilot_plan_pin": retain(tmp_path / "prior-plan.json", prior),
        "phase_caps": dict(workload.phase_ceilings(prior["observation_policy"], prior.get("execution_policy")), job=350), "observed_worst_seconds": worst,
        "group_overhead_cap_seconds": 0, "observed_group_overhead_seconds": 0}
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    if allowed:
        full = workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile=profile, measured_recipe=path)
        assert 7200 < full["elapsed_cap_seconds"] <= 14400
        assert full["phase_caps"]["owner_read"] == 600
        assert full["phase_caps"]["global_close"] == 900
    else:
        with pytest.raises(ValueError, match="workload_allocation_exceeds_authority"):
            workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile=profile, measured_recipe=path)


@pytest.mark.parametrize("global_cap,allowed", [(600, False), (641, True), (900, True), (901, False)])
def test_full_closing_cap_retains_measured_margin_and_profile_ceiling(plan, tmp_path, monkeypatch, global_cap, allowed):
    import json

    from tools._review_dependency_workload import retain

    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    prior = workload.reference_plan(bundle, observation_profile="batch", execution_profile="four-hour")
    worst = dict.fromkeys(workload.PILOT_CAPS, 1)
    worst["global_close"] = 512.395679
    monkeypatch.setattr(workload, "completed_pilot_measurements", lambda *_: (worst, 0))
    caps = dict(workload.phase_ceilings(prior["observation_policy"], prior["execution_policy"]), job=350, global_close=global_cap)
    recipe = {"pilot_plan_pin": retain(tmp_path / "prior-plan.json", prior), "phase_caps": caps,
        "observed_worst_seconds": worst, "group_overhead_cap_seconds": 0, "observed_group_overhead_seconds": 0}
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    if allowed:
        full = workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile="four-hour", measured_recipe=path)
        assert full["phase_caps"]["global_close"] == global_cap
        assert full["elapsed_cap_seconds"] <= 14400
    else:
        with pytest.raises(ValueError, match="workload_measured_allocation_required"):
            workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile="four-hour", measured_recipe=path)


def test_third_owner_read_cannot_borrow_larger_closing_budget(harness, monkeypatch):
    from contextlib import contextmanager

    from tools.decide_release import _HostDeadline

    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="four-hour")
    harness.state["allocation_seconds"] = 2760
    clock, entered = [0], []

    @contextmanager
    def bounded(seconds):
        entered.append(seconds)
        started = clock[0]
        yield
        if clock[0] - started > seconds:
            raise _HostDeadline()

    original = workload._owner_read

    def expensive_closing_read(*args, **kwargs):
        result = original(*args, **kwargs)
        if harness.state["read_count"] == 3:
            clock[0] += 601
        return result

    monkeypatch.setattr(diagnostic, "workload_deadline", bounded)
    monkeypatch.setattr(workload, "_owner_read", expensive_closing_read)
    report = diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)
    assert entered[-2:] == [900, 600]
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["kind"] == "_HostDeadline"
    assert report["acceptance_credits"] == 0


@pytest.mark.parametrize("field", ["authority_end", "credential_expiries", "execution_policy_sha256"])
def test_diagnostic_closing_refuses_changed_actual_runtime_authority(harness, field):
    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="four-hour")
    harness.state["allocation_seconds"] = 2760
    harness.state["allocation_change"] = (field, "changed")
    report = diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == "execution_runtime_binding_invalid"
    assert report["acceptance_credits"] == 0
    assert report["cleanup"]["status"] == "confirmed"


def test_diagnostic_closing_refuses_when_fixed_authority_expires_during_run(harness, monkeypatch):
    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="four-hour")
    harness.state["allocation_seconds"] = 2760
    expiry = datetime.fromisoformat(harness.plan["expires_at"])

    class ClosingClock:
        fromisoformat = datetime.fromisoformat

        @staticmethod
        def now(_zone):
            return expiry

    original = diagnostic.verify_journal_closing

    def expire(snapshots):
        original(snapshots)
        monkeypatch.setattr(diagnostic, "datetime", ClosingClock)

    monkeypatch.setattr(diagnostic, "verify_journal_closing", expire)
    report = diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == "execution_authority_clock_invalid"
    assert report["acceptance_credits"] == 0


def test_final_clock_is_sampled_after_reading_credential_originals(harness, monkeypatch):
    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="four-hour")
    harness.state["allocation_seconds"] = 2760
    expiry = datetime.fromisoformat(harness.plan["expires_at"])

    class ClosingClock:
        fromisoformat = datetime.fromisoformat

        @staticmethod
        def now(_zone):
            return expiry

    original = workload.validate_credential_originals

    def late_read(pins, end):
        original(pins, end)
        monkeypatch.setattr(diagnostic, "datetime", ClosingClock)

    monkeypatch.setattr(workload, "validate_credential_originals", late_read)
    report = diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)
    assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
    assert report["failure"]["reason"] == "execution_authority_clock_invalid"
    assert report["acceptance_credits"] == 0


@pytest.mark.parametrize("target", ["qualification", "usage", "grant_authentication", "grant_payload", "scrub"])
def test_actual_signed_credential_originals_cannot_outlive_shared_end(tmp_path, target):
    from tools._review_dependency_workload import OriginalResponses

    expires = "2026-10-14T01:00:00+00:00"
    qualifications = [{"expires_utc": expires}] * 2
    body = {"grant": {"authentication": {"expires_at": expires}, "payload": {"expires_at": expires}},
        "scrub": {"authentication": {"expires_at": expires}}}
    command = {"authentication": {"expires_at": expires}, "payload": {"action": "deposit", "body": body}}
    selected = {"qualification": qualifications[0], "usage": command["authentication"],
        "grant_authentication": body["grant"]["authentication"], "grant_payload": body["grant"]["payload"],
        "scrub": body["scrub"]["authentication"]}[target]
    selected["expires_utc" if target == "qualification" else "expires_at"] = "9999-12-31T00:00:00+00:00"
    originals = OriginalResponses(tmp_path / "originals")
    originals.append(("qualifications", qualifications))
    originals.append(("source-usage", [{"command": command}]))
    with pytest.raises(ValueError, match="execution_runtime_binding_invalid"):
        workload.validate_credential_originals(originals.pins, expires)


@pytest.mark.parametrize("field", ["execution_policy_sha256", "expires_at", "hard_limits"])
def test_diagnostic_tampering_refuses_before_children(harness, field):
    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="four-hour")
    if field == "hard_limits":
        harness.plan[field]["protected_authority_seconds"] = 14401
    else:
        harness.plan[field] = "changed"
    with pytest.raises(ValueError):
        diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)
    assert harness.events == []
