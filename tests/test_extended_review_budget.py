"""Prospective service bounds require complete pinned measurements."""


import pytest

from tools import _review_dependency_pilot as pilot
from tools import _review_dependency_runner as runner
from tools import _review_dependency_workload as workload
from tools import run_review_dependency_workload as legacy
from tools._execution_budget import execution_policy, validate_execution_policy, validate_execution_window
from tools._observation_freshness import observation_policy
from tools.decide_release import _HostDeadline


@pytest.mark.parametrize("profile,seconds", [("eight-hour", 28800), ("sixteen-hour", 57600), ("twenty-four-hour", 86400)])
def test_extended_authority_is_closed_and_nonrenewable(profile, seconds):
    policy = execution_policy(profile)
    assert policy["schema_version"] == "review-execution-budget-policy/v2"
    assert policy["authority_seconds"] == policy["credential_lifetime_seconds"] == seconds
    assert policy["renewal"] == "forbidden"
    assert validate_execution_policy(policy) == policy
    for changed in ({**policy, "authority_seconds": seconds + 1}, {**policy, "renewal": "allowed"},
                    {**policy, "schema_version": "review-execution-budget-policy/v1"}):
        with pytest.raises(ValueError, match="execution_policy_invalid"):
            validate_execution_policy(changed)
    with pytest.raises(ValueError, match="execution_authority_clock_invalid"):
        validate_execution_window(policy, "2026-10-16T00:00:00+00:00", "2026-10-18T00:00:00+00:00")


@pytest.mark.parametrize("profile,seconds", [("two-hour", 7200), ("four-hour", 14400), ("six-hour", 21600)])
def test_old_execution_policy_document_is_unchanged(profile, seconds):
    assert execution_policy(profile) == {"schema_version": "review-execution-budget-policy/v1", "profile": profile,
        "authority_seconds": seconds, "credential_lifetime_seconds": seconds,
        "deadline_basis": "frozen_plan_expiry", "renewal": "forbidden"}
    assert legacy.phase_ceilings(observation_policy("batch"), execution_policy(profile))["job"] == 600


def test_extended_measurement_profile_has_fixed_pilot_allocation():
    observation, execution = observation_policy("batch"), execution_policy("eight-hour")
    service = legacy.service_policy("extended-observational")
    assert service["application_lease"] == {"schema_version": "research-review-application-lease/v2", "seconds": 3600}
    assert service["native_worker_seconds"] == 600
    legacy.validate_service_policy(service, study="pilot", host="direct", case_id="independent-claim-text",
        observation=observation, execution=execution)
    caps = legacy.plan_phase_ceilings({"study": "pilot", "host": {"host_id": "direct"},
        "case_id": "independent-claim-text", "observation_policy": observation, "execution_policy": execution, "service_policy": service})
    assert legacy.allocation_seconds(caps, 6) == 26760
    assert observation["max_age_seconds"] == 3600
    for study, host, authority in (("full", "direct", execution), ("pilot", "scoped_mcp", execution),
                                   ("pilot", "direct", execution_policy("six-hour"))):
        with pytest.raises(ValueError, match="workload_service_policy_invalid"):
            legacy.validate_service_policy(service, study=study, host=host, case_id="independent-claim-text",
                observation=observation, execution=authority)


@pytest.fixture
def measured_originals(monkeypatch):
    native = {"observation_policy": observation_policy("batch"), "execution_policy": execution_policy("eight-hour"),
              "service_policy": legacy.service_policy("extended-observational")}
    plan = {"schema_version": pilot.OBSERVATIONAL_PLAN, "candidate_sha256": "a" * 64, "native_plan": native}
    worst = {"setup": 10, "mutation": 10, "job": 800, "owner_read": 100, "cleanup": 10, "global_close": 100}
    report = {"closing_current_results": {"strict": {"review_producer_id": "sha256:" + "b" * 64}}}
    monkeypatch.setattr(legacy, "read_pinned_artifact", lambda _: (plan, b"original"))
    monkeypatch.setattr(pilot, "_originals", lambda *_: (plan, report, worst, 12))
    return plan, worst


@pytest.mark.parametrize("profile", ["eight-hour", "sixteen-hour", "twenty-four-hour"])
def test_recipe_separates_original_measurement_authority_from_formal_choice(measured_originals, profile):
    plan, worst = measured_originals
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile=profile)
    assert recipe["measurement_execution_policy"] == plan["native_plan"]["execution_policy"]
    assert recipe["formal_execution_policy"] == execution_policy(profile)
    assert recipe["phase_caps"]["job"] == 1000
    assert recipe["formal_application_policy"] == legacy.formal_application_policy(1000)
    assert pilot.completed_extended_pilot_measurements(recipe, "a" * 64) == (worst, 12)
    for changed in ({**recipe, "candidate_sha256": "c" * 64}, {**recipe, "review_producer_id": "sha256:" + "d" * 64},
                    {**recipe, "measurement_execution_policy": execution_policy("sixteen-hour")},
                    {**recipe, "unbound": True}):
        with pytest.raises(ValueError, match="selective_pilot_measurement_invalid"):
            pilot.completed_extended_pilot_measurements(changed, "a" * 64)
    with pytest.raises(ValueError, match="selective_pilot_measurement_invalid"):
        pilot.completed_pilot_measurements(recipe, "a" * 64)


@pytest.mark.parametrize("field,value", [("job", 999), ("job", 3601), ("job", True), ("setup", 1201),
                                        ("mutation", 1201), ("owner_read", 3601), ("global_close", 10801), ("cleanup", 61)])
def test_extended_recipe_enforces_measured_minima_and_closed_ceilings(measured_originals, field, value):
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour", phase_caps={**recipe["phase_caps"], field: value})


def test_extended_recipe_never_admits_incomplete_pilot(measured_originals, monkeypatch):
    def incomplete(*_):
        raise ValueError("selective_pilot_report_binding_invalid")
    monkeypatch.setattr(pilot, "_originals", incomplete)
    with pytest.raises(ValueError, match="selective_pilot_report_binding_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="sixteen-hour")


def test_extended_recipe_refuses_old_authority_and_service_policy(measured_originals):
    plan, _ = measured_originals
    with pytest.raises(ValueError, match="workload_application_policy_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="six-hour")
    plan["native_plan"]["service_policy"] = legacy.service_policy("observational")
    with pytest.raises(ValueError, match="selective_extended_pilot_required"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="eight-hour")


def test_formal_policy_rejects_worker_changes_mismatched_leases_and_boolean_caps():
    policy = legacy.formal_application_policy(3600)
    execution = execution_policy("twenty-four-hour")
    assert legacy.validate_formal_application_policy(policy, execution, {"job": 3600}) == policy
    for changed in ({**policy, "native_worker_seconds": 601}, {**policy, "application_lease": {"seconds": 3600}},
                    {**policy, "service_seconds": True}):
        with pytest.raises(ValueError, match="workload_application_policy_invalid"):
            legacy.validate_formal_application_policy(changed, execution, {"job": 3600})
    with pytest.raises(ValueError, match="workload_application_policy_invalid"):
        legacy.validate_formal_application_policy(policy, execution, {"job": 3599})


def test_timeout_cause_survives_mutation_unwind_without_changing_completion(tmp_path):
    originals = workload.OriginalResponses(tmp_path / "responses")
    rows = []
    with pytest.raises(RuntimeError):
        with workload.review_job_timing(originals, rows, run_id="run", claim_id="claim", config_ref="critic",
                role="critic", controller="critic-controller", cap=3600):
            try:
                raise _HostDeadline()
            except _HostDeadline as deadline:
                raise RuntimeError("secondary source text must not be retained") from deadline
    assert rows[0]["status"] == "timed_out"
    assert rows[0]["failure"]["kind"] == "_HostDeadline"
    assert rows[0]["application_event_ref"] is None
    secondary = [pin for pin in originals.pins if pin["label"].endswith("secondary-unwind")]
    assert len(secondary) == 1
    assert workload.deadline_cause(RuntimeError("unrelated")) is None
    assert runner._failure(RuntimeError("unrelated"))["kind"] == "RuntimeError"


def test_suppressed_timer_context_remains_causal_evidence():
    try:
        try:
            raise _HostDeadline()
        except _HostDeadline:
            raise RuntimeError("bounded unwind") from None
    except RuntimeError as error:
        assert isinstance(workload.deadline_cause(error), _HostDeadline)
        assert workload.failure_document(error)["kind"] == "_HostDeadline"


@pytest.mark.parametrize("failed", [False, True])
def test_scoped_service_control_borrows_original_window_once_and_closes(monkeypatch, failed):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from evidence_wiki import operation_control, review_application_lease

    end = (datetime.now(timezone.utc) + timedelta(seconds=3000)).isoformat()
    session = SimpleNamespace(execution=SimpleNamespace(profile=SimpleNamespace(
        review_application_lease=SimpleNamespace(seconds=3600, document=lambda: {"schema_version": "research-review-application-lease/v2"}))))
    issued, controls = [], []

    def original_window(actual):
        assert actual is session
        issued.append(actual)
        return {"application_deadline_utc": end}

    class BorrowedControl:
        def __init__(self, **fields):
            self.fields = fields
            self.closed = False
            controls.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(review_application_lease, "issuance", original_window)
    monkeypatch.setattr(operation_control, "OperationControl", BorrowedControl)
    if failed:
        with pytest.raises(RuntimeError):
            with workload.service_operation_control(session, "scoped_mcp") as control:
                assert control is controls[0]
                raise RuntimeError("bounded refusal")
    else:
        with workload.service_operation_control(session, "scoped_mcp") as control:
            assert control is controls[0]
            assert control.fields["deadline_utc"] == end
            assert 0 < control.fields["timeout_seconds"] <= 3000
            assert not control.closed
    assert len(issued) == len(controls) == 1
    assert controls[0].closed


def test_direct_service_does_not_create_scoped_control():
    from types import SimpleNamespace

    session = SimpleNamespace(execution=SimpleNamespace(profile=SimpleNamespace(review_application_lease=None)))
    with workload.service_operation_control(session, "direct") as control:
        assert control is None


def test_extended_formal_ceilings_are_separate_from_measurement_caps():
    from tools import _review_dependency_accounting as accounting

    execution, observation = execution_policy("eight-hour"), observation_policy("batch")
    formal = legacy.formal_phase_ceilings(observation, execution)
    assert formal == {"setup": 1200, "mutation": 1200, "job": 3600, "owner_read": 3600,
                      "global_close": 10800, "cleanup": 60}
    assert legacy.phase_ceilings(observation, execution) == {"setup": 600, "mutation": 600, "job": 600,
        "owner_read": 1200, "global_close": 1500, "cleanup": 60}
    selected = {**formal, "job": 1000, "global_close": 3000}
    limits = legacy.runtime_hard_limits(observation, execution, phase_caps=selected)
    assert limits["native_worker_seconds"] == 600
    assert limits["service_application_seconds"] == limits["scoped_mcp_application_seconds"] == 1000
    assert limits["each_owner_pass_read_seconds"] == 3600
    assert limits["global_close_seconds"] == 3000
    assert "job_and_MCP_seconds" not in limits
    assert legacy.HARD_LIMITS["job_and_MCP_seconds"] == 600
    with pytest.raises(ValueError, match="workload_extended_policy_required"):
        legacy.runtime_hard_limits(observation, execution)
    default = accounting.extended_closing_reservation(3000)
    assert default["mode_close_seconds"] == 1000 and default["pair_close_seconds"] == 2000
    custom = {**default, "mode_close_seconds": 1200, "pair_close_seconds": 1800}
    assert accounting.extended_closing_reservation(3000, custom) == custom
    assert accounting.extended_closing_reservation(10800)["pair_close_seconds"] == 7200
    for changed in ({**custom, "pair_close_seconds": 1801}, {**custom, "mode_close_seconds": True},
                    {**custom, "mode_close_seconds": 3601}, {**custom, "pair_close_seconds": 7201}):
        with pytest.raises(ValueError):
            accounting.extended_closing_reservation(3000, changed)
    for bound in (1499, 10801, True):
        with pytest.raises(ValueError, match="accounting_pair_reservation_invalid"):
            accounting.extended_closing_reservation(bound)


def test_control_observation_is_explicit_closed_and_preserves_old_policy_bytes():
    old = legacy.service_policy("extended-observational")
    assert old == {"schema_version": "review-observational-service-policy/v3", "profile": "extended-observational",
        "service_seconds": 3600, "native_worker_seconds": 600, "qualification": "none",
        "application_lease": {"schema_version": "research-review-application-lease/v2", "seconds": 3600},
        "execution_profile_schema": "research-execution-profile/v17", "job_schema": "research-host-job/v6"}
    selected = legacy.service_policy("control-observational")
    assert selected == {**old, "profile": "control-observational", "schema_version": "review-observational-service-policy/v4", "mutation_seconds": 1200}
    observation, execution = observation_policy("batch"), execution_policy("eight-hour")
    caps = legacy.observational_phase_ceilings(selected, observation, execution)
    assert caps == {"setup": 600, "job": 3600, "mutation": 1200, "owner_read": 1200,
                    "global_close": 1500, "cleanup": 60}
    assert legacy.allocation_seconds(caps, 6) == 27360
    assert legacy.observational_phase_ceilings(old, observation, execution) == {**caps, "mutation": 600}
    assert legacy.formal_phase_ceilings(observation, execution)["mutation"] == 1200
    for changed in ({**selected, "schema_version": old["schema_version"]}, {key: value for key, value in selected.items() if key != "mutation_seconds"},
                    {**selected, "mutation_seconds": 1201}, {**selected, "mutation_seconds": 1200.0},
                    {**selected, "mutation_seconds": True}, {**old, "mutation_seconds": 1200},
                    {**selected, "native_worker_seconds": 1200}, {**selected, "qualification": "formal"}):
        with pytest.raises(ValueError, match="workload_service_policy_invalid"):
            legacy.observational_phase_ceilings(changed, observation, execution)


def test_control_observation_formal_recipe_requires_complete_measurements(measured_originals, monkeypatch):
    plan, worst = measured_originals
    plan["native_plan"]["service_policy"] = legacy.service_policy("control-observational")
    recipe = pilot.extended_measurement_recipe({}, {}, execution_profile="eight-hour")
    assert recipe["phase_caps"]["mutation"] == 13
    assert recipe["formal_application_policy"] == legacy.formal_application_policy(1000)
    worst["mutation"] = 961
    with pytest.raises(ValueError, match="selective_pilot_allocation_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="eight-hour")
    def incomplete(*_):
        raise ValueError("selective_pilot_report_binding_invalid")
    monkeypatch.setattr(pilot, "_originals", incomplete)
    with pytest.raises(ValueError, match="selective_pilot_report_binding_invalid"):
        pilot.extended_measurement_recipe({}, {}, execution_profile="eight-hour")


@pytest.mark.parametrize("profile,mutation_limit", [("extended-observational", 600), ("control-observational", 1200)])
def test_observational_summary_preserves_old_ceilings_and_reports_new_formal_feasibility(measured_originals, monkeypatch, profile, mutation_limit):
    plan, worst = measured_originals
    plan["native_plan"]["service_policy"] = legacy.service_policy(profile)
    worst["mutation"] = 700
    summary = pilot.observational_measurements({}, {})
    assert summary["formal_phase_ceilings"]["mutation"] == mutation_limit
    assert summary["minimum_phase_caps"]["mutation"] == 875
    assert ("mutation" in summary["exceeded_formal_phases"]) == (profile == "extended-observational")
    assert summary["pair_qualified"] is False
    worst["mutation"] = 961
    summary = pilot.observational_measurements({}, {})
    assert summary["minimum_phase_caps"]["mutation"] == 1202
    assert "mutation" in summary["exceeded_formal_phases"]
    assert summary["formal_recipe_status"] == "refused_phase_ceilings"
