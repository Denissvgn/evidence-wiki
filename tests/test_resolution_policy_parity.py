"""Shared hard rules keep whole-original accounting unchanged for native subsets."""

import copy

import pytest

from evidence_wiki.run_policy import RunPolicy, _hard_policy_failure, _owner_observation
from tests.test_run_policy import observation, policy_document


@pytest.mark.parametrize("change,outcome,reason", [
    ("binding", "rejected", "policy_binding"), ("assertion", "rejected", "untrusted_observation"),
    ("duplicate_reference", "rejected", "scope_accounting"), ("foreign_source", "rejected", "scope_accounting"),
    ("authority", "blocked_external", "authority_unavailable"), ("model_grant", "blocked_external", "executor_unavailable"),
    ("unknown", "stop_budget", "usage_unknown"), ("models_equal", "stop_budget", "model_budget_exhausted"),
    ("tokens_equal", "stop_budget", "model_budget_exhausted"), ("tools_equal", "stop_budget", "budget_exhausted"),
    ("seconds_equal", "stop_budget", "budget_exhausted")])
def test_same_native_hard_failure_is_consumed_by_original_and_subset_owner(change, outcome, reason):
    raw = policy_document()
    raw["budgets"].update(model_calls=4, tokens=100)
    policy = RunPolicy(raw)
    value = observation(policy)
    if change == "binding":
        value["policy_id"] = "sha256:" + "0" * 64
    elif change == "assertion":
        value["observation_kind"] = "agent_assertion"
    elif change == "duplicate_reference":
        value["selected_outcomes"][1]["outcome_ref"] = value["selected_outcomes"][0]["outcome_ref"]
    elif change == "foreign_source":
        value["selected_outcomes"][0]["source_refs"] = ["outside-scope"]
    elif change == "authority":
        value["authority_granted"] = False
    elif change == "model_grant":
        value.update(model_required=True, model_granted=False)
    elif change == "unknown":
        value["spent"]["tokens"] = None
    else:
        key = {"models_equal": "model_calls", "tokens_equal": "tokens", "tools_equal": "tool_calls", "seconds_equal": "seconds"}[change]
        value["spent"][key] = policy.to_dict()["budgets"][key]
        if key in {"model_calls", "tokens"}:
            value["model_required"] = True
    before = copy.deepcopy(value)
    actual = _owner_observation(policy.to_dict(), value)
    assert _hard_policy_failure(policy.to_dict(), actual) == (outcome, reason)
    decision = policy.decide(value)
    assert (decision["outcome"], decision["reason"]) == (outcome, reason)
    assert value == before


def test_hard_predicate_does_not_create_selected_or_accepted_originals():
    policy = RunPolicy(policy_document())
    value = observation(policy, selected_count=0)
    before = copy.deepcopy(value)
    assert _hard_policy_failure(policy.to_dict(), _owner_observation(policy.to_dict(), value)) is None
    assert value == before and value["accepted_count"] == 0 and value["selected_outcomes"] == []
