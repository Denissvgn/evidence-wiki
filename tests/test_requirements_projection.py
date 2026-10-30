"""Original preservation, exact declared scope and append-only eligibility changes."""

import copy

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.requirements_projection import (
    BASE,
    DECISION,
    DOCUMENT,
    ENVELOPE,
    PROPOSAL,
    append_projection,
    criteria_identity,
    requirements_view,
    revision_decision,
    validated_lineage,
)


def base():
    from pathlib import Path

    from evidence_wiki.planning_contracts import normalize
    from tests.test_research_planning import question_plan, request
    value = request(Path.cwd() / "native-requirements-fixture")
    value["request"]["payload"]["goal"] = "  Retain both original questions.  "
    value["request"]["payload"]["questions"] = [{"id": "q1", "text": "  First question?  "}, {"id": "q2", "text": "Second question?"}]
    value["request"]["payload"]["budgets"]["questions"] = 5
    value["decisions"]["question_plans"] = [question_plan("q1"), question_plan("q2")]
    for plan in value["decisions"]["question_plans"]:
        plan["facets"][0]["facet_id"] = "units" if plan["question_id"] == "q1" else "scope"
        plan["criteria"][0]["facet_id"] = plan["facets"][0]["facet_id"]
    value, _ = normalize(value)
    return {**value, "schema_version": BASE}


def inputs():
    frozen = base()
    addition = {"id": "derived1", "text": "What unit evidence is retained?", "original_ids": ["q1"],
        "facet_refs": [{"original_id": "q1", "facet_id": "units"}]}
    proposal = {"schema_version": PROPOSAL, "request_id": "append1", "parent_sha256": digest(frozen), "additions": [addition]}
    envelope = {"schema_version": ENVELOPE, "policy_ref": "frozen-host-policy", "base_sha256": digest(frozen),
        "criteria_sha256": criteria_identity(frozen), "allowed_original_ids": ["q1", "q2"],
        "allowed_questions": [{key: addition[key] for key in ("text", "original_ids", "facet_refs")}],
        "max_additions": 2, "max_total_questions": 5}
    return frozen, proposal, envelope


def test_exact_declared_append_preserves_originals_and_frozen_criteria():
    frozen, proposal, envelope = inputs()
    decision = revision_decision(frozen, frozen, proposal, envelope)
    assert decision["schema_version"] == DECISION and decision["outcome"] == "append"
    assert decision["invalidates_eligibility"] and not decision["authorized"]
    effective = append_projection(frozen, frozen, decision)
    assert effective["request"]["payload"]["questions"] == frozen["request"]["payload"]["questions"]
    assert effective["decisions"]["question_plans"][:2] == frozen["decisions"]["question_plans"]
    assert effective["decisions"]["question_plans"][2]["criteria"] == frozen["decisions"]["question_plans"][0]["criteria"]
    assert criteria_identity(effective) == criteria_identity(frozen)
    assert len(effective["request"]["payload"]["derived_questions"]) == 1


@pytest.mark.parametrize("change", ["original", "criteria", "time", "stopping", "scope", "authority"])
def test_results_or_agent_confidence_cannot_rewrite_success_requirements(change):
    frozen, proposal, envelope = inputs()
    edited = copy.deepcopy(frozen)
    if change == "original":
        edited["request"]["payload"]["questions"].pop()
    elif change == "criteria":
        edited["decisions"]["question_plans"][0]["criteria"] = []
    elif change == "scope":
        edited["request"]["payload"]["scope"] = []
    elif change == "authority":
        edited["request"]["payload"]["authority"]["source_scope"] = ["new-private-source"]
    else:
        edited["decisions"]["question_plans"][0]["criteria"][0][change] = "Changed"
    proposal["parent_sha256"] = digest(edited)
    try:
        decision = revision_decision(frozen, edited, proposal, envelope)
    except ExecutionError:
        return
    assert decision["outcome"] == "rejected" and decision["reason"] == "original_or_criteria_changed"


def test_scope_fit_is_exact_predeclaration_and_never_prose_self_assertion():
    frozen, proposal, envelope = inputs()
    proposal["additions"][0]["text"] = "How profitable is an unrelated industry?"
    assert revision_decision(frozen, frozen, proposal, envelope)["reason"] == "scope_fit_not_authorized"
    proposal["additions"][0]["confidence"] = "1"
    with pytest.raises(ExecutionError):
        revision_decision(frozen, frozen, proposal, envelope)


def test_duplicate_questions_and_stale_parent_are_bounded_rejections():
    frozen, proposal, envelope = inputs()
    proposal["additions"][0]["id"] = "q1"
    assert revision_decision(frozen, frozen, proposal, envelope)["reason"] == "duplicate_question"
    proposal["additions"][0]["id"] = "derived1"
    proposal["parent_sha256"] = "sha256:" + "f" * 64
    assert revision_decision(frozen, frozen, proposal, envelope)["outcome"] == "rejected"
    proposal["parent_sha256"] = digest(frozen)
    envelope["max_total_questions"] = 2
    assert revision_decision(frozen, frozen, proposal, envelope)["reason"] == "question_allowance_exhausted"


def test_versioned_lineage_replays_exact_decision_and_keeps_owner_authentication_external():
    frozen, proposal, envelope = inputs()
    decision = revision_decision(frozen, frozen, proposal, envelope)
    document = {"schema_version": DOCUMENT, "base": frozen, "revisions": [{"revision_id": "revision1",
        "proposal": proposal, "envelope": envelope, "decision": decision, "owner_receipt_ref": "native-owner-receipt"}]}
    current = validated_lineage(document)
    assert current["base"] == frozen and current["owner_authentication"] == "not_inferred"
    assert len(current["effective"]["request"]["payload"]["derived_questions"]) == 1
    document["revisions"][0]["decision"]["authorized"] = True
    with pytest.raises(ExecutionError, match="decision_changed"):
        validated_lineage(document)


def test_frozen_addition_allowance_is_cumulative_across_revisions():
    frozen, proposal, envelope = inputs()
    other = copy.deepcopy(proposal["additions"][0])
    other.update(id="derived2", text="What contrary unit evidence is retained?")
    envelope["allowed_questions"].append({key: other[key] for key in ("text", "original_ids", "facet_refs")})
    envelope["max_additions"] = 1
    decision = revision_decision(frozen, frozen, proposal, envelope)
    effective = append_projection(frozen, frozen, decision)
    subsequent = {"schema_version": PROPOSAL, "request_id": "append2", "parent_sha256": digest(effective), "additions": [other]}
    refused = revision_decision(frozen, effective, subsequent, envelope)
    assert refused["outcome"] == "rejected" and refused["reason"] == "addition_allowance_exhausted"


def test_lineage_cannot_expand_frozen_authorization_after_results():
    frozen, proposal, envelope = inputs()
    first = revision_decision(frozen, frozen, proposal, envelope)
    effective = append_projection(frozen, frozen, first)
    other = copy.deepcopy(proposal["additions"][0])
    other.update(id="derived2", text="A newly invented scope question?")
    new_envelope = copy.deepcopy(envelope)
    new_envelope["allowed_questions"].append({key: other[key] for key in ("text", "original_ids", "facet_refs")})
    subsequent = {"schema_version": PROPOSAL, "request_id": "append2", "parent_sha256": digest(effective), "additions": [other]}
    second = revision_decision(frozen, effective, subsequent, new_envelope)
    document = {"schema_version": DOCUMENT, "base": frozen, "revisions": [
        {"revision_id": "revision1", "proposal": proposal, "envelope": envelope, "decision": first, "owner_receipt_ref": "receipt1"},
        {"revision_id": "revision2", "proposal": subsequent, "envelope": new_envelope, "decision": second, "owner_receipt_ref": "receipt2"}]}
    with pytest.raises(ExecutionError, match="authorization_changed"):
        validated_lineage(document)


def test_current_accounting_keeps_every_original_and_new_derived_obligation():
    frozen, proposal, envelope = inputs()
    effective = append_projection(frozen, frozen, revision_decision(frozen, frozen, proposal, envelope))
    mapping = {"originals": [{"id": "q1", "text": "  First question?  ", "question_ids": ["q1", "derived1"]},
        {"id": "q2", "text": "Second question?", "question_ids": ["q2"]}], "gaps": []}
    old_results = [{"original_id": "q1", "original_text": "  First question?  ", "questions": [
        {"question_id": "q1", "accepted": True, "status": "answered"}]},
        {"original_id": "q2", "original_text": "Second question?", "questions": [
        {"question_id": "q2", "accepted": False, "status": "blocked"}]}]
    view = requirements_view(effective, mapping, old_results)
    assert len(view["originals"]) == 2
    assert view["originals"][0]["disposition"] == "partial"
    assert view["originals"][0]["missing_question_ids"] == ["derived1"]
    assert view["originals"][1]["disposition"] == "blocked"
    assert view["question_plans"][0]["criteria"][0]["time"] == "State the observation date"
    assert not view["authorized"] and not view["evidence_accepted"]
    mapping["gaps"] = ["original_or_derived_question_missing:derived1"]
    assert all(row["disposition"] == "blocked" for row in requirements_view(effective, mapping, old_results)["originals"])


def test_open_work_is_blocked_current_not_terminal_abstention():
    frozen = base()
    mapping = {"originals": [{"id": row["id"], "text": row["text"], "question_ids": [row["id"]]}
                            for row in frozen["request"]["payload"]["questions"]], "gaps": []}
    outcomes = [{"original_id": row["id"], "original_text": row["text"], "questions": [
        {"question_id": row["id"], "accepted": False, "status": "open"}]} for row in frozen["request"]["payload"]["questions"]]
    assert all(row["disposition"] == "blocked" for row in requirements_view(frozen, mapping, outcomes)["originals"])
    for row in outcomes:
        row["questions"][0]["terminal_disposition"] = "abstained"
    assert all(row["disposition"] == "abstained" for row in requirements_view(frozen, mapping, outcomes)["originals"])


def test_local_gap_preserves_unrelated_original_acceptance_but_not_whole_accounting():
    frozen = base()
    mapping = {"originals": [{"id": row["id"], "text": row["text"], "question_ids": [row["id"]]}
                            for row in frozen["request"]["payload"]["questions"]],
               "expected": {"q1": "first", "q2": "second"}, "gaps": ["original_or_derived_question_missing:q2"]}
    rows = [{"original_id": "q1", "original_text": "  First question?  ", "questions": [{"question_id": "q1", "accepted": True, "status": "answered"}]}]
    result = requirements_view(frozen, mapping, rows)
    assert result["originals"][0]["disposition"] == "accepted" and not result["accounting_complete"]
    mapping["gaps"] = ["question_mapping_or_text_changed:second"]
    assert requirements_view(frozen, mapping, rows)["originals"][0]["disposition"] == "accepted"
    mapping["gaps"] = ["unsupported_original_scope"]
    assert requirements_view(frozen, mapping, rows)["originals"][0]["disposition"] == "blocked"
