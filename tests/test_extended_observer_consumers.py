"""Pinned observer policies survive selection and allocation without renewal."""

import copy
from types import SimpleNamespace

import pytest

from evidence_wiki import _review_producer
from tests._review_dependency_fixture import case_evidence_report, fixture_document
from tools import _review_dependency_case_budget as budget
from tools import _review_dependency_case_evidence as evidence
from tools import _review_dependency_pilot as pilot
from tools import _review_dependency_runner as runner
from tools import _review_dependency_workload as workload
from tools import run_review_dependency_workload as formal
from tools._execution_budget import execution_policy
from tools._observation_freshness import observation_policy


def pinned_recipe(tmp_path, profile):
    """Persist policy provenance only; this is no completed native execution."""
    source = case_evidence_report()[1]
    observation = observation_policy(profile)
    native = {"schema_version": formal.PLAN_SCHEMA_EXECUTION, "study": "pilot",
        "candidate_sha256": source["candidate_sha256"], "assignments": [],
        "observation_policy": observation, "observation_policy_sha256": workload.digest(observation)}
    envelope = {"schema_version": pilot.PLAN, "mode": "selective_reuse", "native_plan": native,
        "native_plan_sha256": workload.digest(native), "candidate_sha256": source["candidate_sha256"],
        "obligations": []}
    recipe = {"schema_version": pilot.EXTENDED_RECIPE, "candidate_sha256": source["candidate_sha256"],
        "pilot_plan_pin": workload.retain(tmp_path / "pilot.json", envelope),
        "formal_execution_policy": execution_policy("twenty-four-hour"),
        "phase_caps": {"setup": 100, "job": 700, "mutation": 100, "owner_read": 100,
            "global_close": 100, "cleanup": 30}, "group_overhead_cap_seconds": 100,
        "review_producer_id": "sha256:" + "a" * 64,
        "formal_application_policy": formal.formal_application_policy(700)}
    return recipe, source


def selection_from_original(tmp_path, monkeypatch, profile):
    recipe, source = pinned_recipe(tmp_path, profile)
    pin = workload.retain(tmp_path / "recipe.json", recipe)
    monkeypatch.setattr(evidence, "source_identity", lambda: source)
    # Original current-owner admission is a separate boundary from pin propagation.
    monkeypatch.setattr(pilot, "completed_extended_pilot_measurements", lambda *_: None)
    generation = SimpleNamespace(producer_id=recipe["review_producer_id"])
    monkeypatch.setattr(_review_producer, "capture", lambda: generation)
    monkeypatch.setattr(_review_producer, "assert_current", lambda *_: None)
    return budget.select(pin, "twenty-four-hour", fixture_document(), "selective_reuse",
        ["unchanged-baseline"], False), recipe


@pytest.mark.parametrize("profile,seconds", [("batch", 3600), ("batch-extended", 7200)])
def test_native_case_selection_preserves_pinned_policy(tmp_path, monkeypatch, profile, seconds):
    selection, recipe = selection_from_original(tmp_path, monkeypatch, profile)
    expected = observation_policy(profile)
    assert selection["observation_policy"] == expected
    assert selection["observation_policy"]["max_age_seconds"] == seconds
    assert selection["observation_policy_sha256"] == workload.digest(expected)
    assert budget.validate_selection(selection, current=False) == budget.fixture_budget(recipe, selection["execution_policy"])
    assert "observation_policy" not in recipe


@pytest.mark.parametrize("mutation,reason", [
    ("profile", "native_case_extended_binding_invalid"),
    ("digest", "execution_observation_profile_invalid"),
    ("bool-age", "observation_policy_invalid"),
])
def test_historical_selection_rejects_changed_observer_binding(tmp_path, monkeypatch, mutation, reason):
    selection, _ = selection_from_original(tmp_path, monkeypatch, "batch-extended")
    changed = copy.deepcopy(selection)
    if mutation == "profile":
        changed["observation_policy"] = observation_policy("batch")
        changed["observation_policy_sha256"] = workload.digest(changed["observation_policy"])
    elif mutation == "digest":
        changed["observation_policy_sha256"] = "0" * 64
    else:
        changed["observation_policy"]["max_age_seconds"] = True
        changed["observation_policy_sha256"] = workload.digest(changed["observation_policy"])
    with pytest.raises(ValueError, match=reason):
        budget.validate_selection(changed, current=False)


def test_formal_allocation_binding_preserves_extended_observer_policy():
    observation = observation_policy("batch-extended")
    execution = execution_policy("six-hour")
    plan = {"candidate_sha256": "a" * 64,
        "phase_caps": {"setup": 100, "job": 100, "mutation": 100, "owner_read": 100,
            "global_close": 100, "cleanup": 30},
        "execution_policy": execution, "execution_policy_sha256": workload.digest(execution),
        "observation_policy": observation, "observation_policy_sha256": workload.digest(observation),
        "frozen_at": "2026-10-16T00:00:00+00:00", "expires_at": "2026-10-16T06:00:00+00:00",
        "group_overhead_cap_seconds": 100,
        "journal_slots": [{"native_run_id": "control", "kind": "changed_basis_control"}]}
    binding = runner._allocation_binding(plan, [])
    assert binding["observation_policy"] == observation
    assert binding["observation_policy_sha256"] == workload.digest(observation)
    assert binding["hard_limits"] == formal.runtime_hard_limits(observation, execution,
        phase_caps=plan["phase_caps"], ceiling_binding=plan)


@pytest.mark.parametrize("mutation", ["profile", "digest"])
def test_formal_selector_checks_policy_from_original_before_output(tmp_path, monkeypatch, mutation):
    recipe, source = pinned_recipe(tmp_path, "batch-extended")
    from tools import _review_control_ceiling as ceiling_policy
    from tools._review_journal_artifacts import POLICY as journal_policy
    recipe.update(ceiling_policy.binding(ceiling_policy.OWNER_PROFILE),
        schema_version=pilot.OWNER_RECIPE, journal_collection_policy=dict(journal_policy))
    recipe_path = tmp_path / "recipe.json"
    workload.retain(recipe_path, recipe)
    pair_pin = workload.retain(tmp_path / "pair.json", {"host_id": "host", "case_id": "case",
        "candidate_sha256": source["candidate_sha256"]})
    monkeypatch.setattr(runner.accounting, "validate_pair", lambda value: value)
    calls = []
    selected_policy = observation_policy("batch" if mutation == "profile" else "batch-extended")
    def selected(*_args, **kwargs):
        calls.append(kwargs["observation_profile"])
        return {"candidate_sha256": source["candidate_sha256"],
            "observation_policy": selected_policy,
            "observation_policy_sha256": "0" * 64 if mutation == "digest" else workload.digest(selected_policy)}
    monkeypatch.setattr(formal, "reference_plan", selected)
    directory = tmp_path / "output"
    with pytest.raises(ValueError, match="accounting_measured_recipe_changed"):
        runner.reference_plan(None, pair_pin, {}, mode="selective_reuse", directory=directory,
            runtime_directory=tmp_path / "runtime", measured_recipe=recipe_path,
            observation_profile="batch-extended", pair_closing=True, journal_collection=True)
    assert not directory.exists()
    assert calls == ["batch-extended"]


def test_formal_selector_rejects_extended_observer_in_historical_recipe(tmp_path, monkeypatch):
    recipe, source = pinned_recipe(tmp_path, "batch-extended")
    recipe_path = tmp_path / "recipe.json"
    workload.retain(recipe_path, recipe)
    pair_pin = workload.retain(tmp_path / "pair.json", {"host_id": "host", "case_id": "case",
        "candidate_sha256": source["candidate_sha256"]})
    monkeypatch.setattr(runner.accounting, "validate_pair", lambda value: value)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("historical_recipe_reached_native_selector")
    monkeypatch.setattr(formal, "reference_plan", forbidden)
    directory = tmp_path / "output"
    with pytest.raises(ValueError, match="accounting_observation_profile_unsupported"):
        runner.reference_plan(None, pair_pin, {}, mode="selective_reuse", directory=directory,
            runtime_directory=tmp_path / "runtime", measured_recipe=recipe_path,
            observation_profile="batch-extended", pair_closing=True)
    assert not directory.exists()
