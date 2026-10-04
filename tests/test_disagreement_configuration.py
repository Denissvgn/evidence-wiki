"""Optional investigation never becomes a mandatory or approving first pass."""

import copy
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from evidence_wiki.agent_execution import ExecutionError, ExecutionProfile, ExecutionRole
from evidence_wiki.calibration_registry import CalibrationPlan, CalibrationSelection
from evidence_wiki.disagreement_configuration import DisagreementPlan, auxiliary_observation
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.qualification_calibration import calibration_suite
from evidence_wiki.reference_oracles import DOMAIN, protected_cases
from tests.test_disagreement import selected_policy
from tests.test_review_context import inputs


def roles():
    return (ExecutionRole("generator", "host_worker", "generator-controller", "generator-model", "generate/v1"),
        ExecutionRole("profile-a", "evaluator", "controller-a", "evidence-model", "review/v1"),
        ExecutionRole("critic", "evaluator", "critic-controller", "critic-model", "review/v1"))


def scopes(extra=()):
    return ExecutionRoleRegistry((ExecutionRoleScope("generator", "researcher", ("model",)),
        ExecutionRoleScope("profile-a", "evaluator", ("model",)),
        ExecutionRoleScope("critic", "critic", ("model",)), *extra))


def simple_profile():
    return ExecutionProfile("scoped-profile", 100, "EUR", "free-native", roles(), 1, 0, 1, "0", role_scopes=scopes())


@pytest.fixture(scope="module")
def auxiliary_profile():
    rubric = inputs()["rubric"]
    suite = calibration_suite(protected_cases(), suite_ref="protected", split_ref="held-out", domains=[DOMAIN],
        rubric=rubric, model_settings={"temperature": "0"})
    calibration = CalibrationPlan(tuple(CalibrationSelection(config, suite, model_revision="actual-revision")
        for config in ("profile-a", "critic", "investigator")))
    optional = ExecutionRole("investigator", "evaluator", "investigator-controller", "investigator-model", "investigate/v1")
    profile = ExecutionProfile("auxiliary-profile", 100, "EUR", "declared-model-price", (*roles(), optional),
        1, 0, 1, "0", role_scopes=scopes((ExecutionRoleScope("investigator", "adjudicator", ("model",)),)),
        calibration=calibration)
    return profile


def test_default_plan_enables_bounded_repair_on_existing_scoped_profile():
    plan = DisagreementPlan()
    profile = simple_profile()
    plan.check_profile(profile)
    assert plan.auxiliary_profile(selected_policy(), profile) is None
    assert plan.document() == {"schema_version": "research-disagreement-plan/v1", "adjudicator_config_ref": None,
                               "claim_repair_limit": 2, "claim_adjudication_limit": 1}
    assert plan.sha256 == DisagreementPlan().sha256
    with pytest.raises(FrozenInstanceError):
        plan.adjudicator_config_ref = "other"


def test_optional_investigator_is_separate_from_required_firstpasses(auxiliary_profile):
    plan = DisagreementPlan("investigator")
    policy = selected_policy()
    before = policy.to_dict()
    row = plan.auxiliary_profile(policy, auxiliary_profile)
    assert row == {"role": "adjudicator", "profile_ref": "investigator", "controller_ref": "investigator-controller", "human_required": False}
    assert policy.to_dict() == before
    assert {item["role"] for item in policy.to_dict()["required_evaluators"]} == {"evaluator", "critic"}
    assert plan.sha256 != DisagreementPlan().sha256


@pytest.mark.parametrize("reference", ["", "bad ref", True, [], 1])
def test_optional_config_reference_requires_exact_typed_selection(reference):
    with pytest.raises(ExecutionError):
        DisagreementPlan(reference)


def test_optional_investigator_cannot_use_legacy_qualification_or_required_reviewer(auxiliary_profile):
    plan = DisagreementPlan("profile-a")
    with pytest.raises(ExecutionError, match="calibration_required"):
        plan.check_profile(simple_profile())
    with pytest.raises(ExecutionError, match="role_invalid"):
        plan.check_profile(auxiliary_profile)
    document = selected_policy().to_dict()
    document["policy_id"] = None
    document["required_evaluators"].append({"role": "adjudicator", "profile_ref": "investigator",
        "controller_ref": "investigator-controller", "human_required": False})
    from evidence_wiki.run_policy import RunPolicy

    with pytest.raises(ExecutionError, match="is_required"):
        DisagreementPlan("investigator").auxiliary_profile(RunPolicy(document), auxiliary_profile)


def test_shared_auxiliary_controller_is_refused(auxiliary_profile):
    changed_roles = (*auxiliary_profile.roles[:-1], replace(auxiliary_profile.roles[-1], controller_ref="controller-a"))
    profile = replace(auxiliary_profile, roles=changed_roles)
    with pytest.raises(ExecutionError, match="controller_shared"):
        DisagreementPlan("investigator").check_profile(profile)


def test_auxiliary_projection_requests_only_explicit_selected_route(auxiliary_profile):
    selected = {"role": "adjudicator", "profile_ref": "investigator", "controller_ref": "investigator-controller", "human_required": False}
    calls = []
    def observations(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return [{**selected, "qualified": True, "independent": True, "qualification_ref": "protected-reference"}]
    registry = SimpleNamespace(observations=observations)
    plan = DisagreementPlan("investigator")
    result = auxiliary_observation(plan, registry, selected_policy(), auxiliary_profile,
        "2026-10-01T12:00:00Z", "rubric", rubric_sha256="a" * 64, rubric={}, domain={}, probe={})
    assert result["state"] == "qualified" and result["qualified"] is True
    assert calls[0]["selected_profiles"] == [selected] and calls[0]["auxiliary"] is True
    assert not result["native_review_allowed"] and not result["authorized"] and not result["evidence_accepted"]


@pytest.mark.parametrize("rows", [[], [{"role": "evaluator", "profile_ref": "profile-a", "controller_ref": "controller-a",
    "human_required": False, "qualified": True, "independent": True}],
    [{"role": "adjudicator", "profile_ref": "investigator", "controller_ref": "investigator-controller",
    "human_required": False, "qualified": True, "independent": None}]])
def test_missing_legacy_or_unqualified_projection_cannot_promote_auxiliary(rows, auxiliary_profile):
    registry = SimpleNamespace(observations=lambda *_args, **_kwargs: rows)
    result = auxiliary_observation(DisagreementPlan("investigator"), registry, selected_policy(), auxiliary_profile,
        "2026-10-01T12:00:00Z", "rubric", rubric_sha256="a" * 64, rubric={}, domain={}, probe={})
    assert result["state"] == "unknown" and result["qualified"] is None


def test_no_optional_selection_does_not_query_or_poison_firstpass_registry():
    def unexpected(*args, **kwargs):
        pytest.fail("No auxiliary was configured.")
    result = auxiliary_observation(DisagreementPlan(), SimpleNamespace(observations=unexpected), selected_policy(),
        simple_profile(), "2026-10-01T12:00:00Z", "rubric", rubric_sha256="a" * 64, rubric={}, domain={}, probe={})
    assert result["state"] == "unavailable" and result["reason"] == "not_configured"
