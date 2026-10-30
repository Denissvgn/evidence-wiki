"""Finite escalation and refusal to promote consensus over canonical failures."""

import copy

import pytest

from evidence_wiki.disagreement import CRITERIA, disagreement_action
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_run_policy import policy_document

A, B, C = ["sha256:" + value * 64 for value in ("a", "b", "c")]


def selected_policy(**changes):
    supplied = policy_document()
    supplied["budgets"].update(model_calls=100, tokens=10000)
    supplied["required_evaluators"].append({"role": "critic", "profile_ref": "critic", "controller_ref": "critic-controller", "human_required": False})
    supplied.update(changes)
    return RunPolicy(supplied)


def gates(**changes):
    return dict(authentication="pass", accounting="known", mechanical="pass", context_complete=True,
        source_present=True, qualified=True, authority_granted=True, budget_available=True, model_available=True,
        repair_available=True, adjudicator_available=True, acquisition_available=False, source_gap_ref=None,
        run_repair_cycles=0, run_adjudication_cycles=0, run_acquisition_attempts=0) | changes


def reviews(*, basis=A, critic="fail", evidence="pass"):
    result = []
    for role, profile, controller, verdict in (("evaluator", "profile-a", "controller-a", evidence),
        ("critic", "critic", "critic-controller", critic)):
        values = dict.fromkeys(sorted(CRITERIA), "pass")
        values["support"] = verdict
        result.append(dict(review_ref="review-" + role, profile_ref=profile, role=role, controller_ref=controller,
            basis_id=basis, verdicts=values, objection_refs=[]))
    return result


def select(**changes):
    supplied = dict(policy=selected_policy(), claim_id="claim", basis_id=A, generator_controller_ref="generator",
        reviews=reviews(), history=[], gates=gates()) | changes
    return disagreement_action(**supplied)


def event(action, *, index, basis=A, result=None):
    return dict(action_ref=f"action-{index}", claim_id="claim", basis_id=basis, action=action["operation"],
        defect_sha256=action["defect_sha256"], result_basis_id=result)


def test_disagreement_is_one_investigation_then_two_material_repairs_at_most():
    first = select()
    assert first["operation"] == "adjudicate" and first["adjudication_limit"] == 1
    history = [event(first, index=1)]
    second = select(history=history, gates=gates(run_adjudication_cycles=1))
    assert second["operation"] == "repair" and second["requires_new_basis"] and second["requires_fresh_first_passes"]
    history.append(event(second, index=2, result=B))
    third = select(basis_id=B, reviews=reviews(basis=B), history=history,
        gates=gates(run_adjudication_cycles=1, run_repair_cycles=1))
    assert third["operation"] == "repair"
    history.append(event(third, index=3, basis=B, result=C))
    final = select(basis_id=C, reviews=reviews(basis=C), history=history,
        gates=gates(run_adjudication_cycles=1, run_repair_cycles=2))
    assert final["terminal"] and final["outcome"] == "abstained"
    assert final["repairs_used"] == 2 and final["adjudications_used"] == 1
    completed = select(basis_id=C, reviews=reviews(basis=C, critic="pass"), history=history,
        gates=gates(run_adjudication_cycles=1, run_repair_cycles=2))
    assert completed["operation"] == "return_gate" and completed["outcome"] is None
    assert completed["authorized"] is False and completed["evidence_accepted"] is False


def test_adjudication_and_changed_votes_cannot_approve_unchanged_failed_material():
    first = select()
    subsequent = select(reviews=reviews(critic="pass"), history=[event(first, index=1)],
        gates=gates(run_adjudication_cycles=1))
    assert subsequent["operation"] == "repair" and subsequent["requires_new_basis"]
    refused = select(reviews=reviews(critic="pass"), history=[event(first, index=1)],
        gates=gates(run_adjudication_cycles=1, repair_available=False))
    assert refused["outcome"] == "abstained" and refused["detail"] == "adjudication_cannot_approve_original_basis"


def test_no_change_and_basis_oscillation_stop_even_when_fresh_votes_pass():
    proposed = select(gates=gates(adjudicator_available=False))
    unchanged = select(reviews=reviews(critic="pass"), history=[event(proposed, index=1, result=A)],
        gates=gates(run_repair_cycles=1))
    assert unchanged["terminal"] and unchanged["detail"] == "repair_did_not_change_material"
    history = [event(proposed, index=1, result=B), event(proposed, index=2, basis=B, result=A)]
    cycled = select(reviews=reviews(critic="pass"), history=history, gates=gates(run_repair_cycles=2))
    assert cycled["terminal"] and cycled["detail"] == "repair_basis_oscillation"


@pytest.mark.parametrize("kind,expected", [("fail", "repair"), ("unknown", "terminal")])
def test_unanimous_model_passes_cannot_override_deterministic_failures(kind, expected):
    result = select(reviews=reviews(critic="pass"), gates=gates(mechanical=kind))
    assert result["operation"] == expected and result["reason"] == "mechanical_failure"
    assert not result["authorized"] and not result["evidence_accepted"]
    blocked = select(reviews=reviews(critic="pass"), gates=gates(mechanical=kind, repair_available=False))
    assert blocked["terminal"] and blocked["outcome"] in {"rejected", "abstained"}


@pytest.mark.parametrize("values,outcome,reason", [({"authentication": "fail"}, "rejected", "untrusted_observation"),
    ({"authentication": "unknown"}, "blocked_external", "untrusted_observation"),
    ({"accounting": "unknown"}, "stop_budget", "usage_unknown"),
    ({"accounting": "overrun"}, "stop_budget", "budget_exhausted"),
    ({"budget_available": False}, "stop_budget", "budget_exhausted"),
    ({"run_repair_cycles": None}, "stop_budget", "usage_unknown"),
    ({"authority_granted": False}, "blocked_external", "authority_unavailable"),
    ({"qualified": False}, "blocked_external", "qualification_unknown")])
def test_hard_unknown_failure_or_allowance_cannot_be_resolved_by_consensus(values, outcome, reason):
    result = select(reviews=reviews(critic="pass"), gates=gates(**values))
    assert result["terminal"] and result["outcome"] == outcome and result["reason"] == reason


def test_required_unknown_does_not_trigger_adjudication_or_guessed_pass():
    result = select(reviews=reviews(critic="unknown"))
    assert result["outcome"] == "abstained" and result["detail"] == "required_review_unknown"
    acquisition = select(reviews=reviews(critic="unknown"),
        gates=gates(acquisition_available=True, source_gap_ref="retained-gap"))
    assert acquisition["operation"] == "acquire" and acquisition["required_effects"] == ["acquire"]


@pytest.mark.parametrize("available,gap,permitted", [(False, "gap", True), (True, None, True), (True, "gap", False)])
def test_source_acquisition_requires_exact_gap_real_owner_and_existing_policy(available, gap, permitted):
    policy = selected_policy()
    if not permitted:
        supplied = policy.to_dict()
        supplied["allowed_effects"].remove("acquire")
        supplied["policy_id"] = None
        policy = RunPolicy(supplied)
    result = select(policy=policy, gates=gates(context_complete=False, acquisition_available=available, source_gap_ref=gap))
    assert result["terminal"] and result["outcome"] == "abstained"


def test_missing_context_can_use_an_authorized_owner_before_reviewer_qualification():
    result = select(gates=gates(context_complete=False, qualified=False, acquisition_available=True, source_gap_ref="retained-gap"))
    assert result["operation"] == "acquire" and result["requires_new_basis"]
    assert result["requires_fresh_first_passes"] and result["source_gap_ref"] == "retained-gap"


def test_missing_or_stale_first_passes_request_only_fresh_bounded_work():
    result = select(basis_id=B, reviews=reviews())
    assert result["operation"] == "collect_first_passes" and not result["review_refs"]
    unavailable = select(basis_id=B, reviews=reviews(), gates=gates(model_available=False))
    assert unavailable["terminal"] and unavailable["outcome"] == "blocked_external"


def test_default_claim_limits_remain_below_more_permissive_run_limits():
    document = selected_policy().to_dict()
    document["policy_id"] = None
    document["continuation_limits"].update(repair_cycles=8, adjudication_cycles=4)
    result = select(policy=RunPolicy(document))
    assert result["repair_limit"] == 2 and result["adjudication_limit"] == 1
    document["continuation_limits"].update(repair_cycles=0, adjudication_cycles=0)
    exhausted = select(policy=RunPolicy(document))
    assert exhausted["terminal"] and exhausted["outcome"] == "abstained"


def test_spent_other_claim_allowance_and_counter_regression_remain_visible():
    exhausted = select(gates=gates(run_repair_cycles=2, run_adjudication_cycles=1))
    assert exhausted["terminal"]
    prior = select(gates=gates(adjudicator_available=False))
    regressed = select(history=[event(prior, index=1, result=B)])
    assert regressed["outcome"] == "rejected" and regressed["detail"] == "continuation_counter_regressed"
    overrun = select(gates=gates(run_repair_cycles=3))
    assert overrun["outcome"] == "stop_budget"


@pytest.mark.parametrize("mutate", [lambda values: values[0].update(controller_ref="generator"),
    lambda values: values[0].update(role="critic"), lambda values: values[0].update(profile_ref="unselected"),
    lambda values: values[0]["verdicts"].update(support="certain-pass"),
    lambda values: values[0].update(signing_key="inert-injection"),
    lambda values: values[0]["verdicts"].pop("counterevidence")])
def test_forged_identity_role_verdict_and_hidden_authority_are_rejected(mutate):
    values = reviews()
    mutate(values)
    assert select(reviews=values)["outcome"] == "rejected"


def test_duplicate_replies_do_not_add_roles_or_erase_material_objections():
    values = reviews()
    duplicated = select(reviews=values + [copy.deepcopy(values[0])])
    assert duplicated["operation"] == "adjudicate" and len(duplicated["review_refs"]) == 2
    changed = copy.deepcopy(values[0])
    changed["verdicts"]["support"] = "fail"
    assert select(reviews=values + [changed])["outcome"] == "rejected"
    changed["review_ref"] = "another-review"
    conflict = select(reviews=values + [changed])
    assert conflict["outcome"] == "abstained" and conflict["detail"] == "conflicting_first_passes_from_one_profile"
    passing = reviews(critic="pass")
    passing[1]["objection_refs"] = ["material-objection"]
    assert select(reviews=passing)["operation"] == "repair"


def test_unobserved_repair_completion_does_not_refund_or_create_another_attempt():
    first = select(gates=gates(adjudicator_available=False))
    result = select(history=[event(first, index=1)], gates=gates(run_repair_cycles=1))
    assert result["terminal"] and result["detail"] == "repair_completion_unobserved"


def test_human_required_policy_is_incompatible_without_an_interaction_state():
    result = select(policy=selected_policy(human_review=True))
    assert result["terminal"] and result["outcome"] == "policy_incompatible"
    assert "human" not in result["operation"]


def test_no_first_pass_profiles_cannot_create_an_unfulfillable_collection_loop():
    document = policy_document()
    document["required_evaluators"] = [{"role": "adjudicator", "profile_ref": "judge",
        "controller_ref": "judge-controller", "human_required": False}]
    with pytest.raises(WorkflowContractError):
        RunPolicy(document)


def test_one_legacy_evaluator_cannot_replace_independent_counterevidence():
    result = select(policy=RunPolicy(policy_document()), reviews=reviews(critic="pass")[:1])
    assert result["terminal"] and result["outcome"] == "blocked_external"
    assert result["detail"] == "first_pass_profiles_unavailable"


def test_shared_configured_controllers_cannot_create_fresh_independent_first_passes():
    document = selected_policy().to_dict()
    document["policy_id"] = None
    document["required_evaluators"][1]["controller_ref"] = "controller-a"
    result = select(policy=RunPolicy(document), reviews=[])
    assert result["terminal"] and result["outcome"] == "rejected"
    assert result["detail"] == "first_pass_controller_configuration_invalid"


@pytest.mark.parametrize("kind,count", [("repair", 3), ("adjudicate", 2)])
def test_claim_history_overrun_is_terminal_even_if_new_reviews_all_pass(kind, count):
    document = selected_policy().to_dict()
    document["policy_id"] = None
    document["continuation_limits"].update(repair_cycles=8, adjudication_cycles=4)
    action = select()
    history = [event(action | {"operation": kind}, index=index, basis=A, result=B) for index in range(count)]
    values = {"run_repair_cycles": count} if kind == "repair" else {"run_adjudication_cycles": count}
    result = select(policy=RunPolicy(document), basis_id=B, reviews=reviews(basis=B, critic="pass"),
        history=history, gates=gates(**values))
    assert result["terminal"] and result["outcome"] == "stop_budget"
    assert result["detail"] == "claim_continuation_allowance_overrun"


@pytest.mark.parametrize("values", [{"model_available": 1}, {"run_repair_cycles": True},
    {"run_adjudication_cycles": -1}, {"run_acquisition_attempts": float("nan")},
    {"accounting": []}, {"source_gap_ref": "outside scope ref"}])
def test_malformed_gate_states_cannot_manufacture_progress_or_permissions(values):
    assert select(gates=gates(**values))["outcome"] == "rejected"


def test_replay_order_preserves_defect_identity_and_never_mutates_inputs():
    supplied = reviews()
    before = copy.deepcopy(supplied)
    first = select(reviews=supplied)
    second = select(reviews=list(reversed(supplied)))
    assert first == second and supplied == before
    assert first["required_effects"] == ["model", "adjudicate"]
    assert not first["authorized"] and not first["evidence_accepted"]
