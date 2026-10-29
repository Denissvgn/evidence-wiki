"""Independent scope and inert session contract conformance checks."""

import copy
import json
from pathlib import Path

import pytest

from evidence_wiki import research_session_contracts as session_contracts
from evidence_wiki.run_policy import (
    CONTINUATION_OUTCOMES,
    EFFECTS,
    MAX_BYTES,
    OBSERVATION,
    OBSERVATION_V2,
    POLICY,
    POLICY_V2,
    TERMINAL_OUTCOMES,
    RunPolicy,
    validate_document,
)
from evidence_wiki.workflow_contracts import WorkflowContractError

FLAGS = ("source_current", "source_present", "mechanical_pass", "review_pass", "qualified", "context_complete")
DIGEST = "sha256:" + "a" * 64
NATIVE_SOURCE = str(Path(__file__).resolve().parent / "native-workspace" / "raw" / "measurements.txt")


def native_policy_document():
    return {
        "schema_version": POLICY_V2,
        "policy_id": None,
        "objective": "Compare the exact original local measurements.",
        "scope": {
            "question_ids": ["original-one", "original-two"],
            "source_scope": [NATIVE_SOURCE],
            "source_scope_kind": "native_extent",
        },
        "allowed_effects": list(EFFECTS),
        "host_capability_refs": {effect: "host-" + effect for effect in EFFECTS if effect != "offline_check"},
        "assurance": "artifact_checked",
        "human_review": False,
        "required_evaluators": [{"role": "evaluator", "profile_ref": "local-evaluator", "controller_ref": "isolated-controller", "human_required": False}],
        "source_rules": {"require_current": True, "require_present": True, "require_complete_context": True, "allow_partial": True},
        "thresholds": {"critical_false_acceptances_max": 0, "min_reference_cases": 100, "min_families": 10,
                       "positive_acceptance_min": "0.90", "meaning_preserving_flip_max": "0.05"},
        "budgets": {"seconds": 100, "model_calls": 0, "tool_calls": 10, "tokens": 0, "cost_limit": "0",
                    "questions": 2, "claims": 10, "sources": 10},
        "continuation_limits": {"repair_cycles": 1, "adjudication_cycles": 1, "retry_attempts": 1, "acquisition_attempts": 1},
        "terminal_outcomes": list(TERMINAL_OUTCOMES),
        "continuation_outcomes": list(CONTINUATION_OUTCOMES),
    }


def owner_observation(policy):
    frozen = policy.to_dict()
    return {
        "schema_version": OBSERVATION_V2,
        "policy_id": policy.policy_id,
        "basis_id": "current-owner-basis",
        "expected_basis_id": "current-owner-basis",
        "observation_kind": "canonical_owner",
        "owner_ref": "strict-owner",
        "receipt_ref": "strict-release",
        "policy_compatible": True,
        "authority_granted": True,
        "model_required": False,
        "model_granted": False,
        "acquisition_granted": False,
        "critical_disagreement": False,
        "transient_failure": False,
        **dict.fromkeys(FLAGS, True),
        "original_count": 2,
        "accepted_count": 1,
        "unresolved_count": 1,
        "selected_outcomes": [{"question_id": "original-one", "outcome_ref": "current-result", "basis_id": "current-owner-basis",
                               "claim_refs": ["current-claim"], "source_refs": ["captured-source"],
                               "independently_eligible": True, **dict.fromkeys(FLAGS, True)}],
        "evaluators": [{**frozen["required_evaluators"][0], "qualification_ref": "protected-profile", "qualified": True,
                        "independent": True, "assurance": "artifact_checked", "reference_cases": 100, "families": 10,
                        "critical_false_acceptances": 0, "positive_acceptance": "0.90", "meaning_preserving_flip": "0.05"}],
        "spent": {"seconds": 0.125, "model_calls": 0, "tool_calls": 1, "tokens": 0, "cost_limit": "0",
                  "claims": 1, "sources": 1, "repair_cycles": 0, "adjudication_cycles": 0, "retry_attempts": 0, "acquisition_attempts": 0},
        "source_bindings": [{"source_id": "captured-source", "extent_ref": NATIVE_SOURCE, "closure_id": DIGEST}],
    }


def current_document():
    return {
        "schema_version": session_contracts.CURRENT_V2,
        "target": "/workspace",
        "run_id": None,
        "basis_id": DIGEST,
        "policy_id": DIGEST,
        "observed_utc": "2026-10-03T12:00:00Z",
        "accounting": {"original_ids": ["original-one"], "original_outcomes": [{"original_id": "original-one"}],
                       "accounting_complete": True, "missing_mapping": False, "accepted_count": 0, "unresolved_count": 1},
        "canonical_release": {},
        "run": None,
        "sources": {},
        "qualification": {"state": "unknown", "reason": "profile_unavailable", "profiles": []},
        "usage": dict.fromkeys(owner_observation(RunPolicy(native_policy_document()))["spent"], None),
        "policy_compatible": True,
        "limitations": ["Usage and qualification are unknown."],
        "authority": {},
        "execution": {},
        "authorized": False,
        "evidence_accepted": False,
    }


def test_native_mapping_preserves_partial_original_accounting_and_frozen_identity():
    document = native_policy_document()
    policy = RunPolicy(document)
    observation = owner_observation(policy)
    retained = copy.deepcopy(observation)
    result = policy.decide(observation)
    assert (result["outcome"], result["original_count"], result["eligible_count"], result["unresolved_count"]) == ("partial", 2, 1, 1)
    assert result["selected_outcome_refs"] == ["current-result"]
    assert result["authorized"] is False and result["evidence_accepted"] is False
    assert observation == retained
    assert RunPolicy.from_json(policy.to_json()).to_dict() == policy.to_dict()
    extra = str(Path(NATIVE_SOURCE).with_name("extra.txt"))
    document["scope"]["source_scope"].append(extra)
    assert policy.to_dict()["scope"]["source_scope"] == [NATIVE_SOURCE]
    changed = policy.to_dict()
    changed["scope"]["source_scope"] = [extra]
    with pytest.raises(WorkflowContractError) as error:
        RunPolicy(changed)
    assert error.value.error_code == "RUN_POLICY_IDENTITY_MISMATCH"
    changed["policy_id"] = None
    assert RunPolicy(changed).decide(observation)["reason"] == "policy_binding"


@pytest.mark.parametrize("extent", ["measurements.txt", "file:///workspace/raw/measurements.txt", str(Path(NATIVE_SOURCE).parent / ".." / "secret.txt")],
    ids=["relative", "url", "traversal"])
def test_native_policy_requires_absolute_nontraversing_extents(extent):
    document = native_policy_document()
    document["scope"]["source_scope"] = [extent]
    with pytest.raises(WorkflowContractError):
        RunPolicy(document)


@pytest.mark.parametrize("mutation", ["missing_binding", "foreign_identity", "permission_as_identity", "changed_extent", "duplicate_exact", "duplicate_conflicting"])
def test_selected_source_requires_unique_mapping_to_the_exact_frozen_extent(mutation):
    policy = RunPolicy(native_policy_document())
    observation = owner_observation(policy)
    if mutation == "missing_binding":
        observation["source_bindings"] = []
    elif mutation == "foreign_identity":
        observation["selected_outcomes"][0]["source_refs"] = ["foreign-source"]
    elif mutation == "permission_as_identity":
        observation["selected_outcomes"][0]["source_refs"] = [NATIVE_SOURCE]
    elif mutation == "changed_extent":
        observation["source_bindings"][0]["extent_ref"] += "/child"
    else:
        observation["source_bindings"].append(copy.deepcopy(observation["source_bindings"][0]))
        if mutation == "duplicate_conflicting":
            observation["source_bindings"][1]["closure_id"] = "sha256:" + "b" * 64
    result = policy.decide(observation)
    assert (result["outcome"], result["reason"], result["eligible_count"]) == ("rejected", "scope_accounting", 0)


@pytest.mark.parametrize("version", [[], {}, [POLICY_V2], 2, True, None, "research-run-policy/v999"])
@pytest.mark.parametrize("constructor", [RunPolicy, lambda value: RunPolicy.from_json(json.dumps(value).encode())])
def test_hostile_policy_version_always_has_a_typed_refusal(version, constructor):
    document = native_policy_document()
    document["schema_version"] = version
    with pytest.raises(WorkflowContractError):
        constructor(document)


@pytest.mark.parametrize("raw", [b'{"schema_version":"research-run-policy/v2","schema_version":[]}', b'{"schema_version":NaN}', b'{"schema_version":"\\ud800"}', b'{"schema_version":1e999}', b'[]', b'null'])
def test_hostile_json_cannot_escape_as_an_untyped_error(raw):
    with pytest.raises(WorkflowContractError):
        RunPolicy.from_json(raw)


@pytest.mark.parametrize("field", tuple(owner_observation(RunPolicy(native_policy_document()))["spent"]))
def test_native_mapping_does_not_promote_unknown_usage(field):
    policy = RunPolicy(native_policy_document())
    observation = owner_observation(policy)
    observation["spent"][field] = None
    result = policy.decide(observation)
    assert (result["outcome"], result["reason"], result["eligible_count"]) == ("stop_budget", "usage_unknown", 0)


@pytest.mark.parametrize("field", ["qualified", "independent", "qualification_ref", "reference_cases", "families", "critical_false_acceptances", "positive_acceptance", "meaning_preserving_flip"])
def test_native_mapping_does_not_promote_unknown_qualification(field):
    policy = RunPolicy(native_policy_document())
    observation = owner_observation(policy)
    observation["evaluators"][0][field] = None
    result = policy.decide(observation)
    assert (result["outcome"], result["reason"], result["eligible_count"]) == ("blocked_external", "qualification_unknown", 0)


def test_identity_scope_cannot_use_native_mapping_to_expand_permissions():
    document = native_policy_document()
    document["scope"] = {"question_ids": ["original-one", "original-two"], "source_scope": ["authorized-source"], "source_scope_kind": "identity"}
    policy = RunPolicy(document)
    assert policy.decide(owner_observation(policy))["reason"] == "scope_accounting"


def test_v1_identity_scope_retains_legacy_behavior_and_closed_shapes():
    document = native_policy_document()
    document["schema_version"] = POLICY
    document["scope"] = {"question_ids": ["original-one", "original-two"], "source_scope": ["captured-source"]}
    policy = RunPolicy(document)
    observation = owner_observation(policy)
    observation["schema_version"] = OBSERVATION
    del observation["source_bindings"]
    assert policy.decide(observation)["outcome"] == "partial"
    assert RunPolicy.from_json(policy.to_json()).to_dict() == policy.to_dict()
    observation["source_bindings"] = []
    assert policy.decide(observation)["reason"] == "invalid_input"
    document["scope"]["source_scope_kind"] = "identity"
    with pytest.raises(WorkflowContractError):
        validate_document(POLICY, document)


@pytest.mark.parametrize("seconds", [None, 0, 0.125])
def test_v2_current_roundtrip_keeps_unknowns_and_inertness(seconds):
    current = current_document()
    current["usage"]["seconds"] = seconds
    assert session_contracts.decode(session_contracts.CURRENT_V2, session_contracts.encode(session_contracts.CURRENT_V2, current)) == current
    current["authorized"] = True
    with pytest.raises(WorkflowContractError):
        session_contracts.checked(session_contracts.CURRENT_V2, current)


def test_v1_current_rejects_new_qualification_and_fractional_usage():
    current = current_document()
    current["schema_version"] = session_contracts.CURRENT
    del current["execution"]
    current["qualification"] = {"state": "unknown", "reason": "no_authenticated_evaluator_profile_owner"}
    assert session_contracts.checked(session_contracts.CURRENT, current) == current
    current["usage"]["seconds"] = 0.125
    with pytest.raises(WorkflowContractError):
        session_contracts.checked(session_contracts.CURRENT, current)


def test_v2_next_must_validate_nested_original_accounting():
    current = current_document()
    assert session_contracts.checked(session_contracts.CURRENT_V2, current) == current
    current["accounting"]["original_ids"] *= 2
    current["accounting"]["original_outcomes"] *= 2
    current["accounting"]["unresolved_count"] = 2
    with pytest.raises(WorkflowContractError):
        session_contracts.checked(session_contracts.CURRENT_V2, current)
    next_document = {"schema_version": session_contracts.NEXT_V2, "current": current, "actions": [], "decision": None,
                     "authorized": False, "evidence_accepted": False}
    with pytest.raises(WorkflowContractError):
        session_contracts.checked(session_contracts.NEXT_V2, next_document)


def binding_document():
    policy = RunPolicy(native_policy_document())
    return {
        "schema_version": session_contracts.BINDING_V2,
        "run_id": "local-run",
        "actor_ref": "host-controller",
        "target": "/workspace",
        "workspace_binding": {"path": "/workspace", "device": "1", "inode": "2"},
        "policy": policy.to_dict(),
        "policy_id": policy.policy_id,
        "request_binding": DIGEST,
        "host_context_sha256": DIGEST,
        "owner_generation": {},
        "original_ids": ["original-one", "original-two"],
        "execution_profile_id": DIGEST,
        "qualification_id": DIGEST,
        "signer_id": DIGEST,
        "observer_id": DIGEST,
        "authorized": False,
        "evidence_accepted": False,
    }


def test_v2_binding_requires_same_frozen_policy_and_originals():
    binding = binding_document()
    assert session_contracts.decode(session_contracts.BINDING_V2, session_contracts.encode(session_contracts.BINDING_V2, binding)) == binding
    for field, value in (("original_ids", ["original-one"]), ("original_ids", ["original-one", "original-one"]), ("policy_id", DIGEST)):
        invalid = copy.deepcopy(binding)
        invalid[field] = value
        with pytest.raises(WorkflowContractError):
            session_contracts.checked(session_contracts.BINDING_V2, invalid)
    binding["policy"]["scope"]["source_scope"] = ["/workspace/raw/extra.txt"]
    with pytest.raises(WorkflowContractError):
        session_contracts.checked(session_contracts.BINDING_V2, binding)


def test_v1_binding_cannot_silently_adopt_native_policy_meaning():
    binding = binding_document()
    binding["schema_version"] = session_contracts.BINDING
    for key in ("execution_profile_id", "qualification_id", "signer_id", "observer_id"):
        del binding[key]
    with pytest.raises(WorkflowContractError):
        session_contracts.checked(session_contracts.BINDING, binding)


@pytest.mark.parametrize("name", [session_contracts.CURRENT_V2, session_contracts.NEXT_V2, session_contracts.BINDING_V2, session_contracts.STEP_V2])
@pytest.mark.parametrize("raw", [b'{"schema_version":"x","schema_version":"y"}', b'{"x":NaN}', b'{"x":"\\ud800"}', b'[]', b'null'])
def test_v2_session_codec_hostile_json_has_typed_refusals(name, raw):
    with pytest.raises(WorkflowContractError):
        session_contracts.decode(name, raw)


@pytest.mark.parametrize("seconds", [-0.125, True, "0.125", float("nan"), float("inf")])
def test_v2_usage_seconds_refuses_invalid_measurements(seconds):
    current = current_document()
    current["usage"]["seconds"] = seconds
    with pytest.raises(WorkflowContractError):
        session_contracts.checked(session_contracts.CURRENT_V2, current)


def test_policy_json_size_limit_retains_typed_limit_reason():
    with pytest.raises(WorkflowContractError) as error:
        RunPolicy.from_json(b" " * (MAX_BYTES + 1))
    assert error.value.error_code == "RUN_POLICY_LIMIT"
