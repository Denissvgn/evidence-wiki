"""Inert native inventory constraints do not establish repair adequacy."""

import copy
import json

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.repair_progress import DIFF, inventory_diff, schemas
from tests.test_review_context import inputs


def inventory():
    snapshot = inputs()["owner_snapshot"]
    fields = owner("_strict_contract").schema_documents()["evidence-strict-claims/v1"]["properties"]["claims"]["items"]["properties"]
    claims = [{key: claim[key] for key in fields} for claim in [snapshot["claim"], *snapshot["premises"]]]
    return {"schema_version": "evidence-strict-claims/v1", "questions": [snapshot["question"]], "claims": claims}


@pytest.mark.parametrize("field", ["units", "scope", "text", "time"])
def test_target_field_diff_is_exact_without_source_text_or_semantic_pass(field):
    before = inventory()
    after = copy.deepcopy(before)
    after["claims"][0][field] += " Revised."
    result = inventory_diff(before, after, target_claim_id=before["claims"][0]["id"])
    assert [item["field"] for item in result["changes"]] == [field]
    assert result["changes"][0]["before_sha256"] != result["changes"][0]["after_sha256"]
    assert result["semantic_progress"] == "not_established" and not result["evidence_accepted"]
    assert after["claims"][0][field] not in json.dumps(result)
    owner("_strict_contract").validate_shape(result, schemas()[DIFF])
    assert before != after


def test_wrong_source_change_is_reported_without_certifying_it():
    before = inventory()
    after = copy.deepcopy(before)
    after["claims"][0]["evidence"][0]["source_id"] = "wrong-source"
    result = inventory_diff(before, after, target_claim_id=before["claims"][0]["id"])
    assert result["changes"][0]["field"] == "evidence"
    assert result["current_approval"] == "not_established"


def test_equal_claim_is_not_claimed_as_changed_or_repaired():
    before = inventory()
    result = inventory_diff(before, copy.deepcopy(before), target_claim_id=before["claims"][0]["id"])
    assert not result["claim_bytes_changed"] and result["changes"] == []
    assert result["semantic_progress"] == "not_established"


@pytest.mark.parametrize("attack", ["rename", "remove", "add", "move", "peer", "original", "criterion"])
def test_unauthorized_surface_changes_reject(attack):
    before = inventory()
    if len(before["claims"]) == 1:
        peer = copy.deepcopy(before["claims"][0])
        peer["id"] = "unchanged-peer"
        before["claims"].append(peer)
    after = copy.deepcopy(before)
    if attack == "rename":
        after["claims"][0]["id"] = "renamed-target"
    elif attack == "remove":
        after["claims"].pop()
    elif attack == "add":
        added = copy.deepcopy(after["claims"][0])
        added["id"] = "extra-target"
        after["claims"].append(added)
    elif attack == "move":
        after["claims"][0]["question_slug"] = "foreign-question"
    elif attack == "peer":
        after["claims"][-1]["units"] += " Altered peer."
    elif attack == "original":
        after["questions"][0]["question"] += " Easier original."
    else:
        after["claims"][0]["threshold"] = "lowered"
    with pytest.raises((ExecutionError, ValueError)):
        inventory_diff(before, after, target_claim_id=before["claims"][0]["id"])


@pytest.mark.parametrize("criterion", ["units", "scope", "support"])
def test_failed_criterion_routes_only_bounded_repair_not_approval(criterion):
    from tests.test_disagreement import gates, reviews, select
    current = reviews(critic="pass")
    current[-1]["verdicts"][criterion] = "fail"
    decision = select(reviews=current, gates=gates(adjudicator_available=False))
    assert decision["operation"] == "repair"
    assert decision["requires_new_basis"] and decision["requires_fresh_first_passes"]
    assert not decision["evidence_accepted"] and not decision["authorized"]


def test_exhausted_repair_preserves_unresolved_result_not_human_queue():
    from tests.test_disagreement import A, B, C, event, gates, reviews, select
    first = select(gates=gates(adjudicator_available=False))
    history = [event(first, index=1, basis=A, result=B), event(first, index=2, basis=B, result=C)]
    final = select(basis_id=C, reviews=reviews(basis=C), history=history,
        gates=gates(run_repair_cycles=2, adjudicator_available=False))
    assert final["terminal"] and final["outcome"] == "abstained"
    assert final["repairs_used"] == 2
    assert not final["evidence_accepted"] and "human" not in final["operation"]
