"""Extended allocations retain the original fixture policy boundary."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from evidence_wiki import RunPolicy
from tests._script_loader import load_module
from tests.test_selective_review_workload import cohort as _cohort_fixture
from tests.test_selective_review_workload import pin as _pin_fixture
from tools import _review_dependency_accounting as accounting
from tools import _review_dependency_pilot as pilot
from tools import _review_dependency_runner as runner
from tools import _review_dependency_workload as workload
from tools import run_review_dependency_workload as legacy
from tools._execution_budget import execution_policy
from tools._observation_freshness import observation_policy

cohort = _cohort_fixture
pin = _pin_fixture


@pytest.fixture
def real_policy():
    path = workload.EXAMPLE / "support.py"
    return load_module("fixture_policy_boundary", path).policy


@pytest.fixture
def observational_binding():
    execution, observation = execution_policy("eight-hour"), observation_policy("batch")
    service = legacy.service_policy("extended-observational")
    caps = dict(legacy.phase_ceilings(observation, execution), job=3600)
    frozen = datetime.now(timezone.utc) - timedelta(seconds=1)
    binding = {"execution_policy": execution, "execution_policy_sha256": workload.digest(execution),
        "observation_policy": observation, "observation_policy_sha256": workload.digest(observation),
        "hard_limits": legacy.runtime_hard_limits(observation, execution, phase_caps=caps),
        "source_protocol_limits": legacy.HARD_LIMITS, "phase_caps": caps, "group_overhead_cap_seconds": 0,
        "authority_frozen_at": frozen.isoformat(), "authority_expires_at": (frozen + timedelta(hours=8)).isoformat(),
        "service_policy": service, "service_selection": {"study": "pilot", "host": "direct", "case_id": "independent-claim-text", "mode": "selective"}}
    return execution, binding


def test_real_fixture_policy_extended_allocation_uses_exact_validated_budget(real_policy, observational_binding, tmp_path):
    execution, binding = observational_binding
    with pytest.raises(ValueError):
        real_policy(tmp_path, "Retained records", run_seconds=26760)
    document = workload._run_policy_document(real_policy, tmp_path, "Retained records", 26760,
        execution_policy=execution, allocation_binding=binding)
    actual = RunPolicy(document)
    assert actual.to_dict()["budgets"]["seconds"] == 26760
    assert actual.to_dict()["scope"]["source_scope"] == [str(tmp_path / "raw")]
    assert actual.to_dict()["required_evaluators"] == real_policy(tmp_path, "Retained records").to_dict()["required_evaluators"]
    assert binding["authority_expires_at"] == (datetime.fromisoformat(binding["authority_frozen_at"]) + timedelta(hours=8)).isoformat()


@pytest.mark.parametrize("seconds", [1, 1800, 5220, 21600, 25200])
@pytest.mark.parametrize("execution", [None, execution_policy("six-hour")])
def test_legacy_policy_documents_and_duration_boundary_remain_exact(real_policy, tmp_path, seconds, execution):
    expected = real_policy(tmp_path, "Retained records", run_seconds=seconds).to_dict()
    expected["policy_id"] = None
    actual = workload._run_policy_document(real_policy, tmp_path, "Retained records", seconds, execution_policy=execution)
    assert workload.canonical(actual) == workload.canonical(expected)
    assert RunPolicy(actual).to_dict() == RunPolicy(expected).to_dict()
    with pytest.raises(ValueError):
        workload._run_policy_document(real_policy, tmp_path, "Retained records", 25201, execution_policy=execution)


@pytest.mark.parametrize("change", ["missing", "allocation", "authority", "typed_policy", "expired"])
def test_unvalidated_extended_budget_refuses_before_policy_factory(observational_binding, tmp_path, change):
    execution, binding = observational_binding
    binding = copy.deepcopy(binding)
    seconds = 26760
    if change == "missing":
        binding = None
    elif change == "allocation":
        seconds += 1
    elif change == "authority":
        execution = {**execution, "authority_seconds": 86401}
    elif change == "typed_policy":
        del binding["service_policy"]
    elif change == "expired":
        frozen = datetime.now(timezone.utc) - timedelta(hours=9)
        binding.update(authority_frozen_at=frozen.isoformat(), authority_expires_at=(frozen + timedelta(hours=8)).isoformat())
    called = []

    def factory(*args, **kwargs):
        called.append(True)
        raise AssertionError("unvalidated allocation reached policy construction")

    with pytest.raises(ValueError):
        workload._run_policy_document(factory, tmp_path, "Retained records", seconds,
            execution_policy=execution, allocation_binding=binding)
    assert called == []


def test_real_formal_policy_supports_larger_exact_allocation_after_recipe_admission(real_policy, cohort, tmp_path, monkeypatch):
    plan, _pair = cohort
    caps = legacy.formal_phase_ceilings(plan["observation_policy"], execution_policy("twenty-four-hour"))
    caps["job"] = 1000
    execution = execution_policy("twenty-four-hour")
    frozen = datetime.now(timezone.utc) - timedelta(seconds=1)
    plan.update(schema_version=accounting.EXTENDED_PLAN, execution_policy=execution,
        execution_policy_sha256=workload.digest(execution), phase_caps=caps,
        formal_application_policy=legacy.formal_application_policy(caps["job"]),
        pair_closing_reservation=accounting.extended_closing_reservation(caps["global_close"]),
        frozen_at=frozen.isoformat(), expires_at=(frozen + timedelta(hours=24)).isoformat(),
        elapsed_cap_seconds=legacy.allocation_seconds(caps, 24) + 2 * caps["global_close"])
    plan["host_identity_pin"] = workload.retain(tmp_path / "host.json", {"host_id": "direct"})
    recipe = {"formal_execution_policy": execution, "phase_caps": caps,
        "candidate_sha256": plan["candidate_sha256"], "review_producer_id": plan["review_producer_id"],
        "formal_application_policy": plan["formal_application_policy"],
        "group_overhead_cap_seconds": 0}
    plan["measured_recipe_pin"] = workload.retain(tmp_path / "recipe.json", recipe)
    admitted = []

    def admitted_recipe(actual, candidate):
        assert actual == recipe and candidate == plan["candidate_sha256"]
        admitted.append(True)
        return {}, 0

    # Only historical native recipe admission is isolated; authority, allocation,
    # the actual example policy factory and public policy validation remain real.
    monkeypatch.setattr(pilot, "completed_extended_pilot_measurements", admitted_recipe)
    assignments = [{**row["coordinate"],
        **{key: row[key] for key in ("original_id", "controller_ref", "execution_role", "task_role", "required", "journal_key", "native_run_id")},
        "attempt_id": None, "application_event_ref": None} for row in plan["obligations"]]
    binding = runner._allocation_binding(plan, assignments)
    seconds = plan["elapsed_cap_seconds"]
    assert seconds == 66060
    with pytest.raises(ValueError):
        real_policy(tmp_path, "Retained records", run_seconds=seconds)
    document = workload._run_policy_document(real_policy, tmp_path, "Retained records", seconds,
        execution_policy=execution, allocation_binding=binding)
    assert RunPolicy(document).to_dict()["budgets"]["seconds"] == seconds
    assert admitted == [True]
    with pytest.raises(ValueError, match="execution_runtime_binding_invalid"):
        workload._run_policy_document(real_policy, tmp_path, "Retained records", seconds - 1,
            execution_policy=execution, allocation_binding=binding)


def test_control_observation_provisioning_binds_caps_before_policy_factory(real_policy, observational_binding, tmp_path):
    execution, binding = observational_binding
    binding = copy.deepcopy(binding)
    binding["service_policy"] = legacy.service_policy("control-observational")
    binding["phase_caps"] = legacy.observational_phase_ceilings(binding["service_policy"], binding["observation_policy"], execution)
    assert workload._run_policy_document(real_policy, tmp_path, "Retained records", 27360,
        execution_policy=execution, allocation_binding=binding)["budgets"]["seconds"] == 27360
    for mutation in (600, 1201, True):
        changed = copy.deepcopy(binding)
        changed["phase_caps"]["mutation"] = mutation
        called = []
        def factory(*args, _called=called, **kwargs):
            _called.append(True)
            raise AssertionError("unvalidated allocation reached policy construction")
        with pytest.raises(ValueError):
            workload._run_policy_document(factory, tmp_path, "Retained records", 27360,
                execution_policy=execution, allocation_binding=changed)
        assert called == []
