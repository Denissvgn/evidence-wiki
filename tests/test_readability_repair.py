"""Typed presentation defects retain existing native repair and hard gates."""

from evidence_wiki.disagreement import disagreement_action
from tests.test_disagreement import A, gates, reviews, selected_policy

DEFECT = {"code": "meaning_unclear", "field": "/text", "field_sha256": A, "report_ref": A}


def action(**changes):
    arguments = dict(policy=selected_policy(), claim_id="claim", basis_id=A, generator_controller_ref="generator",
        reviews=reviews(critic="pass"), history=[], gates=gates(), presentation_defects=[DEFECT])
    arguments.update(changes)
    return disagreement_action(**arguments)


def test_issues_require_a_new_basis_and_fresh_ordinary_reviews_without_changing_votes():
    result = action()
    assert result["operation"] == "repair"
    assert result["requires_new_basis"] and result["requires_fresh_first_passes"]
    assert result["authorized"] is False and result["evidence_accepted"] is False
    assert action(presentation_defects=None)["operation"] == "return_gate"


def test_unsettled_accounting_and_missing_native_authentication_still_block_repair():
    assert action(gates=gates(accounting="unknown"))["outcome"] == "stop_budget"
    assert action(gates=gates(authentication="fail"))["outcome"] == "rejected"
    assert action(gates=gates(repair_available=False))["outcome"] == "abstained"
