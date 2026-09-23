#!/usr/bin/env python3
"""Bounded interpretation of inert expressions with isolated decimal arithmetic."""

from __future__ import annotations

import ast
import decimal
import operator
import re
from dataclasses import dataclass, field
from typing import Any

MAX_EXPRESSION_BYTES = 4096
MAX_AST_NODES = 256
MAX_AST_DEPTH = 24
MAX_DIGITS = 128
MAX_EXPONENT = 256
MAX_OPERATIONS = 200_000
FUNCTIONS = frozenset({"min", "max", "abs", "clamp", "round", "bracket_lookup", "decimal"})
ROUNDINGS = frozenset({"ROUND_HALF_EVEN", "ROUND_HALF_UP", "ROUND_HALF_DOWN", "ROUND_UP", "ROUND_DOWN",
                      "ROUND_CEILING", "ROUND_FLOOR", "ROUND_05UP"})
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


class ComputationInvalid(ValueError):
    """A bounded reason code; untrusted expressions and values are never echoed."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ComputationInvalid(reason)


def number(value: Any) -> decimal.Decimal:
    if type(value) is int:
        require(value.bit_length() <= 426, "computation_number_bound")
        value = str(value)
    if type(value) is str:
        require(len(value) <= 280 and NUMBER.fullmatch(value) is not None, "computation_decimal_required")
        coefficient, *exponent = re.split("[eE]", value)
        require(sum(char.isdigit() for char in coefficient) <= MAX_DIGITS
                and (not exponent or len(exponent[0].lstrip("+-")) <= 3 and abs(int(exponent[0])) <= MAX_EXPONENT),
                "computation_number_bound")
        value = decimal.Decimal(value)
    require(type(value) is decimal.Decimal and value.is_finite(), "computation_decimal_required")
    parts = value.as_tuple()
    require(len(parts.digits) <= MAX_DIGITS and abs(parts.exponent) <= MAX_EXPONENT
            and abs(value.adjusted()) <= MAX_EXPONENT, "computation_number_bound")
    return value


def decimal_text(value: decimal.Decimal) -> str:
    value = number(value)
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    rendered = rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    if sum(char.isdigit() for char in rendered) <= MAX_DIGITS:
        return rendered
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(map(str, digits))
    mantissa = coefficient[0] + ("." + coefficient[1:] if len(coefficient) > 1 else "")
    return ("-" if sign else "") + mantissa + "E" + format(exponent + len(digits) - 1, "+d")


@dataclass(frozen=True)
class Value:
    value: Any
    lineage: frozenset[str] = frozenset()
    rounded: bool = False


@dataclass
class Budget:
    remaining: int = MAX_OPERATIONS
    consumed: int = 0

    def tick(self, amount: int = 1) -> None:
        self.remaining -= amount
        self.consumed += amount
        require(self.remaining >= 0, "computation_operation_bound")


def parse(expression: str) -> ast.Expression:
    require(type(expression) is str and bool(expression.strip())
            and len(expression.encode("utf-8")) <= MAX_EXPRESSION_BYTES, "computation_expression_bound")
    try:
        tree = ast.parse(expression, mode="eval")
    except (ValueError, SyntaxError, RecursionError):
        raise ComputationInvalid("computation_expression_invalid") from None
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Attribute, ast.Subscript, ast.Load,
               ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
               ast.UnaryOp, ast.UAdd, ast.USub, ast.Not, ast.BoolOp, ast.And, ast.Or,
               ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Call, ast.IfExp)
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        require(count <= MAX_AST_NODES and depth <= MAX_AST_DEPTH, "computation_expression_bound")
        require(isinstance(node, allowed), "computation_expression_node_forbidden")
        if isinstance(node, ast.Name):
            require(NAME.fullmatch(node.id) is not None, "computation_expression_name_invalid")
        if isinstance(node, ast.Attribute):
            require(NAME.fullmatch(node.attr) is not None, "computation_expression_field_invalid")
        if isinstance(node, ast.Call):
            require(isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
                    and not node.keywords and 1 <= len(node.args) <= 16, "computation_expression_call_forbidden")
        if isinstance(node, ast.Constant):
            require(type(node.value) in {int, float, str, bool, type(None)}, "computation_expression_literal_invalid")
            if type(node.value) in {int, float}:
                number(ast.get_source_segment(expression, node))
            elif type(node.value) is str:
                require(len(node.value.encode("utf-8")) <= 1024, "computation_string_bound")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree


def references(expression: str) -> set[str]:
    """Return data roots and dotted field references without evaluating anything."""
    tree = parse(expression)
    result = set()
    def visit(node):
        if isinstance(node, ast.Call):
            for arg in node.args:
                visit(arg)
            return
        if isinstance(node, ast.Attribute):
            parts = [node.attr]
            base = node.value
            while isinstance(base, ast.Attribute):
                parts.append(base.attr)
                base = base.value
            if isinstance(base, ast.Name):
                result.add(".".join([base.id, *reversed(parts)]))
                return
        if isinstance(node, ast.Name):
            result.add(node.id)
        for child in ast.iter_child_nodes(node):
            visit(child)
    visit(tree)
    return result


def arithmetic_policy(value: Any) -> dict:
    require(type(value) is dict and set(value) == {"mode", "precision", "scale", "rounding"}, "computation_arithmetic_invalid")
    require(value["mode"] in {"exact", "rounded"} and value["rounding"] in ROUNDINGS, "computation_arithmetic_invalid")
    require(type(value["precision"]) is int and 8 <= value["precision"] <= MAX_DIGITS, "computation_precision_invalid")
    require(value["scale"] is None or type(value["scale"]) is int and 0 <= value["scale"] <= 18, "computation_scale_invalid")
    return dict(value)


@dataclass
class Evaluator:
    policy: dict
    budget: Budget = field(default_factory=Budget)
    tables: dict = field(default_factory=dict)

    def __post_init__(self):
        self.policy = arithmetic_policy(self.policy)
        self.context = decimal.Context(prec=self.policy["precision"], rounding=self.policy["rounding"],
                                       Emin=-MAX_EXPONENT, Emax=MAX_EXPONENT, capitals=1, clamp=0)
        for signal in self.context.traps:
            self.context.traps[signal] = signal not in {decimal.Rounded, decimal.Inexact}
        self.context.traps[decimal.Inexact] = self.policy["mode"] == "exact"

    def numeric(self, operation, *values: Value) -> Value:
        self.budget.tick()
        self.context.clear_flags()
        try:
            result = number(operation(*[number(item.value) for item in values]))
        except decimal.DecimalException:
            reason = "computation_inexact" if self.context.flags[decimal.Inexact] else "computation_arithmetic_failed"
            raise ComputationInvalid(reason) from None
        return Value(result, frozenset().union(*(item.lineage for item in values)),
                     any(item.rounded for item in values) or self.context.flags[decimal.Inexact])

    def add(self, left: Value, right: Value) -> Value:
        return self.numeric(self.context.add, left, right)

    def divide(self, left: Value, right: Value) -> Value:
        return self.numeric(self.context.divide, left, right)

    def render(self, value: Value) -> dict:
        raw = value.value
        if type(raw) is decimal.Decimal:
            rendered, rounded = decimal_text(raw), value.rounded
            formatted = rendered
            if self.policy["scale"] is not None:
                quantum = decimal.Decimal((0, (1,), -self.policy["scale"]))
                display = self.numeric(self.context.quantize, value, Value(quantum))
                formatted = format(display.value, "f")
                rounded |= display.rounded
            return {"type": "decimal", "value": rendered, "formatted": formatted, "rounded": rounded,
                    "lineage": sorted(value.lineage)}
        require(type(raw) in {str, bool, type(None)}, "computation_scalar_required")
        return {"type": "null" if raw is None else "boolean" if type(raw) is bool else "string",
                "value": raw, "formatted": None, "rounded": value.rounded, "lineage": sorted(value.lineage)}

    def evaluate(self, expression: str, environment: dict[str, Value]) -> Value:
        tree = parse(expression)
        def boolean(item):
            require(type(item.value) is bool, "computation_boolean_required")
            return item.value
        def combine(raw, *items):
            return Value(raw, frozenset().union(*(item.lineage for item in items)), any(item.rounded for item in items))
        def data_field(container, key):
            raw = container.value
            if type(raw) is dict:
                require(type(key) is str and key in raw and not key.startswith("_"), "computation_field_missing")
                child = raw[key]
            elif type(raw) is list:
                index = number(key)
                require(index == index.to_integral_value() and 0 <= index < len(raw), "computation_index_invalid")
                child = raw[int(index)]
            else:
                raise ComputationInvalid("computation_data_field_required")
            child = child if isinstance(child, Value) else Value(child)
            return combine(child.value, container, child)
        def walk(node):
            self.budget.tick()
            if isinstance(node, ast.Constant):
                return Value(number(ast.get_source_segment(expression, node)) if type(node.value) in {int, float} else node.value)
            if isinstance(node, ast.Name):
                require(node.id in environment, "computation_variable_missing")
                return environment[node.id]
            if isinstance(node, ast.Attribute):
                return data_field(walk(node.value), node.attr)
            if isinstance(node, ast.Subscript):
                index = walk(node.slice)
                selected = data_field(walk(node.value), index.value)
                return combine(selected.value, selected, index)
            if isinstance(node, ast.UnaryOp):
                item = walk(node.operand)
                if isinstance(node.op, ast.Not):
                    return combine(not boolean(item), item)
                return self.numeric(self.context.minus if isinstance(node.op, ast.USub) else self.context.plus, item)
            if isinstance(node, ast.BinOp):
                left, right = walk(node.left), walk(node.right)
                operators = {ast.Add: self.context.add, ast.Sub: self.context.subtract,
                             ast.Mult: self.context.multiply, ast.Div: self.context.divide, ast.Pow: self.context.power}
                if isinstance(node.op, ast.Pow):
                    exponent = number(right.value)
                    require(exponent == exponent.to_integral_value() and abs(exponent) <= 32, "computation_power_bound")
                return self.numeric(operators[type(node.op)], left, right)
            if isinstance(node, ast.BoolOp):
                used = []
                for child in node.values:
                    item = walk(child)
                    used.append(item)
                    if boolean(item) == isinstance(node.op, ast.Or):
                        return combine(item.value, *used)
                return combine(used[-1].value, *used)
            if isinstance(node, ast.Compare):
                left, used = walk(node.left), []
                for comparison, child in zip(node.ops, node.comparators, strict=True):
                    right = walk(child)
                    used.extend([left, right])
                    a, b = left.value, right.value
                    require(type(a) is type(b) and type(a) in {decimal.Decimal, str, bool, type(None)}, "computation_comparison_type_mismatch")
                    if isinstance(comparison, (ast.Eq, ast.NotEq)):
                        passed = a == b if isinstance(comparison, ast.Eq) else a != b
                    else:
                        require(type(a) in {decimal.Decimal, str}, "computation_ordered_value_required")
                        passed = {ast.Lt: operator.lt, ast.LtE: operator.le,
                                  ast.Gt: operator.gt, ast.GtE: operator.ge}[type(comparison)](a, b)
                    if not passed:
                        return combine(False, *used)
                    left = right
                return combine(True, *used)
            if isinstance(node, ast.IfExp):
                condition = walk(node.test)
                item = walk(node.body if boolean(condition) else node.orelse)
                return combine(item.value, condition, item)
            if isinstance(node, ast.Call):
                args, name = [walk(arg) for arg in node.args], node.func.id
                if name == "decimal":
                    require(len(args) == 1, "computation_function_arity")
                    return combine(number(args[0].value), *args)
                if name == "bracket_lookup":
                    require(len(args) == 2 and type(args[0].value) is str and args[0].value in self.tables, "computation_lookup_invalid")
                    target = number(args[1].value)
                    table = self.tables[args[0].value]
                    require(type(table) is list and 1 <= len(table) <= 128, "computation_lookup_invalid")
                    for row in table:
                        self.budget.tick()
                        lower, upper = number(row["lower"]), None if row["upper"] is None else number(row["upper"])
                        if lower <= target and (upper is None or target < upper):
                            return combine(number(row["value"]), *args, Value(None, frozenset({"table:" + args[0].value})))
                    raise ComputationInvalid("computation_lookup_outside_bounds")
                require(all(type(arg.value) is decimal.Decimal for arg in args), "computation_decimal_required")
                if name in {"min", "max"}:
                    return combine((min if name == "min" else max)(arg.value for arg in args), *args)
                if name == "abs":
                    require(len(args) == 1, "computation_function_arity")
                    return self.numeric(self.context.abs, *args)
                if name == "clamp":
                    require(len(args) == 3 and args[1].value <= args[2].value, "computation_clamp_invalid")
                    return combine(max(args[1].value, min(args[0].value, args[2].value)), *args)
                if name == "round":
                    require(len(args) == 2 and args[1].value == args[1].value.to_integral_value()
                            and -18 <= args[1].value <= 18, "computation_round_invalid")
                    quantum = decimal.Decimal((0, (1,), -int(args[1].value)))
                    result = self.numeric(self.context.quantize, args[0], Value(quantum))
                    return combine(result.value, result, args[1])
            raise ComputationInvalid("computation_expression_node_forbidden")
        return walk(tree.body)
