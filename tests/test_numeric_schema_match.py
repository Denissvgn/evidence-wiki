"""Finite JSON numeric observations retain closed schema and bounds checks."""

import pytest

from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.onboarding_contract import _matches


@pytest.mark.parametrize("value", [0, -2, 1.25, -2.0, 100.0])
def test_numeric_schema_accepts_finite_integer_and_float_scores(value):
    _matches(value, {"type": "number", "minimum": -2, "maximum": 100})


@pytest.mark.parametrize("value", [True, False, "1.25", None, float("nan"), float("inf"), -float("inf"), -2.01, 100.01])
def test_numeric_schema_refuses_coercion_nonfinite_and_outside_bounds(value):
    with pytest.raises(EvidenceWikiError):
        _matches(value, {"type": "number", "minimum": -2, "maximum": 100})


def test_integer_schema_does_not_acquire_float_or_boolean_coercion():
    for value in [1.0, True]:
        with pytest.raises(EvidenceWikiError):
            _matches(value, {"type": "integer"})


def test_closed_nested_numeric_result_cannot_hide_extra_authority_fields():
    schema = {"type": "object", "properties": {"score": {"type": "number"}},
        "required": ["score"], "additionalProperties": False}
    _matches({"score": 0.125}, schema)
    with pytest.raises(EvidenceWikiError):
        _matches({"score": 0.125, "authorized": True}, schema)
