"""Exact finite consequences and abstention are separate from model qualification."""

import copy

import pytest

from evidence_wiki.brief_projection import project
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.readability_contracts import OUTPUT
from evidence_wiki.readability_reference import (
    assess_references,
    expected_issues,
    observe_domain,
    protected_cases,
    score_output,
)
from evidence_wiki.resolution_projection import _project
from tests.test_resolution_projection import inputs


def response(case, *, state="issues", issues=None):
    packet = case["input_document"]["packet"]
    return {"schema_version": OUTPUT, "context_id": packet["context_id"], "input_sha256": digest(packet),
        "state": state, "issues": [] if issues is None else issues}


def test_reference_units_are_distinct_and_variants_do_not_inflate_the_baseline():
    cases = protected_cases()
    assessment = assess_references(cases)
    assert len(cases) == 600 and assessment["baseline_materials"] == 100
    assert assessment["authorized"] is False and assessment["evidence_accepted"] is False
    critical = next(case for case in cases if case["critical"])
    assert score_output(critical["input_document"], response(critical, issues=expected_issues(critical["input_document"]))) == "reject"
    assert score_output(critical["input_document"], response(critical)) == "accept"


def test_malformed_required_manifest_requires_explicit_abstention():
    case = copy.deepcopy(protected_cases()[0])
    case["input_document"]["packet"]["field_manifest"]["/value"] = "sha256:" + "0" * 64
    with pytest.raises(UsageError) as caught:
        score_output(case["input_document"], response(case))
    assert caught.value.details["reason"] == "reference_requires_abstention"
    assert score_output(case["input_document"], response(case, state="abstain")) == "unknown"


def test_domain_declines_control_unicode_or_long_scalar_generalization():
    brief = project(_project(**inputs()))
    assert observe_domain(brief)["state"] == "observed"
    for text in ("bidi \u202e", "new\nline", "unicode μ", "x" * 4097):
        changed = copy.deepcopy(brief)
        changed["original_outcomes"][0]["original_text"] = text
        assert observe_domain(changed)["state"] == "unknown"
