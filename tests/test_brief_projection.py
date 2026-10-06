"""Pure permitted-content and literal-rendering cases establish no native approval."""

import copy
import hashlib
import html

import pytest

from evidence_wiki.brief_contracts import BRIEF, checked
from evidence_wiki.brief_projection import project, render
from evidence_wiki.errors import UsageError
from evidence_wiki.resolution_projection import _project
from tests.test_resolution_projection import inputs


def test_partial_and_unresolved_originals_keep_exact_native_eligible_fields():
    resolution = _project(**inputs())
    before = copy.deepcopy(resolution)
    brief = project(resolution)
    assert brief["original_outcomes"] == resolution["original_outcomes"]
    assert [row["outcome"] for row in brief["original_outcomes"]] == ["partial", "abstain"]
    assert brief["original_outcomes"][0]["questions"][0]["current_checks"] == {
        "source_and_mechanics": "current_native_pass", "required_reviews": "current_owned_pass",
        "actual_target_qualification": "current", "producer_and_context": "current_owned_proof"}
    assert resolution == before and brief["external_action_authorized"] is False
    assert "verification" not in brief["original_outcomes"][0]["eligible_questions"][0]["claims"][0]


def test_private_material_changes_do_not_enter_visible_brief_content():
    source = inputs()
    first = project(_project(**source))
    source["current"]["basis_id"] = "sha256:" + "9" * 64
    source["current"]["canonical_release"]["publication"]["producer_id"] = "sha256:" + "8" * 64
    second = project(_project(**source))
    assert first == second
    assert render(first) == render(second)


def test_hostile_content_stays_literal_and_output_hash_covers_actual_bytes():
    value = _project(**inputs())
    hostile = '<img src=x onerror=alert(1)> [open](javascript:alert(1)) \u202e'
    value["original_outcomes"][0]["eligible_questions"][0]["claims"][0]["text"] = hostile
    value["original_outcomes"][1]["reasons"].append("No allowed evidence.")
    artifact = render(project(value))
    assert "<img" not in artifact["markdown"] and "[open](" not in artifact["markdown"]
    assert hostile[:-1] in html.unescape(artifact["markdown"])
    assert "⟦U+202E⟧" in artifact["markdown"]
    assert "No allowed evidence." in html.unescape(artifact["markdown"])
    assert artifact["markdown_sha256"] == "sha256:" + hashlib.sha256(artifact["markdown"].encode()).hexdigest()


def test_expansion_refuses_instead_of_dropping_qualifications():
    value = project(_project(**inputs()))
    claim = value["original_outcomes"][0]["eligible_questions"][0]["claims"][0]
    claim["limitations"] = ["&" * 4096 for _ in range(16)]
    question = value["original_outcomes"][0]["eligible_questions"][0]
    for index in range(3):
        peer = copy.deepcopy(claim)
        peer["id"] = "other-" + str(index)
        question["claims"].append(peer)
        value["original_outcomes"][0]["questions"][0]["eligible_claim_refs"].append(peer["id"])
    with pytest.raises(UsageError) as caught:
        render(value)
    assert caught.value.details["reason"] == "markdown_bound"


def test_public_brief_cannot_accept_private_verification_or_authority_fields():
    value = project(_project(**inputs()))
    value["original_outcomes"][0]["eligible_questions"][0]["claims"][0]["verification"] = {"key": "private"}
    with pytest.raises(UsageError):
        checked(BRIEF, value)
    value = project(_project(**inputs()))
    value["authorized"] = True
    with pytest.raises(UsageError):
        checked(BRIEF, value)
