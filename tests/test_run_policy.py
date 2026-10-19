"""Immutable policy, owner-bound eligibility and conservative decision precedence."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
from dataclasses import FrozenInstanceError

import pytest

from evidence_wiki.run_policy import (
    CONTINUATION_OUTCOMES,
    DECISION,
    EFFECTS,
    MAX_BYTES,
    MAX_DEPTH,
    MAX_NODES,
    OBSERVATION,
    OBSERVATION_V2,
    POLICY,
    POLICY_V2,
    POLICY_V3,
    POLICY_V4,
    TERMINAL_OUTCOMES,
    RunPolicy,
    decide,
    decode_document,
    encode_document,
    evaluate,
    schema_document,
    schemas,
    validate_document,
)
from evidence_wiki.workflow_contracts import WorkflowContractError

FLAGS = ("source_current", "source_present", "mechanical_pass", "review_pass", "qualified", "context_complete")


def policy_document():
    return {
        "schema_version": POLICY, "policy_id": None, "objective": "  Compare α and β.\r\nKeep the exact original.  ",
        "scope": {"question_ids": ["question-a", "question-b"], "source_scope": ["source-a", "source-b"]},
        "allowed_effects": list(EFFECTS),
        "host_capability_refs": {effect: f"configured-{effect}" for effect in EFFECTS if effect != "offline_check"},
        "assurance": "artifact_checked", "human_review": False,
        "required_evaluators": [{"role": "evaluator", "profile_ref": "profile-a", "controller_ref": "controller-a", "human_required": False}],
        "source_rules": {"require_current": True, "require_present": True, "require_complete_context": True, "allow_partial": True},
        "thresholds": {"critical_false_acceptances_max": 0, "min_reference_cases": 100, "min_families": 10,
                       "positive_acceptance_min": "0.90", "meaning_preserving_flip_max": "0.05"},
        "budgets": {"seconds": 300, "model_calls": 0, "tool_calls": 50, "tokens": 0, "cost_limit": "0",
                    "questions": 5, "claims": 20, "sources": 20},
        "continuation_limits": {"repair_cycles": 2, "adjudication_cycles": 1, "retry_attempts": 2, "acquisition_attempts": 2},
        "terminal_outcomes": list(TERMINAL_OUTCOMES), "continuation_outcomes": list(CONTINUATION_OUTCOMES),
    }


def evaluator(profile=None):
    profile = profile or policy_document()["required_evaluators"][0]
    return {
        **{key: profile[key] for key in ("role", "profile_ref", "controller_ref", "human_required")},
        "qualification_ref": "qualification-receipt", "qualified": True, "independent": True,
        "assurance": "artifact_checked", "reference_cases": 100, "families": 10, "critical_false_acceptances": 0,
        "positive_acceptance": "0.90", "meaning_preserving_flip": "0.05",
    }


def observation(policy=None, selected_count=2):
    policy = policy or RunPolicy(policy_document())
    frozen = policy.to_dict()
    selected = [{"question_id": question, "outcome_ref": f"outcome-{index}", "basis_id": "basis-a", "claim_refs": [f"claim-{index}"],
                 "source_refs": ["source-a"], "independently_eligible": True, **dict.fromkeys(FLAGS, True)}
                for index, question in enumerate(frozen["scope"]["question_ids"][:selected_count])]
    return {
        "schema_version": OBSERVATION, "policy_id": policy.policy_id, "basis_id": "basis-a", "expected_basis_id": "basis-a",
        "observation_kind": "canonical_owner", "owner_ref": "canonical-owner", "receipt_ref": "owner-receipt",
        "policy_compatible": True, "authority_granted": True, "model_required": False, "model_granted": True,
        "acquisition_granted": True, "critical_disagreement": False, "transient_failure": False, **dict.fromkeys(FLAGS, True),
        "original_count": len(frozen["scope"]["question_ids"]), "accepted_count": len(selected),
        "unresolved_count": len(frozen["scope"]["question_ids"]) - len(selected),
        "selected_outcomes": selected, "evaluators": [evaluator(row) for row in frozen["required_evaluators"]],
        "spent": {"seconds": 0, "model_calls": 0, "tool_calls": 0, "tokens": 0, "cost_limit": "0", "claims": 2, "sources": 1,
                  "repair_cycles": 0, "adjudication_cycles": 0, "retry_attempts": 0, "acquisition_attempts": 0},
    }


def outcome(policy, observed):
    result = policy.decide(observed)
    assert result["authorized"] is False and result["evidence_accepted"] is False
    assert result["terminal"] is (result["outcome"] in TERMINAL_OUTCOMES)
    assert decode_document(DECISION, encode_document(DECISION, result)) == result
    if result["outcome"] not in {"accepted", "partial"}:
        assert result["eligible_count"] == 0
        assert result["selected_outcome_refs"] == []
    return result["outcome"], result["reason"]


def test_policy_identity_immutable_nested_views_and_exact_roundtrip():
    source = policy_document()
    original = copy.deepcopy(source)
    policy = RunPolicy.from_dict(source)
    assert source == original
    frozen = policy.to_dict()
    canonical = json.dumps({key: value for key, value in frozen.items() if key != "policy_id"},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert policy.policy_id == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert frozen["objective"] == original["objective"]
    assert RunPolicy.from_json(policy.to_json()).to_dict() == frozen
    source["thresholds"]["positive_acceptance_min"] = "0"
    source["allowed_effects"].clear()
    frozen["scope"]["question_ids"].clear()
    assert policy.to_dict()["thresholds"]["positive_acceptance_min"] == "0.90"
    assert len(policy.to_dict()["scope"]["question_ids"]) == 2
    assert not hasattr(policy, "__dict__")
    with pytest.raises(FrozenInstanceError):
        policy._raw = b"{}"


def test_schema_catalog_detached_and_supplied_identity_cannot_relabel_policy():
    assert set(schemas()) == {POLICY, POLICY_V2, POLICY_V3, POLICY_V4, OBSERVATION, OBSERVATION_V2, DECISION}
    shape = schema_document(POLICY)
    shape["properties"]["schema_version"]["const"] = "invented"
    assert schema_document(POLICY)["properties"]["schema_version"]["const"] == POLICY
    policy = RunPolicy(policy_document())
    changed = policy.to_dict()
    changed["budgets"]["model_calls"] = 1
    with pytest.raises(WorkflowContractError) as exc:
        RunPolicy(changed)
    assert exc.value.error_code == "RUN_POLICY_IDENTITY_MISMATCH"
    changed["policy_id"] = None
    assert RunPolicy(changed).policy_id != policy.policy_id


def test_offline_acceptance_partial_and_no_outcome_are_inert():
    policy = RunPolicy(policy_document())
    assert outcome(policy, observation(policy)) == ("accepted", "current_outcomes")
    assert outcome(policy, observation(policy, 1)) == ("partial", "current_outcomes")
    assert outcome(policy, observation(policy, 0)) == ("abstained", "current_outcomes")
    result = policy.decide(observation(policy, 1))
    assert result["eligible_count"] == 1 and result["unresolved_count"] == 1
    no_partial = policy.to_dict()
    no_partial["source_rules"]["allow_partial"] = False
    no_partial["policy_id"] = None
    stricter = RunPolicy(no_partial)
    assert outcome(stricter, observation(stricter, 1)) == ("abstained", "partial_disallowed")


@pytest.mark.parametrize("update,expected", [
    ({"policy_compatible": False}, ("policy_incompatible", "human_review_required")),
    ({"authority_granted": False}, ("blocked_external", "authority_unavailable")),
    ({"qualified": None}, ("blocked_external", "qualification_unknown")),
    ({"source_current": False}, ("revise", "basis_changed")),
    ({"mechanical_pass": False}, ("revise", "mechanical_failure")),
    ({"review_pass": False}, ("revise", "unsupported_claim")),
    ({"critical_disagreement": True}, ("revise", "review_disagreement")),
    ({"source_present": False}, ("acquire_more", "insufficient_evidence")),
    ({"context_complete": False}, ("acquire_more", "insufficient_evidence")),
    ({"transient_failure": True}, ("retry", "executor_unavailable")),
])
def test_explicit_decision_precedence(update, expected):
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed.update(update)
    assert outcome(policy, observed) == expected


@pytest.mark.parametrize("field", tuple(observation()))
def test_every_missing_observation_field_refuses_before_success(field):
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    del observed[field]
    assert outcome(policy, observed) == ("rejected", "invalid_input")


def test_counts_only_model_assertions_unknown_fields_and_wrong_policy_refuse():
    policy = RunPolicy(policy_document())
    assert outcome(policy, {"accepted_count": 2}) == ("rejected", "invalid_input")
    observed = observation(policy)
    observed["observation_kind"] = "agent_assertion"
    assert outcome(policy, observed) == ("rejected", "untrusted_observation")
    for location in (None, "spent", "selected_outcomes", "evaluators"):
        observed = observation(policy)
        target = observed if location is None else observed[location]
        if type(target) is list:
            target = target[0]
        target["hidden_success"] = True
        assert outcome(policy, observed) == ("rejected", "invalid_input")
    observed = observation(policy)
    observed["policy_id"] = "sha256:" + "a" * 64
    assert outcome(policy, observed) == ("rejected", "policy_binding")
    observed = observation(policy)
    observed["selected_outcomes"].clear()
    assert outcome(policy, observed) == ("rejected", "scope_accounting")


@pytest.mark.parametrize("field,value", [
    ("question_id", "outside-question"), ("source_refs", ["outside-source"]),
    ("outcome_ref", "outcome-1"),
])
def test_scope_and_unique_selected_receipts_are_not_inferred_from_counts(field, value):
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["selected_outcomes"][0][field] = value
    assert outcome(policy, observed) == ("rejected", "scope_accounting")


@pytest.mark.parametrize("flag", FLAGS + ("independently_eligible",))
@pytest.mark.parametrize("value", [False, None])
def test_partial_cannot_preserve_a_selected_outcome_without_individual_eligibility(flag, value):
    policy = RunPolicy(policy_document())
    observed = observation(policy, 1)
    observed["selected_outcomes"][0][flag] = value
    result = policy.decide(observed)
    assert result["outcome"] not in {"accepted", "partial"}
    assert result["eligible_count"] == 0


def test_basis_change_and_selected_stale_receipt_invalidate_counts():
    policy = RunPolicy(policy_document())
    for target in ("expected_basis_id", "selected_outcomes"):
        observed = observation(policy)
        if target == "selected_outcomes":
            observed[target][0]["basis_id"] = "old-basis"
        else:
            observed[target] = "new-basis"
        assert outcome(policy, observed) == ("revise", "basis_changed")
        observed["spent"]["repair_cycles"] = 2
        assert outcome(policy, observed) == ("abstained", "basis_changed")


@pytest.mark.parametrize("counter,flag,exhausted", [
    ("repair_cycles", "mechanical_pass", ("rejected", "mechanical_failure")),
    ("adjudication_cycles", "critical_disagreement", ("abstained", "review_disagreement")),
    ("retry_attempts", "transient_failure", ("blocked_external", "executor_unavailable")),
    ("acquisition_attempts", "source_present", ("abstained", "insufficient_evidence")),
])
def test_every_continuation_is_capped_without_resetting_other_counters(counter, flag, exhausted):
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed[flag] = flag in {"critical_disagreement", "transient_failure"}
    observed["spent"][counter] = policy.to_dict()["continuation_limits"][counter]
    assert outcome(policy, observed) == exhausted
    observed["spent"][counter] += 1
    assert outcome(policy, observed) == ("stop_budget", "budget_exhausted")


def test_disagreement_uses_adjudication_not_majority_or_repair_allowance():
    source = policy_document()
    source["required_evaluators"].append({"role": "critic", "profile_ref": "profile-b", "controller_ref": "controller-b", "human_required": False})
    policy = RunPolicy(source)
    observed = observation(policy)
    observed["critical_disagreement"] = True
    observed["spent"]["repair_cycles"] = 2
    result = policy.decide(observed)
    assert (result["outcome"], result["next_effect"]) == ("revise", "adjudicate")
    observed["spent"]["adjudication_cycles"] = 1
    assert outcome(policy, observed) == ("abstained", "review_disagreement")
    observed["evaluators"][1]["qualified"] = False
    assert outcome(policy, observed) == ("blocked_external", "qualification_unknown")


def test_material_disagreement_can_adjudicate_failed_review_after_repair_exhaustion():
    source = policy_document()
    source["required_evaluators"].append({"role": "critic", "profile_ref": "profile-b", "controller_ref": "controller-b", "human_required": False})
    policy = RunPolicy(source)
    observed = observation(policy)
    observed.update(critical_disagreement=True, review_pass=False)
    observed["spent"].update(repair_cycles=2, adjudication_cycles=0)
    result = policy.decide(observed)
    assert (result["outcome"], result["reason"], result["next_effect"]) == ("revise", "review_disagreement", "adjudicate")
    assert result["eligible_count"] == 0 and result["authorized"] is False and result["evidence_accepted"] is False
    observed["spent"]["adjudication_cycles"] = 1
    assert outcome(policy, observed) == ("abstained", "review_disagreement")


def test_adjudication_cannot_override_combined_hard_mechanical_failure():
    source = policy_document()
    source["required_evaluators"].append({"role": "critic", "profile_ref": "profile-b", "controller_ref": "controller-b", "human_required": False})
    policy = RunPolicy(source)
    observed = observation(policy)
    observed.update(critical_disagreement=True, review_pass=False, mechanical_pass=False)
    observed["spent"].update(repair_cycles=2, adjudication_cycles=0)
    result = policy.decide(observed)
    assert (result["outcome"], result["reason"], result["next_effect"]) == ("rejected", "mechanical_failure", None)
    assert result["eligible_count"] == 0
    observed["spent"]["repair_cycles"] = 1
    result = policy.decide(observed)
    assert (result["outcome"], result["reason"], result["next_effect"]) == ("revise", "mechanical_failure", "revise")


@pytest.mark.parametrize("effect,flag", [("acquire", "source_present"), ("revise", "mechanical_pass"),
                                         ("retry", "transient_failure"), ("adjudicate", "critical_disagreement")])
@pytest.mark.parametrize("remove", ["effect", "reference"])
def test_continuations_require_explicit_allowed_effect_and_host_reference(effect, flag, remove):
    source = policy_document()
    if remove == "effect":
        source["allowed_effects"].remove(effect)
    else:
        source["host_capability_refs"][effect] = None
    policy = RunPolicy(source)
    observed = observation(policy)
    observed[flag] = flag in {"critical_disagreement", "transient_failure"}
    result = policy.decide(observed)
    assert result["next_effect"] is None
    assert result["outcome"] in {"abstained", "blocked_external"}


def test_acquisition_observation_does_not_create_missing_capability():
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["source_present"] = False
    observed["acquisition_granted"] = False
    assert outcome(policy, observed) == ("abstained", "acquisition_ungranted")


@pytest.mark.parametrize("field", tuple(observation()["spent"]))
def test_unknown_usage_is_not_zero_and_cannot_approve_or_continue(field):
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["spent"][field] = None
    assert outcome(policy, observed) == ("stop_budget", "usage_unknown")
    assert decode_document(OBSERVATION, encode_document(OBSERVATION, observed))["spent"][field] is None


def test_zero_model_budget_allows_offline_checks_but_required_model_stops():
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["model_granted"] = False
    assert outcome(policy, observed) == ("accepted", "current_outcomes")
    observed["model_required"] = True
    assert outcome(policy, observed) == ("blocked_external", "executor_unavailable")
    observed["model_granted"] = True
    assert outcome(policy, observed) == ("stop_budget", "model_budget_exhausted")
    observed["model_required"] = False
    observed["spent"]["model_calls"] = 1
    assert outcome(policy, observed) == ("stop_budget", "budget_exhausted")


@pytest.mark.parametrize("field,value", [("seconds", 300), ("seconds", 300.000001), ("tool_calls", 50),
                                        ("cost_limit", "0.000001"), ("claims", 21), ("sources", 21), ("tokens", 1)])
def test_known_budget_exhaustion_and_overruns_prevent_acceptance(field, value):
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["spent"][field] = value
    assert outcome(policy, observed) == ("stop_budget", "budget_exhausted")


def test_selected_source_extent_cannot_be_hidden_by_a_lower_usage_count():
    source = policy_document()
    source["budgets"]["sources"] = 1
    policy = RunPolicy(source)
    observed = observation(policy)
    observed["selected_outcomes"][0]["source_refs"] = ["source-a", "source-b"]
    assert observed["spent"]["sources"] == 1
    assert outcome(policy, observed) == ("stop_budget", "budget_exhausted")


def test_selected_claim_extent_cannot_be_hidden_by_zero_observed_claim_usage():
    source = policy_document()
    source["budgets"]["claims"] = 1
    policy = RunPolicy(source)
    observed = observation(policy)
    observed["spent"]["claims"] = 0
    assert len(observed["selected_outcomes"]) == 2
    assert outcome(policy, observed) == ("stop_budget", "budget_exhausted")
    assert decode_document(OBSERVATION, encode_document(OBSERVATION, observed))["spent"]["claims"] == 0


def test_explicit_shared_claim_refs_are_counted_once_and_missing_refs_refuse():
    source = policy_document()
    source["budgets"]["claims"] = 1
    policy = RunPolicy(source)
    observed = observation(policy)
    observed["spent"]["claims"] = 1
    for selected in observed["selected_outcomes"]:
        selected["claim_refs"] = ["shared-claim"]
    assert outcome(policy, observed) == ("accepted", "current_outcomes")
    observed["selected_outcomes"][0]["claim_refs"] = []
    assert outcome(policy, observed) == ("rejected", "invalid_input")
    observed["selected_outcomes"][0]["claim_refs"] = ["shared-claim", "shared-claim"]
    assert outcome(policy, observed) == ("rejected", "invalid_input")


def test_fractional_seconds_and_exact_zero_cost_are_preserved():
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["spent"]["seconds"] = 0.125
    observed["spent"]["cost_limit"] = "0.000000"
    assert outcome(policy, observed) == ("accepted", "current_outcomes")
    assert decode_document(OBSERVATION, encode_document(OBSERVATION, observed))["spent"]["seconds"] == 0.125


@pytest.mark.parametrize("field,value", [("qualified", None), ("independent", False), ("qualification_ref", None),
                                        ("reference_cases", 99), ("families", 9), ("critical_false_acceptances", 1),
                                        ("positive_acceptance", "0.89"), ("meaning_preserving_flip", "0.050001"),
                                        ("controller_ref", "other-controller"), ("assurance", "host_enforced")])
def test_each_required_profile_must_be_current_qualified_and_meet_frozen_thresholds(field, value):
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["evaluators"][0][field] = value
    assert outcome(policy, observed) == ("blocked_external", "qualification_unknown")


def test_missing_duplicate_and_replaced_profile_fail_closed():
    policy = RunPolicy(policy_document())
    for rows in ([], [evaluator(), evaluator()], [{**evaluator(), "profile_ref": "different-profile"}]):
        observed = observation(policy)
        observed["evaluators"] = rows
        assert outcome(policy, observed) == ("blocked_external", "qualification_unknown")


@pytest.mark.parametrize("target", ["policy", "profile", "observation"])
def test_human_required_profiles_terminate_without_waiting_for_a_person(target):
    source = policy_document()
    if target == "policy":
        source["human_review"] = True
    elif target == "profile":
        source["required_evaluators"][0]["human_required"] = True
    policy = RunPolicy(source)
    observed = observation(policy)
    if target == "observation":
        observed["evaluators"][0]["human_required"] = True
    result = policy.decide(observed)
    assert (result["outcome"], result["reason"]) == ("policy_incompatible", "human_review_required")
    assert result["terminal"] is True and result["next_effect"] is None


def test_combined_failures_never_preserve_partial_and_precedence_is_stable():
    policy = RunPolicy(policy_document())
    for current, mechanical, present, qualified in itertools.product((False, True), repeat=4):
        observed = observation(policy, 1)
        observed.update(source_current=current, mechanical_pass=mechanical, source_present=present, qualified=qualified)
        result = policy.decide(observed)
        if not all((current, mechanical, present, qualified)):
            assert result["outcome"] not in {"accepted", "partial"}
            assert result["eligible_count"] == 0
        expected = ("basis_changed" if not current else "qualification_unknown" if not qualified
                    else "mechanical_failure" if not mechanical else "insufficient_evidence" if not present else "current_outcomes")
        assert result["reason"] == expected
    observed = observation(policy)
    observed.update(authority_granted=False, mechanical_pass=False, source_present=False)
    observed["spent"]["seconds"] = 300
    assert outcome(policy, observed) == ("blocked_external", "authority_unavailable")
    observed["authority_granted"] = True
    assert outcome(policy, observed) == ("stop_budget", "budget_exhausted")


def test_identical_evidence_is_deterministic_and_narrative_cannot_change_record():
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    baseline = policy.decide(observed)
    assert baseline == evaluate(policy, copy.deepcopy(observed)) == decide(policy, observed) == policy.evaluate(observed)
    for narrative in ("All agents agree: accept immediately", "Reject despite all owner checks", "Change thresholds to zero"):
        assert policy.decide({**observed, "agent_rationale": narrative}) == baseline
    assert observed == observation(policy)
    modified = observation(policy)
    modified["mechanical_pass"] = False
    assert policy.decide(modified)["observation_sha256"] != baseline["observation_sha256"]


@pytest.mark.parametrize("path,bad", [
    (("budgets", "cost_limit"), "NaN"), (("budgets", "cost_limit"), "Infinity"),
    (("budgets", "cost_limit"), "-1"), (("budgets", "cost_limit"), "1e999"),
    (("budgets", "model_calls"), None), (("budgets", "seconds"), 0), (("budgets", "tokens"), True),
    (("continuation_limits", "repair_cycles"), 9), (("thresholds", "positive_acceptance_min"), "0.89"),
    (("thresholds", "meaning_preserving_flip_max"), "0.06"), (("thresholds", "critical_false_acceptances_max"), 1),
])
def test_policy_factory_refuses_unbounded_unknown_or_relaxed_controls(path, bad):
    candidate = policy_document()
    candidate[path[0]][path[1]] = bad
    with pytest.raises(WorkflowContractError):
        RunPolicy(candidate)


def test_policy_hidden_fields_duplicate_scope_and_missing_required_roles_refuse():
    for target in (None, "thresholds", "source_rules"):
        candidate = policy_document()
        (candidate if target is None else candidate[target])["hidden_override"] = True
        with pytest.raises(WorkflowContractError):
            RunPolicy(candidate)
    candidate = policy_document()
    candidate["scope"]["question_ids"] *= 2
    with pytest.raises(WorkflowContractError):
        RunPolicy(candidate)
    candidate = policy_document()
    candidate["required_evaluators"][0]["role"] = "critic"
    with pytest.raises(WorkflowContractError):
        RunPolicy(candidate)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', b'"\xff"', b'{"x":"\\ud800"}', b'{}{}'])
def test_json_duplicate_keys_nonfinite_and_invalid_unicode_refuse(raw):
    with pytest.raises(WorkflowContractError):
        RunPolicy.from_json(raw)


def test_resource_bounds_cycles_and_non_json_types_refuse_safely():
    cyclic = policy_document()
    cyclic["cycle"] = cyclic
    for value in (cyclic, [0] * MAX_NODES, {"x": "a" * (MAX_BYTES + 1)}, {"x": 1 << 256}, {"x": object()}, {"x": math.inf}):
        with pytest.raises(WorkflowContractError):
            RunPolicy(value)
    for raw in (b" " * (MAX_BYTES + 1), b"[" * (MAX_DEPTH + 1) + b"]" * (MAX_DEPTH + 1)):
        with pytest.raises(WorkflowContractError) as exc:
            decode_document(POLICY, raw)
        assert exc.value.error_code == "RUN_POLICY_LIMIT"
    policy = RunPolicy(policy_document())
    observed = observation(policy)
    observed["agent_rationale"] = "a" * (MAX_BYTES + 1)
    assert outcome(policy, observed) == ("rejected", "invalid_input")


def test_serialized_byte_growth_and_exact_boundary():
    candidate = policy_document()
    candidate["objective"] = "α" * 65_536
    policy = RunPolicy(candidate)
    padded = policy.to_json() + b" " * (MAX_BYTES - len(policy.to_json()))
    assert RunPolicy.from_json(padded).policy_id == policy.policy_id
    with pytest.raises(WorkflowContractError):
        RunPolicy.from_json(padded + b" ")
    candidate["objective"] = '"' * 65_536
    candidate["scope"]["source_scope"] = ['"' * 4090 + str(index) for index in range(64)]
    with pytest.raises(WorkflowContractError) as exc:
        RunPolicy(candidate)
    assert exc.value.error_code == "RUN_POLICY_LIMIT"


def test_decision_envelope_cannot_be_promoted_to_authority():
    policy = RunPolicy(policy_document())
    result = policy.decide(observation(policy))
    for key in ("authorized", "evidence_accepted"):
        candidate = {**result, key: True}
        with pytest.raises(WorkflowContractError):
            validate_document(DECISION, candidate)


@pytest.mark.parametrize("faults,expected", [
    ("scope_human", ("rejected", "scope_accounting")),
    ("human_authority", ("policy_incompatible", "human_review_required")),
    ("grant_unknown", ("blocked_external", "executor_unavailable")),
    ("unknown_budget", ("stop_budget", "usage_unknown")),
    ("budget_stale", ("stop_budget", "budget_exhausted")),
])
def test_hard_multifault_precedence_preserves_frozen_policy_bytes(faults, expected):
    policy = RunPolicy(policy_document())
    before = policy.to_json()
    observed = observation(policy)
    if faults == "scope_human":
        observed["selected_outcomes"][0]["source_refs"] = ["outside-source"]
        observed["policy_compatible"] = False
    elif faults == "human_authority":
        observed.update(policy_compatible=False, authority_granted=False)
    elif faults == "grant_unknown":
        observed.update(model_required=True, model_granted=False)
        observed["spent"]["seconds"] = None
    elif faults == "unknown_budget":
        observed["spent"].update(tokens=None, seconds=policy.to_dict()["budgets"]["seconds"] + 1)
    else:
        observed["spent"]["seconds"] = policy.to_dict()["budgets"]["seconds"] + 1
        observed.update(source_current=False, expected_basis_id="changed-basis")
    assert outcome(policy, observed) == expected
    assert policy.to_json() == before
    assert RunPolicy.from_json(before).to_json() == before
