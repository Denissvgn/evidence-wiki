"""Independent numeric and hostile-input checks for the expression boundary."""

import decimal
from fractions import Fraction
from pathlib import Path

import pytest

from tests._script_loader import load_isolated_module

EXPR = load_isolated_module("computation_expression", Path(__file__).resolve().parents[1] / "workspace-template/scripts/_computation_expression.py")


def policy(**changes):
    return {"mode": "exact", "precision": 64, "scale": None, "rounding": "ROUND_HALF_EVEN", **changes}


@pytest.mark.parametrize("expression,expected", [
    ("0.1 + 0.2", "0.3"), ("1.25 * 8 - 3", "7"), ("10 / 8", "1.25"),
    ("9007199254740993 + 1", "9007199254740994"), ("2 ** -3", "0.125"),
    ("min(2, 3) + max(4, 1)", "6"), ("abs(-4.5)", "4.5"),
    ("clamp(12, 0, 10)", "10"), ("round(1.25, 2)", "1.25"),
    ("decimal('0.10') + decimal('0.20')", "0.3"),
])
def test_decimal_literals_and_registered_functions(expression, expected):
    evaluator = EXPR.Evaluator(policy())
    with decimal.localcontext() as ambient:
        ambient.prec = 2
        ambient.rounding = decimal.ROUND_UP
        got = evaluator.evaluate(expression, {})
    assert EXPR.decimal_text(got.value) == expected
    assert got.rounded is False


def test_independent_rational_oracle_for_finite_decimal_operations():
    evaluator = EXPR.Evaluator(policy(precision=128))
    for a in (-17, 0, 31):
        for b in (2, 5, 8, 25):
            expression = f"({a} / {b}) * 1.25 + 0.03"
            result = evaluator.evaluate(expression, {})
            expected = Fraction(a, b) * Fraction(5, 4) + Fraction(3, 100)
            assert Fraction(result.value) == expected


def test_exact_refuses_loss_and_rounded_results_disclose_it():
    with pytest.raises(EXPR.ComputationInvalid, match="computation_inexact"):
        EXPR.Evaluator(policy()).evaluate("1 / 3", {})
    with pytest.raises(EXPR.ComputationInvalid, match="computation_inexact"):
        EXPR.Evaluator(policy()).evaluate("round(1.255, 2)", {})
    evaluator = EXPR.Evaluator(policy(mode="rounded", precision=8, scale=2))
    result = evaluator.render(evaluator.evaluate("1 / 3", {}))
    assert result == {"type": "decimal", "value": "0.33333333", "formatted": "0.33", "rounded": True, "lineage": []}


@pytest.mark.parametrize("rounding,expected", [("ROUND_HALF_EVEN", "1.24"), ("ROUND_HALF_UP", "1.25")])
def test_tie_rounding_is_explicit(rounding, expected):
    evaluator = EXPR.Evaluator(policy(mode="rounded", rounding=rounding))
    assert EXPR.decimal_text(evaluator.evaluate("round(1.245, 2)", {}).value) == expected


def test_data_fields_and_boolean_operators_preserve_lineage():
    environment = {"record": EXPR.Value({"amount": EXPR.Value(decimal.Decimal("12"), frozenset({"source:/amount"})),
                                         "active": EXPR.Value(True, frozenset({"source:/active"}))})}
    result = EXPR.Evaluator(policy()).evaluate("record.active and record.amount >= 10", environment)
    assert result.value is True and result.lineage == {"source:/amount", "source:/active"}


def test_lookup_uses_closed_lower_open_upper_bounds():
    tables = {"bands": [{"lower": "0", "upper": "10", "value": "0.1"},
                         {"lower": "10", "upper": None, "value": "0.2"}]}
    evaluator = EXPR.Evaluator(policy(), tables=tables)
    result = evaluator.evaluate("bracket_lookup('bands', 10)", {})
    assert result.value == decimal.Decimal("0.2")
    assert result.lineage == {"table:bands"}
    with pytest.raises(EXPR.ComputationInvalid, match="computation_lookup_outside_bounds"):
        evaluator.evaluate("bracket_lookup('bands', -1)", {})


@pytest.mark.parametrize("expression", [
    "__import__('os').system('echo forbidden')", "open('/tmp/forbidden', 'w')", "record.__class__",
    "(lambda: 1)()", "[x for x in record]", "[1] * 99999999", "1 / 0", "2 ** 1000000",
    "1e99999999999999999999", "True + 1", "1 and True", "round(1, 19)", "min(True, 1)",
    "abs(1, 2)", "missing + 1", "record.get('value')", "record['__class__']", "float('NaN')",
    "1 == True", "0x10 + 1", "1_000 + 1", "1j", "(x := 1)", "{1: 2}",
    "decimal(True)", "decimal('1,000.00')", "decimal('NaN')", "decimal('1.2 units')",
])
def test_forbidden_or_undefined_inputs_refuse(expression):
    with pytest.raises(EXPR.ComputationInvalid):
        EXPR.Evaluator(policy()).evaluate(expression, {"record": EXPR.Value({})})


def test_expression_and_operation_bounds():
    with pytest.raises(EXPR.ComputationInvalid):
        EXPR.parse("1+" * 600 + "1")
    with pytest.raises(EXPR.ComputationInvalid):
        EXPR.parse("1" * 5000)
    evaluator = EXPR.Evaluator(policy(), budget=EXPR.Budget(remaining=2))
    with pytest.raises(EXPR.ComputationInvalid, match="computation_operation_bound"):
        evaluator.evaluate("1 + 2", {})


@pytest.mark.parametrize("value", [True, 0.1, "NaN", "Infinity", "1e257", "9" * 129])
def test_lossy_special_or_unbounded_numbers_refuse(value):
    with pytest.raises(EXPR.ComputationInvalid):
        EXPR.number(value)


@pytest.mark.parametrize("text", ["1e256", "1e-256", "1.23e256", "-1.23e-250"])
def test_bounded_scientific_values_round_trip_without_ambient_formatting(text):
    value = EXPR.number(text)
    with decimal.localcontext() as context:
        context.capitals = 0
        context.prec = 8
        encoded = EXPR.decimal_text(value)
    assert EXPR.number(encoded) == value
    assert "E" in encoded and "e" not in encoded
