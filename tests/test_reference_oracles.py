"""Independent finite reference reproduction, without a model or scorer oracle."""

from decimal import Decimal, localcontext

import pytest

from evidence_wiki.qualification_calibration import AXES, calibration_suite
from evidence_wiki.reference_oracles import DOMAIN, FAMILIES, oracle_label, protected_cases


def independent_label(document):
    facts, claim = document["facts"], document["claim"]
    if facts is None:
        return "unknown"
    if claim["units"] != facts["units"] or claim["scope"] != facts["scope"] or claim["time"] != facts["time"]:
        return "reject"
    if facts["contradiction"]:
        return "reject"
    with localcontext() as context:
        context.prec = 60
        a, b = map(Decimal, facts["values"])
        numerator, *denominator = claim["value"].split("/")
        # Cross multiplication independently checks a rational claim without
        # duplicating Fraction-based reference conversion or reducer paths.
        top, bottom = Decimal(numerator), Decimal(denominator[0]) if denominator else Decimal(1)
        if facts["operation"] == "sum":
            equal = top == (a + b) * bottom
        elif facts["operation"] == "difference":
            equal = top == (a - b) * bottom
        elif facts["operation"] == "product":
            equal = top == a * b * bottom
        elif facts["operation"] == "mean":
            equal = top * 2 == (a + b) * bottom
        else:
            equal = top * b == a * bottom
    return "accept" if equal else "reject"


def test_every_reference_and_mutation_is_independently_reproduced():
    cases = protected_cases()
    for case in cases:
        assert independent_label(case["input_document"]) == case["expected_label"]
        assert oracle_label(case["input_document"]) == case["expected_label"]
    assert len(cases) == 600
    bases = [case for case in cases if case["kind"] == "baseline"]
    assert len({case["reference"]["basis_sha256"] for case in bases}) == 100
    assert {label: sum(case["expected_label"] == label for case in bases) for label in ("accept", "reject", "unknown")} == {
        "accept": 40, "reject": 40, "unknown": 20}


@pytest.mark.parametrize("family", FAMILIES)
def test_every_case_family_contains_known_valid_invalid_and_unknown_evidence(family):
    cases = [case for case in protected_cases() if case["family_id"] == family and case["kind"] == "baseline"]
    assert len(cases) == 10
    assert {case["expected_label"] for case in cases} == {"accept", "reject", "unknown"}


@pytest.mark.parametrize("axis", AXES)
def test_style_identity_order_and_repeat_never_change_exact_reference_consequences(axis):
    cases = protected_cases()
    bases = {case["case_id"]: case for case in cases if case["kind"] == "baseline"}
    selected = [case for case in cases if case["kind"] == axis]
    assert len(selected) == 100
    for case in selected:
        base = bases[case["base_case_id"]]
        assert case["expected_label"] == independent_label(case["input_document"]) == base["expected_label"]
        assert case["reference"] == base["reference"]
        assert case["input_document"]["facts"] == base["input_document"]["facts"]
        assert case["input_document"]["claim"] == base["input_document"]["claim"]


def test_reference_suite_freezes_narrow_domain_and_protected_input():
    cases = protected_cases()
    value = calibration_suite(cases, suite_ref="finite-protected-reference", split_ref="held-out",
        domains=[DOMAIN], rubric={"id": "finite-observation", "revision": "1", "criteria": {"support": "Check declared finite observations."}},
        model_settings={"temperature": "0"})
    assert value["domains"] == [DOMAIN]
    assert all("expected_label" not in case["input_document"] for case in value["cases"])
