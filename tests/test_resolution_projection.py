"""Inert owner-shaped fixtures test reduction only, not authentication."""

import copy

import pytest

from evidence_wiki.errors import UsageError
from evidence_wiki.resolution_contracts import checked
from evidence_wiki.resolution_projection import _project
from evidence_wiki.resolution_rules import blockers
from evidence_wiki.run_policy import RunPolicy
from tests.test_run_policy import observation, policy_document

H = "sha256:" + "1" * 64


def native_question(slug, original, accepted):
    claim = {"id": "claim-" + slug, "question_slug": slug, "qualification": "supported", "text": "Sample source says 7.",
        "scope": "fixture", "time": "fixture", "units": "count", "evidence": [], "premises": [], "derivation": None,
        "limitations": [], "verification": {"observation": {"basis_id": H, "claim_id": "claim-" + slug,
            "producer_id": H, "reasons": []}, "review": {"passed": True, "reason": "pass"}}}
    return {"slug": slug, "original_id": original, "question": "Fixture?", "status": "answered" if accepted else "blocked",
        "accepted": accepted, "claims": [claim] if accepted else [], "gaps": [] if accepted else ["strict_release_not_eligible"],
        "unresolved_claims": []}


def inputs():
    policy = RunPolicy(policy_document())
    frozen = policy.to_dict()
    pub = {"schema_version": "evidence-strict-publication/v1", "basis_id": H, "policy_id": H, "producer_id": H,
        "evaluated_at": "2026-01-01T00:00:00Z", "assurance": "artifact_checked", "verdict": "blocked_on_sources",
        "limitations": [], "selection_complete": True,
        "questions": [native_question("one", "question-a", True), native_question("two", "question-a", False),
            native_question("three", "question-b", False)],
        "original_outcomes": [{"original_id": "question-a", "question_slugs": ["one", "two"], "accepted": False, "unselected_question_slugs": []},
            {"original_id": "question-b", "question_slugs": ["three"], "accepted": False, "unselected_question_slugs": []}]}
    rows = []
    for identifier, names in (("question-a", ["one", "two"]), ("question-b", ["three"])):
        rows.append({"original_id": identifier, "original_text": "Original " + identifier,
            "accepted": False, "missing_question_ids": [], "questions": [
                {"question_id": name, "slug": name, "accepted": name == "one", "gaps": [] if name == "one" else ["not_released"]}
                for name in names]})
    current = {"basis_id": H, "policy_id": policy.policy_id, "accounting": {"original_outcomes": rows,
        "accounting_complete": True}, "canonical_release": {"publication": pub, "gaps": []}, "policy_compatible": True,
        "qualification": {"state": "qualified"}, "run": {"terminal": True}, "execution": {"pending": None}}
    observed = observation(policy, selected_count=0)
    observed.update(basis_id=H, expected_basis_id=H)
    proof = {"one": {"eligible": True, "claim_refs": ["claim-one"], "reasons": []}}
    return dict(run_id="fixture-run", binding_id=H, current=current, policy_document=frozen,
        policy_observation=observed, decision=policy.decide(observed), derived_proofs=proof, subset_blockers=[],
        lifecycle={"terminal": True, "decision": {"outcome": "accepted", "reason": "historical_fixture"}})


def test_partial_original_keeps_all_accounting_and_whole_original_counts_unchanged():
    value = inputs()
    before = copy.deepcopy(value)
    result = _project(**value)
    assert result["verdict"] == "partial"
    assert [row["outcome"] for row in result["original_outcomes"]] == ["partial", "abstain"]
    assert result["original_outcomes"][0]["questions"][1]["outcome"] == "abstain"
    assert len(result["original_outcomes"][0]["eligible_questions"]) == 1
    assert value == before
    assert value["policy_observation"]["accepted_count"] == 0
    assert result["external_action_authorized"] is False


def test_permitted_content_does_not_export_whole_workspace_review_commitments():
    value = inputs()
    first = _project(**value)
    hidden = copy.deepcopy(value)
    hidden["current"]["basis_id"] = "sha256:" + "2" * 64
    pub = hidden["current"]["canonical_release"]["publication"]
    pub["basis_id"] = "sha256:" + "3" * 64
    claim = pub["questions"][0]["claims"][0]
    claim["verification"]["observation"]["basis_id"] = pub["basis_id"]
    claim["verification"]["review"] = {"passed": True, "reason": "pass", "review_id": "sha256:" + "4" * 64,
        "principal": "native-principal", "independence": "host_controller_assertion", "human_review": False}
    second = _project(**hidden)
    assert first["original_outcomes"] == second["original_outcomes"]
    assert first["current_basis_id"] == second["current_basis_id"]
    assert second["publication_basis_id"] is None and second["current_publication"] is None
    assert all("verification" not in claim for row in second["original_outcomes"]
        for question in row["eligible_questions"] for claim in question["claims"])


def test_no_owned_derived_proof_withholds_native_latest_generic_acceptance():
    value = inputs()
    value["derived_proofs"] = {}
    result = _project(**value)
    assert result["verdict"] == "abstain"
    assert result["current_publication"] is None
    assert not result["original_outcomes"][0]["eligible_questions"]


def test_coarse_current_or_historical_rejection_does_not_reject_every_original():
    value = inputs()
    value.update(derived_proofs={}, decision={"outcome": "rejected", "reason": "scope_accounting"},
        lifecycle={"terminal": True, "decision": {"outcome": "rejected", "reason": "old_failure"}})
    result = _project(**value)
    assert result["verdict"] == "abstain"
    assert all(row["outcome"] == "abstain" for row in result["original_outcomes"])
    assert result["lifecycle"]["outcome"] == "rejected"


@pytest.mark.parametrize("change", ["global_gap", "accounting", "missing_original", "selection", "legacy", "policy", "qualification", "subset_cap"])
def test_global_blockers_withhold_every_eligible_claim(change):
    value = inputs()
    current = value["current"]
    if change == "global_gap":
        current["canonical_release"]["gaps"] = ["instructions_changed"]
    elif change == "accounting":
        current["accounting"]["accounting_complete"] = False
    elif change == "missing_original":
        current["accounting"]["original_outcomes"].pop()
    elif change == "selection":
        current["canonical_release"]["publication"]["selection_complete"] = False
    elif change == "legacy":
        current["canonical_release"]["publication"]["schema_version"] = "legacy"
    elif change == "policy":
        current["policy_compatible"] = False
    elif change == "qualification":
        current["qualification"]["state"] = "unknown"
    else:
        value["subset_blockers"] = ["current_budget_exhausted"]
    result = _project(**value)
    assert result["global_blockers"] and result["current_publication"] is None
    assert len(result["original_outcomes"]) == 2
    assert all(not row["eligible_questions"] for row in result["original_outcomes"])


def test_partial_disallowed_does_not_upgrade_derived_subset():
    value = inputs()
    value["policy_document"]["source_rules"]["allow_partial"] = False
    result = _project(**value)
    assert result["verdict"] == "abstain"


def test_claim_reference_outside_exact_owned_subset_refused():
    value = inputs()
    value["derived_proofs"]["one"]["claim_refs"] = ["different-claim"]
    with pytest.raises(UsageError):
        _project(**value)


def test_current_exact_local_rejection_can_reject_only_its_original():
    value = inputs()
    value["derived_proofs"] = {slug: {"eligible": False, "rejected": True, "claim_refs": [], "reasons": []}
        for slug in ("one", "two")}
    result = _project(**value)
    assert [row["outcome"] for row in result["original_outcomes"]] == ["reject", "abstain"]
    assert result["verdict"] == "abstain"


def test_global_authority_failure_does_not_turn_local_rejection_into_known_current_result():
    value = inputs()
    value["derived_proofs"] = {slug: {"eligible": False, "rejected": True, "claim_refs": [], "reasons": []}
        for slug in ("one", "two", "three")}
    value["subset_blockers"] = ["terminal_authority_unavailable"]
    result = _project(**value)
    assert all(row["outcome"] == "abstain" for row in result["original_outcomes"])


def test_complete_accounting_cannot_be_inferred_from_partial_acceptance_status():
    value = inputs()
    value["current"]["accounting"]["accounting_complete"] = False
    result = _project(**value)
    assert result["accounting_complete"] is False and result["verdict"] == "abstain"


def test_full_current_original_and_partial_between_originals():
    value = inputs()
    second = value["current"]["canonical_release"]["publication"]["questions"][1]
    second.update(native_question("two", "question-a", True))
    value["current"]["accounting"]["original_outcomes"][0]["questions"][1]["accepted"] = True
    value["derived_proofs"]["two"] = {"eligible": True, "claim_refs": ["claim-two"], "reasons": []}
    result = _project(**value)
    assert [row["outcome"] for row in result["original_outcomes"]] == ["accept", "abstain"]
    assert result["verdict"] == "partial"


def test_unqualified_native_generic_claim_is_not_returned_in_partial_publication():
    value = inputs()
    third = value["current"]["canonical_release"]["publication"]["questions"][2]
    third.update(native_question("three", "question-b", True))
    value["current"]["accounting"]["original_outcomes"][1]["questions"][0]["accepted"] = True
    result = _project(**value)
    assert result["verdict"] == "partial"
    assert result["current_publication"] is None
    assert result["original_outcomes"][1]["eligible_questions"] == []


def test_partial_disallowed_also_withholds_a_wholly_proven_original_when_another_is_unresolved():
    value = inputs()
    value["policy_document"]["source_rules"]["allow_partial"] = False
    original = value["current"]["accounting"]["original_outcomes"][0]
    original["questions"] = original["questions"][:1]
    original["accepted"] = True
    selected = {"question_id": original["original_id"], "basis_id": H, "claim_refs": ["claim-one"],
        **dict.fromkeys(("independently_eligible", "source_current", "source_present", "mechanical_pass", "review_pass", "qualified", "context_complete"), True)}
    value["policy_observation"]["selected_outcomes"] = [selected]
    result = _project(**value)
    assert result["verdict"] == "abstain" and not result["original_outcomes"][0]["eligible_questions"]


@pytest.mark.parametrize("change", ["unknown_usage", "seconds", "tools", "cost", "pending", "authority", "human", "foreign_source", "qualification"])
def test_explicit_subset_rules_preserve_global_policy_budget_and_scope(change):
    value = inputs()
    current, policy, observed = value["current"], value["policy_document"], value["policy_observation"]
    if change == "unknown_usage":
        observed["spent"]["model_calls"] = None
    elif change == "seconds":
        observed["spent"]["seconds"] = policy["budgets"]["seconds"]
    elif change == "tools":
        observed["spent"]["tool_calls"] = policy["budgets"]["tool_calls"]
    elif change == "cost":
        observed["spent"]["cost_limit"] = "0.1"
    elif change == "pending":
        current["execution"]["pending"] = {"reservation_occupied": True}
    elif change == "authority":
        observed["authority_granted"] = False
    elif change == "human":
        policy["human_review"] = True
    elif change == "foreign_source":
        current["canonical_release"]["publication"]["questions"][0]["claims"][0]["evidence"] = [{"source_id": "foreign"}]
    else:
        observed["evaluators"][0]["qualified"] = False
    assert blockers(current, policy, observed, value["derived_proofs"])


def test_checked_result_cannot_change_false_authority_or_permissive_flags():
    result = _project(**inputs())
    result["external_action_authorized"] = True
    with pytest.raises(UsageError):
        checked(result)


def test_duplicate_original_and_unbounded_native_claim_text_refused():
    value = inputs()
    value["current"]["accounting"]["original_outcomes"].append(copy.deepcopy(value["current"]["accounting"]["original_outcomes"][0]))
    with pytest.raises(UsageError):
        _project(**value)
    value = inputs()
    value["current"]["canonical_release"]["publication"]["questions"][0]["claims"][0]["text"] = "x" * 65537
    with pytest.raises(UsageError):
        _project(**value)
