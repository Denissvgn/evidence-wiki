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
    validate_execution_window,
    validate_runtime_allocation,
)
from tools._review_dependency_workload import digest, provision

harness = _harness_fixture
plan = _plan_fixture


@pytest.mark.parametrize("profile,seconds,owner_cap,global_cap,total", [
    ("two-hour", 7200, 600, 600, 6660), ("four-hour", 14400, 600, 900, 6960),
    ("six-hour", 21600, 1200, 1500, 8760)])
def test_explicit_plan_binds_separate_execution_and_observation_limits(plan, profile, seconds, owner_cap, global_cap, total):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    selected = workload.reference_plan(bundle, observation_profile="batch", execution_profile=profile)
    assert selected["schema_version"] == "review-dependency-workload-plan/v4"
    assert selected["execution_policy_sha256"] == digest(execution_policy(profile))
    assert selected["hard_limits"]["protected_authority_seconds"] == seconds
    assert selected["source_protocol_limits"] == workload.HARD_LIMITS
    assert selected["source_protocol_limits"]["protected_authority_seconds"] == 7200
    assert selected["hard_limits"]["observer_recency_seconds"] == 3600
    assert selected["hard_limits"]["native_calibration_probe_lifetime_seconds"] == 30
    assert selected["phase_caps"] == {"setup": 600, "job": 600, "mutation": 600,
        "owner_read": owner_cap, "global_close": global_cap, "cleanup": 60}
    assert selected["hard_limits"]["each_owner_pass_read_seconds"] == owner_cap
    assert selected["hard_limits"]["global_close_seconds"] == global_cap
    assert selected["hard_limits"]["job_and_MCP_seconds"] == 600
    assert selected["elapsed_cap_seconds"] == total
    assert datetime.fromisoformat(selected["expires_at"]) - datetime.fromisoformat(selected["frozen_at"]) == timedelta(seconds=seconds)
    assert workload.validate_plan(selected, digest(selected)) == selected
    assert workload.reference_plan(bundle)["schema_version"] == "review-dependency-workload-plan/v2"
    assert workload.reference_plan(bundle, observation_profile="batch")["schema_version"] == "review-dependency-workload-plan/v3"


@pytest.mark.parametrize("profile,owner_cap,global_cap,total", [
    (None, 600, 600, 2040), ("two-hour", 600, 600, 2460),
    ("four-hour", 600, 900, 2760), ("six-hour", 1200, 1500, 4560)])
def test_diagnostic_ceilings_follow_selected_execution_profile(harness, profile, owner_cap, global_cap, total):
    selected = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile=profile)
    assert selected["phase_caps"]["global_close"] == selected["hard_limits"]["global_close_seconds"] == global_cap
    assert selected["elapsed_cap_seconds"] == total
    assert selected["phase_caps"]["owner_read"] == selected["hard_limits"]["each_owner_pass_read_seconds"] == owner_cap
    assert selected["phase_caps"]["setup"] == (180 if profile is None else 600)
    assert selected["phase_caps"]["cleanup"] == 60
    assert selected["hard_limits"]["observer_recency_seconds"] == 3600
    assert selected["hard_limits"]["native_calibration_probe_lifetime_seconds"] == 30
    assert selected["source_protocol_limits"] == workload.HARD_LIMITS


@pytest.mark.parametrize("cap", [901, 900.0, True])
def test_diagnostic_four_hour_closing_ceiling_remains_bounded_and_typed(harness, cap):
    caps = dict(diagnostic.EXECUTION_PHASE_CAPS, global_close=cap)
    with pytest.raises(ValueError, match="closure_allocation_invalid"):
        diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="four-hour", phase_caps=caps)
    assert harness.events == []


@pytest.mark.parametrize("field,cap", [("owner_read", 1201), ("owner_read", 1200.0),
    ("owner_read", True), ("global_close", 1501), ("global_close", 1500.0), ("global_close", True)])
def test_diagnostic_six_hour_caps_remain_bounded_and_typed(harness, field, cap):
    caps = dict(diagnostic.EXECUTION_PHASE_CAPS, owner_read=1200, global_close=1500)
    caps[field] = cap
    with pytest.raises(ValueError, match="closure_allocation_invalid"):
        diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile="six-hour", phase_caps=caps)
    assert harness.events == []


@pytest.mark.parametrize("profile", [None, True, 21600, 21600.0, "21600", "twelve-hour", ["six-hour"]])
def test_execution_profile_requires_a_closed_string(profile):
    with pytest.raises(ValueError, match="execution_policy_invalid"):
        execution_policy(profile)


def test_six_hour_policy_has_one_fixed_nonrenewable_window():
    policy = execution_policy("six-hour")
    assert policy == {"schema_version": "review-execution-budget-policy/v1", "profile": "six-hour",
        "authority_seconds": 21600, "credential_lifetime_seconds": 21600,
        "deadline_basis": "frozen_plan_expiry", "renewal": "forbidden"}
    frozen = datetime(2026, 10, 15, tzinfo=timezone.utc)
    expires = frozen + timedelta(seconds=21600)
    assert validate_execution_window(policy, frozen.isoformat(), expires.isoformat(), instant=frozen) == (frozen, expires)
    for invalid_end in (expires - timedelta(seconds=1), expires + timedelta(seconds=1)):
        with pytest.raises(ValueError, match="execution_authority_clock_invalid"):
            validate_execution_window(policy, frozen.isoformat(), invalid_end.isoformat())
    with pytest.raises(ValueError, match="execution_authority_clock_invalid"):
        validate_execution_window(policy, frozen.isoformat(), expires.isoformat(), instant=expires)


@pytest.mark.parametrize("field", ["authority_seconds", "credential_lifetime_seconds"])
def test_six_hour_policy_refuses_integer_subclasses(field):
    class GrantedSeconds(int):
        pass

    policy = execution_policy("six-hour")
    policy[field] = GrantedSeconds(21600)
    with pytest.raises(ValueError, match="execution_policy_invalid"):
        validate_execution_policy(policy)


@pytest.mark.parametrize("module", [workload, diagnostic])
def test_both_freeze_commands_accept_explicit_six_hour_profile(tmp_path, monkeypatch, module, capsys):
    selected = []

    def freeze(bundle, **kwargs):
        selected.append((bundle, kwargs))
        return {"execution_policy": execution_policy(kwargs["execution_profile"])}

    monkeypatch.setattr(module, "reference_plan", freeze)
    output = tmp_path / "frozen-plan.json"
    assert module.main(["freeze", "--bundle", str(tmp_path), "--output", str(output),
        "--execution-profile", "six-hour"]) == 0
    assert selected[0][0] == tmp_path
    assert selected[0][1]["execution_profile"] == "six-hour"
    assert selected[0][1]["observation_profile"] == "batch"
    assert output.exists()
    assert '"runtime_qualification": "NOT RUN"' in capsys.readouterr().out


@pytest.mark.parametrize("change", ["boolean", "float", "unknown", "duration", "renewal", "profile", "schema"])
@pytest.mark.parametrize("profile", ["two-hour", "four-hour", "six-hour"])
def test_closed_execution_policy_refuses_self_asserted_grants(change, profile):
    policy = execution_policy(profile)
    if change == "boolean":
        policy["authority_seconds"] = True
    elif change == "float":
        policy["credential_lifetime_seconds"] = float(policy["credential_lifetime_seconds"])
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


@pytest.fixture(params=["two-hour", "four-hour", "six-hour"])
def execution_plan(plan, request):
    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    return workload.reference_plan(bundle, observation_profile="batch", execution_profile=request.param)


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


@pytest.mark.parametrize("credential", ["trust", "usage", "signing", "service", "reference_qualification"])
def test_every_runtime_credential_uses_the_original_fixed_end(execution_plan, credential):
    allocation = allocation_document(execution_plan)
    assert allocation["credential_expiries"] == dict.fromkeys(
        ("trust", "usage", "signing", "service", "reference_qualification"), execution_plan["expires_at"])
    assert validate_runtime_allocation(allocation, execution_plan) == allocation
    allocation["credential_expiries"][credential] = (
        datetime.fromisoformat(execution_plan["expires_at"]) + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="execution_runtime_binding_invalid"):
        validate_runtime_allocation(allocation, execution_plan)


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


@pytest.mark.parametrize("profile,owner_cap,global_cap,total", [
    ("four-hour", 600, 900, 2760), ("six-hour", 1200, 1500, 4560)])
def test_diagnostic_capacity_plan_closes_thirteen_journals_without_review_credit(harness, profile, owner_cap, global_cap, total):
    harness.plan = diagnostic.reference_plan(harness.bundle, journal_count=13,
        observation_profile="batch", execution_profile=profile)
    harness.state["allocation_seconds"] = total
    assert harness.plan["schema_version"] == "review-closure-diagnostic-plan/v3"
    assert harness.plan["phase_caps"] == {"setup": 600, "owner_read": owner_cap, "cleanup": 60, "global_close": global_cap}
    assert harness.plan["elapsed_cap_seconds"] == total
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


@pytest.mark.parametrize("profile,prior_profile", [("four-hour", None), ("four-hour", "two-hour"),
    ("six-hour", None), ("six-hour", "two-hour"), ("six-hour", "four-hour")])
def test_full_execution_plan_refuses_other_pilot_policy(plan, tmp_path, monkeypatch, profile, prior_profile):
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
        workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile=profile, measured_recipe=path)


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


@pytest.mark.parametrize("job_cap,allowed", [(350, True), (600, False)])
def test_six_hour_full_allocation_includes_every_job_close_and_group(plan, tmp_path, monkeypatch, job_cap, allowed):
    import json

    from tools._review_dependency_workload import retain

    bundle = Path(plan["artifacts"]["fresh-fixture-input.json"]["path"]).parent
    prior = workload.reference_plan(bundle, observation_profile="batch", execution_profile="six-hour")
    worst = dict.fromkeys(workload.PILOT_CAPS, 1)
    monkeypatch.setattr(workload, "completed_pilot_measurements", lambda *_: (worst, 8))
    caps = dict(workload.phase_ceilings(prior["observation_policy"], prior["execution_policy"]), job=job_cap)
    recipe = {"pilot_plan_pin": retain(tmp_path / "prior-plan.json", prior), "phase_caps": caps,
        "observed_worst_seconds": worst, "group_overhead_cap_seconds": 12, "observed_group_overhead_seconds": 8}
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe))
    if not allowed:
        with pytest.raises(ValueError, match="workload_allocation_exceeds_authority"):
            workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile="six-hour", measured_recipe=path)
        return
    full = workload.reference_plan(bundle, study="full", observation_profile="batch", execution_profile="six-hour", measured_recipe=path)
    assert len(full["assignments"]) == 24
    assert full["review_journals"] == 12 and full["control_journals"] == 1
    assert full["phase_caps"] == caps
    assert full["elapsed_cap_seconds"] == 600 + 24 * 350 + 600 + 2 * 1200 + 3 * 1500 + 60 + 12 * 12 == 16704
    assert 14400 < full["elapsed_cap_seconds"] <= 21600
    assert workload.validate_plan(full, digest(full)) == full
    allocation = allocation_document(full)
    assert allocation["run_policy"]["budgets"]["seconds"] == 16704
    assert validate_runtime_allocation(allocation, full) == allocation


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


@pytest.mark.parametrize("profile,owner_cap,global_cap,total", [
    ("four-hour", 600, 900, 2760), ("six-hour", 1200, 1500, 4560)])
@pytest.mark.parametrize("overrun", [False, True])
def test_third_owner_read_enforces_own_cap_inside_global_close(harness, monkeypatch, profile, owner_cap, global_cap, total, overrun):
    from contextlib import contextmanager

    from tools.decide_release import _HostDeadline

    harness.plan = diagnostic.reference_plan(harness.bundle, observation_profile="batch", execution_profile=profile)
    harness.state["allocation_seconds"] = total
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
            clock[0] += owner_cap + int(overrun)
        return result

    monkeypatch.setattr(diagnostic, "workload_deadline", bounded)
    monkeypatch.setattr(workload, "_owner_read", expensive_closing_read)
    report = diagnostic.run_diagnostic(harness.plan, digest(harness.plan), harness.directory)
    assert entered[-2:] == [global_cap, owner_cap]
    if overrun:
        assert report["terminal_outcome"] == "diagnostic_failed_unqualified"
        assert report["failure"]["kind"] == "_HostDeadline"
    else:
        assert report["terminal_outcome"] == "diagnostic_completed_unqualified"
        assert harness.state["read_count"] == 3
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
def test_actual_signed_credential_originals_cannot_outlive_shared_end(tmp_path, execution_plan, target):
    from tools._review_dependency_workload import OriginalResponses

    expires = execution_plan["expires_at"]
    qualifications = [{"expires_utc": expires}] * 2
    body = {"grant": {"authentication": {"expires_at": expires}, "payload": {"expires_at": expires}},
        "scrub": {"authentication": {"expires_at": expires}}}
    command = {"authentication": {"expires_at": expires}, "payload": {"action": "deposit", "body": body}}
    originals = OriginalResponses(tmp_path / "valid-originals")
    originals.append(("qualifications", qualifications))
    originals.append(("source-usage", [{"command": command}]))
    workload.validate_credential_originals(originals.pins, expires)
    selected = {"qualification": qualifications[0], "usage": command["authentication"],
        "grant_authentication": body["grant"]["authentication"], "grant_payload": body["grant"]["payload"],
        "scrub": body["scrub"]["authentication"]}[target]
    selected["expires_utc" if target == "qualification" else "expires_at"] = "9999-12-31T00:00:00+00:00"
    originals = OriginalResponses(tmp_path / "changed-originals")
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
