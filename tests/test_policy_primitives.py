"""`_policy_primitives.py` — what a pack may declare, and what the declaration decides.

CR-9 lets a domain pack automate policies it previously could only send to a human. The
value of that trade depends entirely on two properties, and these tests exist to hold
both.

First, a rule is *data*: a closed set of primitive names with fixed arguments, validated
before the pack ships. Every malformed shape below is a shape a pack author will
eventually write, and each one has to come back naming the policy and the offending key
rather than being quietly ignored — a dropped rule is a policy that silently stops being
checked.

Second, evaluation is *fail-closed and pure*. Every resolution failure — no structured
view, a pointer into nothing, a subtree where a scalar was promised, a timestamp nobody
can parse — has to land on `fail` with a typed reason, never on `manual_review`. The
traps are written out on purpose: `re.search` passing where `fullmatch` must not, a
future timestamp read as age zero, and a date-only value quietly becoming "fresh".
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RULES = load_script_module("cr9_policy_primitives", "_policy_primitives.py")
POLICIES = load_script_module("cr9_policy_primitives_evidence", "_evidence_policies.py")

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def never_matches(host: str, domain: str) -> bool:
    return False


def pack(
    rules: Any,
    *,
    name: str = "market-data",
    vocabularies: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """A domain pack declaring ``rules``, with a vocabulary that covers the ids used here."""
    if vocabularies is None:
        vocabularies = {
            "freshness_policy": {"pack:market-data/quote-48h": "A quote must be at most 48 hours old."},
            "identity_policy": {"pack:market-data/sku-matches-candidate": "The SKU must match."},
            "source_policy": {"pack:market-data/named-supplier": "The supplier must be named."},
        }
    declaration: dict[str, Any] = {"name": name, "policy_vocabularies": vocabularies}
    if rules is not None:
        declaration["policy_rules"] = rules
    return declaration


def freshness_rule(*primitives: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"pack:market-data/quote-48h": {"all_of": list(primitives), **top}}


def identity_rule(*primitives: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"pack:market-data/sku-matches-candidate": {"all_of": list(primitives), **top}}


def parse_one(*primitives: dict[str, Any], **top: Any):
    """Parse a single-rule pack and return the one `Rule`, asserting it validated."""
    declaration = pack(freshness_rule(*primitives, **top))
    errors = RULES.declaration_errors(declaration)
    if errors:
        raise AssertionError(f"expected a valid declaration, got: {errors}")
    return RULES.pack_policy_rules({"domain_pack": declaration})["pack:market-data/quote-48h"]


def context(**overrides: Any):
    defaults: dict[str, Any] = {
        "source_id": "src-x",
        "structured_view": None,
        "structured_view_error": None,
        "provenance": {},
        "question_frontmatter": None,
        "origin_host": None,
        "provider_ids": (),
        "now": NOW,
        "domain_matches": never_matches,
    }
    defaults.update(overrides)
    return RULES.RuleContext(**defaults)


def retrieved_hours_ago(hours: float) -> dict[str, str]:
    moment = NOW - timedelta(hours=hours)
    return {"retrieved_at": moment.isoformat().replace("+00:00", "Z")}


class DeclarationShapeTests(unittest.TestCase):
    """The schema itself: sections, ids, and the rule's own top level."""

    def test_a_pack_declaring_no_rules_is_valid_and_yields_nothing(self):
        declaration = pack(None)
        self.assertEqual([], RULES.declaration_errors(declaration))
        self.assertEqual({}, RULES.pack_policy_rules({"domain_pack": declaration}))

    def test_absent_config_and_absent_domain_pack_yield_nothing(self):
        self.assertEqual({}, RULES.pack_policy_rules(None))
        self.assertEqual({}, RULES.pack_policy_rules({}))
        self.assertEqual([], RULES.declaration_errors(None))

    def test_the_normative_example_round_trips_into_dataclasses(self):
        declaration = pack(
            {
                "pack:market-data/quote-48h": {
                    "all_of": [{"max_age": {"field": "provenance/retrieved_at", "hours": 48}}]
                },
                "pack:market-data/sku-matches-candidate": {
                    "manual_review_required": False,
                    "all_of": [
                        {
                            "equals": {
                                "field": "record/supplier_quote/sku",
                                "question_field": "metadata/candidate_sku",
                            }
                        },
                        {"one_of_provenance": {"providers": ["aliexpress-ds", "partner-catalog"]}},
                    ],
                },
            }
        )
        self.assertEqual([], RULES.declaration_errors(declaration))
        parsed = RULES.pack_policy_rules({"domain_pack": declaration})
        self.assertEqual(
            {"pack:market-data/quote-48h", "pack:market-data/sku-matches-candidate"},
            set(parsed),
        )

        freshness = parsed["pack:market-data/quote-48h"]
        self.assertEqual("pack:market-data/quote-48h", freshness.policy_id)
        self.assertFalse(freshness.manual_review_required)
        self.assertEqual("all_of", freshness.composition.name)
        max_age = freshness.composition.children[0]
        self.assertEqual("max_age", max_age.name)
        self.assertEqual("provenance", max_age.field.root)
        self.assertEqual("/retrieved_at", max_age.field.pointer)
        self.assertEqual("provenance/retrieved_at", max_age.field.display)
        self.assertEqual(Decimal("48"), max_age.hours)

        identity = parsed["pack:market-data/sku-matches-candidate"]
        equals, provenance = identity.composition.children
        self.assertEqual("record", equals.field.root)
        self.assertEqual("/supplier_quote/sku", equals.field.pointer)
        self.assertIsNone(equals.operand.ref.root, "a question_field carries no root")
        self.assertEqual("/metadata/candidate_sku", equals.operand.ref.pointer)
        self.assertEqual(("aliexpress-ds", "partner-catalog"), provenance.providers)
        self.assertEqual((), provenance.domains)

    def test_manual_review_required_is_carried_but_not_acted_on(self):
        rule = parse_one({"max_age": {"field": "provenance/retrieved_at", "hours": 48}},
                         manual_review_required=True)
        self.assertTrue(rule.manual_review_required)
        evaluation = RULES.evaluate_rule(rule, context(provenance=retrieved_hours_ago(1)))
        self.assertTrue(evaluation.passed, "the flag is the evaluator's business, not the rule's")

    def test_policy_rules_must_be_a_mapping(self):
        errors = RULES.declaration_errors(pack([{"pack:market-data/quote-48h": {}}]))
        self.assertEqual(1, len(errors), errors)
        self.assertIn("domain_pack.policy_rules must be a mapping", errors[0])

    def test_a_malformed_id_is_rejected_and_named(self):
        errors = RULES.declaration_errors(pack({"quote-48h": {"all_of": []}}))
        self.assertEqual(1, len(errors), errors)
        self.assertIn("quote-48h", errors[0])
        self.assertIn("pack:<pack-name>/<policy-id>", errors[0])

    def test_a_rule_in_another_packs_namespace_is_rejected(self):
        errors = RULES.declaration_errors(
            pack({"pack:other-pack/quote-48h": {"all_of": [{"max_age": {"field": "provenance/x", "hours": 1}}]}})
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("pack:other-pack/quote-48h", errors[0])
        self.assertIn("other-pack", errors[0])
        self.assertIn("market-data", errors[0])

    def test_a_rule_for_an_undeclared_policy_is_rejected(self):
        errors = RULES.declaration_errors(
            pack({"pack:market-data/never-declared": {"all_of": [{"max_age": {"field": "provenance/x", "hours": 1}}]}})
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("pack:market-data/never-declared", errors[0])
        self.assertIn("policy_vocabularies", errors[0])

    def test_a_rule_targeting_an_evidence_path_names_that_section(self):
        """An evidence path says which facet must be covered, not what the evidence says."""
        declaration = pack(
            {"pack:market-data/quote-path": {"all_of": [{"max_age": {"field": "provenance/x", "hours": 1}}]}},
            vocabularies={"evidence_paths": {"pack:market-data/quote-path": "A supplier quote path."}},
        )
        errors = RULES.declaration_errors(declaration)
        self.assertEqual(1, len(errors), errors)
        self.assertIn("evidence_paths", errors[0])
        self.assertIn("pack:market-data/quote-path", errors[0])

    def test_a_rule_needs_exactly_one_composition(self):
        both = RULES.declaration_errors(
            pack({"pack:market-data/quote-48h": {"all_of": [{"max_age": {"field": "provenance/x", "hours": 1}}],
                                                 "any_of": [{"max_age": {"field": "provenance/x", "hours": 1}}]}})
        )
        self.assertEqual(1, len(both), both)
        self.assertIn("exactly one of all_of or any_of", both[0])
        self.assertIn("all_of, any_of", both[0])

        neither = RULES.declaration_errors(pack({"pack:market-data/quote-48h": {"manual_review_required": True}}))
        self.assertEqual(1, len(neither), neither)
        self.assertIn("neither", neither[0])

    def test_an_unknown_top_level_key_is_named(self):
        errors = RULES.declaration_errors(
            pack(freshness_rule({"max_age": {"field": "provenance/x", "hours": 1}}, when="tuesday"))
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("unknown key(s): when", errors[0])
        self.assertIn("pack:market-data/quote-48h", errors[0])

    def test_manual_review_required_must_be_boolean(self):
        errors = RULES.declaration_errors(
            pack(freshness_rule({"max_age": {"field": "provenance/x", "hours": 1}}, manual_review_required="yes"))
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("manual_review_required must be true or false", errors[0])

    def test_declaration_errors_never_raises_on_hostile_input(self):
        for value in (None, [], "text", 7, {"name": 3, "policy_rules": {3: 4}}, {"policy_rules": {"": {}}}):
            with self.subTest(value=value):
                self.assertIsInstance(RULES.declaration_errors(value), list)


class PrimitiveValidationTests(unittest.TestCase):
    """Each primitive's arguments, and the compositions that hold them."""

    def errors_for(self, primitive: Any) -> list[str]:
        return RULES.declaration_errors(pack(freshness_rule(primitive)))

    def only_error(self, primitive: Any) -> str:
        errors = self.errors_for(primitive)
        self.assertEqual(1, len(errors), errors)
        return errors[0]

    def test_an_unknown_primitive_names_itself_and_the_allowed_set(self):
        message = self.only_error({"no_such_primitive": {}})
        self.assertIn("no_such_primitive", message)
        for name in RULES.PRIMITIVE_NAMES:
            self.assertIn(name, message)

    def test_a_primitive_entry_names_exactly_one_primitive(self):
        message = self.only_error({"max_age": {"field": "provenance/x", "hours": 1}, "regex": {}})
        self.assertIn("exactly one primitive", message)

    def test_max_age_requires_a_positive_finite_hours(self):
        self.assertIn("missing required key(s): hours", self.only_error({"max_age": {"field": "provenance/x"}}))
        for hours in (0, -1, "48", True, float("inf"), float("nan")):
            with self.subTest(hours=hours):
                message = self.only_error({"max_age": {"field": "provenance/x", "hours": hours}})
                self.assertIn(".hours", message)
        self.assertEqual([], self.errors_for({"max_age": {"field": "provenance/x", "hours": 0.5}}))

    def test_max_age_rejects_an_unknown_argument(self):
        message = self.only_error({"max_age": {"field": "provenance/x", "hours": 1, "days": 2}})
        self.assertIn("unknown key(s): days", message)

    def test_a_field_reference_must_carry_a_known_root(self):
        message = self.only_error({"max_age": {"field": "manifest/retrieved_at", "hours": 1}})
        self.assertIn("record", message)
        self.assertIn("provenance", message)
        self.assertIn("'manifest'", message)

    def test_a_field_reference_must_name_a_field_below_its_root(self):
        for reference in ("record", "record/", "provenance/"):
            with self.subTest(reference=reference):
                self.assertIn(".field", self.only_error({"max_age": {"field": reference, "hours": 1}}))

    def test_a_field_reference_rejects_invalid_pointer_escaping(self):
        message = self.only_error({"max_age": {"field": "record/bad~2token", "hours": 1}})
        self.assertIn("RFC 6901", message)

    def test_equals_needs_exactly_one_of_value_and_question_field(self):
        both = self.only_error(
            {"equals": {"field": "record/sku", "value": "A", "question_field": "metadata/sku"}}
        )
        self.assertIn("declares both value and question_field", both)
        neither = self.only_error({"equals": {"field": "record/sku"}})
        self.assertIn("exactly one of value or question_field", neither)

    def test_equals_rejects_a_non_scalar_literal(self):
        message = self.only_error({"equals": {"field": "record/sku", "value": {"a": 1}}})
        self.assertIn("must be a scalar", message)

    def test_a_question_field_must_not_carry_a_root(self):
        message = self.only_error(
            {"equals": {"field": "record/sku", "question_field": "record/metadata/sku"}}
        )
        self.assertIn("bare pointer", message)
        self.assertIn("'record'", message)

    def test_numeric_range_needs_at_least_one_bound(self):
        message = self.only_error({"numeric_range": {"field": "record/price"}})
        self.assertIn("at least one bound", message)

    def test_numeric_range_rejects_a_doubled_or_non_numeric_bound(self):
        doubled = self.only_error(
            {"numeric_range": {"field": "record/price", "min": 1, "min_question_field": "metadata/floor"}}
        )
        self.assertIn("declares both min and min_question_field", doubled)
        non_numeric = self.only_error({"numeric_range": {"field": "record/price", "max": "cheap"}})
        self.assertIn("must be a decimal number", non_numeric)

    def test_regex_patterns_are_compiled_and_capped_at_declaration_time(self):
        self.assertIn(
            "not a valid regular expression",
            self.only_error({"regex": {"field": "record/sku", "pattern": "B0(["}}),
        )
        oversize = "a" * (RULES.MAX_REGEX_PATTERN_LENGTH + 1)
        message = self.only_error({"regex": {"field": "record/sku", "pattern": oversize}})
        self.assertIn(str(RULES.MAX_REGEX_PATTERN_LENGTH), message)
        self.assertEqual(
            [],
            self.errors_for({"regex": {"field": "record/sku", "pattern": "a" * RULES.MAX_REGEX_PATTERN_LENGTH}}),
        )

    def test_one_of_provenance_needs_a_non_empty_list(self):
        self.assertIn("providers list, domains list, or both", self.only_error({"one_of_provenance": {}}))
        self.assertIn(
            "non-empty list", self.only_error({"one_of_provenance": {"providers": []}})
        )
        self.assertIn(
            "non-empty strings", self.only_error({"one_of_provenance": {"domains": [" "]}})
        )
        self.assertEqual(
            [], self.errors_for({"one_of_provenance": {"domains": ["supplier.example"]}})
        )

    def test_a_composition_must_hold_at_least_one_primitive(self):
        errors = RULES.declaration_errors(pack({"pack:market-data/quote-48h": {"all_of": []}}))
        self.assertEqual(1, len(errors), errors)
        self.assertIn("non-empty list of primitives", errors[0])

    def test_nesting_is_capped_at_three_compositions(self):
        leaf = {"max_age": {"field": "provenance/retrieved_at", "hours": 1}}
        deepest_allowed = {"all_of": [{"any_of": [leaf]}]}
        self.assertEqual([], self.errors_for(deepest_allowed))
        too_deep = {"all_of": [{"any_of": [{"all_of": [leaf]}]}]}
        message = self.only_error(too_deep)
        self.assertIn(str(RULES.MAX_COMPOSITION_DEPTH), message)


class RaisingAccessorTests(unittest.TestCase):
    """`pack_policy_rules` is the consumer that must not degrade quietly."""

    def test_a_malformed_declaration_raises_with_every_error_attached(self):
        declaration = pack(
            {
                "pack:market-data/quote-48h": {"all_of": [{"no_such_primitive": {}}]},
                "pack:market-data/sku-matches-candidate": {"all_of": []},
            }
        )
        with self.assertRaises(RULES.PolicyRuleError) as caught:
            RULES.pack_policy_rules({"domain_pack": declaration})
        error = caught.exception
        self.assertEqual("CONFIG_INVALID", error.error_code)
        self.assertIn("domain_pack.policy_rules is invalid", error.message)
        self.assertIn("no_such_primitive", error.message)
        self.assertEqual(2, len(error.details["errors"]), error.details)
        self.assertIn("policy_rules", error.remediation)

    def test_the_error_shape_matches_the_shared_refusal_contract(self):
        error = RULES.PolicyRuleError("CONFIG_INVALID", "broken")
        self.assertEqual("broken", str(error))
        self.assertEqual("broken", error.message)
        self.assertEqual(RULES.POLICY_RULE_REMEDIATION, error.remediation)
        self.assertEqual({}, error.details)


class TimestampReaderTests(unittest.TestCase):
    def test_iso_instants_are_read_as_utc(self):
        expected = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        for text in ("2026-08-10T09:00:00Z", "2026-08-10T09:00:00+00:00", "2026-08-10T11:00:00+02:00"):
            with self.subTest(text=text):
                self.assertEqual(expected, RULES.datetime_from_value(text))

    def test_a_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(
            datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc),
            RULES.datetime_from_value("2026-08-10T09:00:00"),
        )

    def test_a_date_only_value_becomes_midnight_utc(self):
        """Conservative on purpose: midnight can only make a source look older."""
        midnight = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(midnight, RULES.datetime_from_value("2026-08-10"))
        self.assertEqual(midnight, RULES.datetime_from_value(date(2026, 8, 10)))

    def test_unreadable_values_are_none(self):
        for value in (None, "", "   ", "yesterday", 1754820000, [], {"a": 1}):
            with self.subTest(value=value):
                self.assertIsNone(RULES.datetime_from_value(value))


class MaxAgeEvaluationTests(unittest.TestCase):
    def rule(self, hours: float = 48):
        return parse_one({"max_age": {"field": "provenance/retrieved_at", "hours": hours}})

    def evaluate(self, hours_old: float, hours: float = 48):
        return RULES.evaluate_rule(self.rule(hours), context(provenance=retrieved_hours_ago(hours_old)))

    def test_inside_the_window_passes(self):
        evaluation = self.evaluate(47)
        self.assertTrue(evaluation.passed, evaluation.reasons)
        self.assertNotIn("rule_", evaluation.reasons[0].split(":")[0])

    def test_the_bound_itself_passes(self):
        self.assertTrue(self.evaluate(48).passed)

    def test_outside_the_window_fails_as_stale(self):
        evaluation = self.evaluate(50)
        self.assertFalse(evaluation.passed)
        reason = evaluation.reasons[0]
        self.assertTrue(reason.startswith("rule_stale: src-x provenance/retrieved_at "), reason)
        self.assertIn("50.0h old", reason)
        self.assertIn("max_age allows 48h", reason)

    def test_a_future_timestamp_beyond_skew_fails_rather_than_reading_as_fresh(self):
        evaluation = self.evaluate(-1)
        self.assertFalse(evaluation.passed)
        self.assertIn("rule_future_timestamp", evaluation.reasons[0])
        self.assertIn(str(RULES.MAX_AGE_FUTURE_SKEW_MINUTES), evaluation.reasons[0])

    def test_a_future_timestamp_within_skew_passes(self):
        minutes = RULES.MAX_AGE_FUTURE_SKEW_MINUTES - 1
        self.assertTrue(self.evaluate(-minutes / 60).passed)

    def test_the_skew_bound_is_not_pack_configurable(self):
        errors = RULES.declaration_errors(
            pack(freshness_rule({"max_age": {"field": "provenance/x", "hours": 1, "future_skew_minutes": 60}}))
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("unknown key(s): future_skew_minutes", errors[0])

    def test_an_unparseable_timestamp_fails_closed(self):
        evaluation = RULES.evaluate_rule(
            self.rule(), context(provenance={"retrieved_at": "last tuesday"})
        )
        self.assertFalse(evaluation.passed)
        self.assertIn(RULES.REASON_FIELD_UNRESOLVED, evaluation.reasons[0])
        self.assertIn("last tuesday", evaluation.reasons[0])

    def test_a_missing_provenance_key_fails_closed(self):
        evaluation = RULES.evaluate_rule(self.rule(), context(provenance={}))
        self.assertFalse(evaluation.passed)
        reason = evaluation.reasons[0]
        self.assertTrue(reason.startswith(f"{RULES.REASON_FIELD_UNRESOLVED}:"), reason)
        self.assertIn("merged provenance", reason, "the reason names the document actually walked")

    def test_a_yaml_date_in_provenance_is_read_as_midnight_utc(self):
        evaluation = RULES.evaluate_rule(
            self.rule(hours=48), context(provenance={"retrieved_at": date(2026, 8, 10)})
        )
        self.assertTrue(evaluation.passed, evaluation.reasons)
        self.assertIn("2026-08-10", evaluation.reasons[0])


class EqualsEvaluationTests(unittest.TestCase):
    def rule(self, **operand: Any):
        return parse_one({"equals": {"field": "record/supplier_quote/sku", **operand}})

    def view(self, sku: Any) -> dict[str, Any]:
        return {"supplier_quote": {"sku": sku}}

    def test_a_literal_operand_decides_both_ways(self):
        rule = self.rule(value="B0ABC123")
        self.assertTrue(RULES.evaluate_rule(rule, context(structured_view=self.view("B0ABC123"))).passed)
        miss = RULES.evaluate_rule(rule, context(structured_view=self.view("B0XYZ999")))
        self.assertFalse(miss.passed)
        self.assertIn(RULES.REASON_VALUE_MISMATCH, miss.reasons[0])
        self.assertIn("B0XYZ999", miss.reasons[0])

    def test_a_question_field_operand_decides_both_ways(self):
        rule = self.rule(question_field="metadata/candidate_sku")
        hit = RULES.evaluate_rule(
            rule,
            context(
                structured_view=self.view("B0ABC123"),
                question_frontmatter={"metadata": {"candidate_sku": "B0ABC123"}},
            ),
        )
        self.assertTrue(hit.passed, hit.reasons)
        miss = RULES.evaluate_rule(
            rule,
            context(
                structured_view=self.view("B0ABC123"),
                question_frontmatter={"metadata": {"candidate_sku": "B0XYZ999"}},
            ),
        )
        self.assertFalse(miss.passed)
        self.assertIn("question/metadata/candidate_sku", miss.reasons[0])

    def test_a_missing_question_key_fails_closed(self):
        rule = self.rule(question_field="metadata/candidate_sku")
        evaluation = RULES.evaluate_rule(
            rule, context(structured_view=self.view("B0ABC123"), question_frontmatter={"metadata": {}})
        )
        self.assertFalse(evaluation.passed)
        reason = evaluation.reasons[0]
        self.assertIn(RULES.REASON_FIELD_UNRESOLVED, reason)
        self.assertIn("question frontmatter", reason)

    def test_a_question_with_no_frontmatter_at_all_fails_closed(self):
        rule = self.rule(question_field="metadata/candidate_sku")
        evaluation = RULES.evaluate_rule(
            rule, context(structured_view=self.view("B0ABC123"), question_frontmatter=None)
        )
        self.assertFalse(evaluation.passed)
        self.assertIn("no frontmatter", evaluation.reasons[0])

    def test_numbers_compare_as_decimals_not_as_text(self):
        rule = parse_one({"equals": {"field": "record/supplier_quote/price", "value": "23.99"}})
        matching = RULES.evaluate_rule(
            rule, context(structured_view={"supplier_quote": {"price": 23.99}})
        )
        self.assertTrue(matching.passed, matching.reasons)
        near_miss = RULES.evaluate_rule(
            rule, context(structured_view={"supplier_quote": {"price": 23.990001}})
        )
        self.assertFalse(near_miss.passed, "decimal canonicalization is equality, not proximity")

    def test_equality_never_degrades_to_containment(self):
        rule = self.rule(value="B0")
        evaluation = RULES.evaluate_rule(rule, context(structured_view=self.view("B0ABC123")))
        self.assertFalse(evaluation.passed)

    def test_a_yaml_date_in_frontmatter_canonicalizes_to_its_iso_form(self):
        rule = parse_one(
            {"equals": {"field": "record/published_on", "question_field": "metadata/as_of"}}
        )
        evaluation = RULES.evaluate_rule(
            rule,
            context(
                structured_view={"published_on": "2026-08-01"},
                question_frontmatter={"metadata": {"as_of": date(2026, 8, 1)}},
            ),
        )
        self.assertTrue(evaluation.passed, evaluation.reasons)


class NumericRangeEvaluationTests(unittest.TestCase):
    def rule(self, **bounds: Any):
        return parse_one({"numeric_range": {"field": "record/supplier_quote/price", **bounds}})

    def view(self, price: Any) -> dict[str, Any]:
        return {"supplier_quote": {"price": price}}

    def test_bounds_are_inclusive_on_both_ends(self):
        rule = self.rule(min=10, max=20)
        for price in (10, 15, 20, "10.000", 19.99):
            with self.subTest(price=price):
                self.assertTrue(RULES.evaluate_rule(rule, context(structured_view=self.view(price))).passed)
        for price in (9.99, 20.01):
            with self.subTest(price=price):
                evaluation = RULES.evaluate_rule(rule, context(structured_view=self.view(price)))
                self.assertFalse(evaluation.passed)
                self.assertIn(RULES.REASON_OUT_OF_RANGE, evaluation.reasons[0])
                self.assertIn("min 10, max 20", evaluation.reasons[0])

    def test_a_single_bound_is_enough(self):
        rule = self.rule(max=20)
        self.assertTrue(RULES.evaluate_rule(rule, context(structured_view=self.view(-500))).passed)
        self.assertFalse(RULES.evaluate_rule(rule, context(structured_view=self.view(21))).passed)

    def test_bounds_can_come_from_the_question(self):
        rule = self.rule(max_question_field="budget/ceiling")
        inside = RULES.evaluate_rule(
            rule,
            context(structured_view=self.view(18), question_frontmatter={"budget": {"ceiling": 20}}),
        )
        self.assertTrue(inside.passed, inside.reasons)
        outside = RULES.evaluate_rule(
            rule,
            context(structured_view=self.view(25), question_frontmatter={"budget": {"ceiling": 20}}),
        )
        self.assertFalse(outside.passed)

    def test_a_non_numeric_question_bound_fails_closed(self):
        rule = self.rule(max_question_field="budget/ceiling")
        evaluation = RULES.evaluate_rule(
            rule,
            context(structured_view=self.view(18), question_frontmatter={"budget": {"ceiling": "lots"}}),
        )
        self.assertFalse(evaluation.passed)
        self.assertIn(RULES.REASON_OUT_OF_RANGE, evaluation.reasons[0])
        self.assertIn("question/budget/ceiling", evaluation.reasons[0])

    def test_a_non_numeric_target_fails_closed(self):
        rule = self.rule(min=1)
        for price in ("23.99 EUR", True, None):
            with self.subTest(price=price):
                evaluation = RULES.evaluate_rule(rule, context(structured_view=self.view(price)))
                self.assertFalse(evaluation.passed)
                self.assertIn(RULES.REASON_OUT_OF_RANGE, evaluation.reasons[0])


class RegexEvaluationTests(unittest.TestCase):
    def rule(self, pattern: str):
        return parse_one({"regex": {"field": "record/supplier_quote/sku", "pattern": pattern}})

    def view(self, sku: str) -> dict[str, Any]:
        return {"supplier_quote": {"sku": sku}}

    def test_a_full_match_passes(self):
        evaluation = RULES.evaluate_rule(self.rule(r"B0[A-Z0-9]{8}"), context(structured_view=self.view("B0ABC12345")))
        self.assertTrue(evaluation.passed, evaluation.reasons)

    def test_a_substring_match_does_not_pass(self):
        """`search` would reintroduce the containment weakness anchors exist to remove."""
        evaluation = RULES.evaluate_rule(self.rule("B0"), context(structured_view=self.view("XX-B0-YY")))
        self.assertFalse(evaluation.passed)
        self.assertIn(RULES.REASON_REGEX_MISMATCH, evaluation.reasons[0])
        self.assertIn("XX-B0-YY", evaluation.reasons[0])

    def test_declared_permissiveness_does_pass(self):
        evaluation = RULES.evaluate_rule(self.rule(".*B0.*"), context(structured_view=self.view("XX-B0-YY")))
        self.assertTrue(evaluation.passed, evaluation.reasons)

    def test_a_number_is_matched_through_its_canonical_rendering(self):
        rule = parse_one({"regex": {"field": "record/supplier_quote/price", "pattern": r"\d+\.\d{2}"}})
        evaluation = RULES.evaluate_rule(rule, context(structured_view={"supplier_quote": {"price": 23.99}}))
        self.assertTrue(evaluation.passed, evaluation.reasons)


class ProvenanceEvaluationTests(unittest.TestCase):
    def rule(self, **lists: Any):
        return parse_one({"one_of_provenance": lists})

    def test_a_provider_id_hit_passes(self):
        rule = self.rule(providers=["aliexpress-ds", "partner-catalog"])
        evaluation = RULES.evaluate_rule(rule, context(provider_ids=("partner-catalog",)))
        self.assertTrue(evaluation.passed, evaluation.reasons)
        self.assertIn("partner-catalog", evaluation.reasons[0])

    def test_a_fetch_agent_id_is_not_a_provider_id(self):
        """`retrieved_by` names the agent, so it never reaches `provider_ids`."""
        rule = self.rule(providers=["keepa"])
        evaluation = RULES.evaluate_rule(rule, context(provider_ids=("fixture-agent/keepa",)))
        self.assertFalse(evaluation.passed, evaluation.reasons)

    def test_a_domain_hit_goes_through_the_injected_matcher(self):
        rule = self.rule(domains=["supplier.example"])
        calls: list[tuple[str, str]] = []

        def matcher(host: str, domain: str) -> bool:
            calls.append((host, domain))
            return POLICIES.domain_matches(host, domain)

        exact = RULES.evaluate_rule(rule, context(origin_host="supplier.example", domain_matches=matcher))
        self.assertTrue(exact.passed, exact.reasons)
        subdomain = RULES.evaluate_rule(
            rule, context(origin_host="eu.cdn.supplier.example", domain_matches=matcher)
        )
        self.assertTrue(subdomain.passed, subdomain.reasons)
        self.assertEqual([("supplier.example", "supplier.example"),
                          ("eu.cdn.supplier.example", "supplier.example")], calls)

    def test_a_lookalike_host_does_not_match(self):
        rule = self.rule(domains=["supplier.example"])
        evaluation = RULES.evaluate_rule(
            rule, context(origin_host="notsupplier.example", domain_matches=POLICIES.domain_matches)
        )
        self.assertFalse(evaluation.passed)

    def test_neither_list_matching_fails_and_names_both(self):
        rule = self.rule(providers=["aliexpress-ds"], domains=["supplier.example"])
        evaluation = RULES.evaluate_rule(
            rule, context(provider_ids=("other-ds",), origin_host="elsewhere.test")
        )
        self.assertFalse(evaluation.passed)
        reason = evaluation.reasons[0]
        self.assertTrue(reason.startswith(f"{RULES.REASON_PROVENANCE_NOT_ALLOWED}: src-x "), reason)
        self.assertIn("aliexpress-ds", reason)
        self.assertIn("supplier.example", reason)
        self.assertIn("other-ds", reason)
        self.assertIn("elsewhere.test", reason)


class StructuredViewFailureTests(unittest.TestCase):
    """A missing sidecar fails record-rooted checks and leaves the rest alone."""

    def mixed_rule(self):
        return parse_one(
            {"max_age": {"field": "provenance/retrieved_at", "hours": 48}},
            {"equals": {"field": "record/supplier_quote/sku", "value": "B0ABC123"}},
        )

    def test_record_rooted_checks_fail_with_the_sidecars_own_code(self):
        evaluation = RULES.evaluate_rule(
            self.mixed_rule(),
            context(structured_view=None, provenance=retrieved_hours_ago(1)),
        )
        self.assertFalse(evaluation.passed)
        self.assertEqual(1, len(evaluation.reasons), "the provenance leaf was unaffected")
        self.assertTrue(
            evaluation.reasons[0].startswith(f"{RULES.RESULT_STRUCTURED_VIEW_MISSING}: src-x record/"),
            evaluation.reasons[0],
        )

    def test_the_callers_own_load_failure_is_echoed(self):
        evaluation = RULES.evaluate_rule(
            self.mixed_rule(),
            context(
                structured_view=None,
                structured_view_error=(
                    RULES.RESULT_STRUCTURED_VIEW_CORRUPT,
                    "The structured view at sources/normalized/x.structured.json hashes to sha256:beef.",
                ),
                provenance=retrieved_hours_ago(1),
            ),
        )
        self.assertFalse(evaluation.passed)
        self.assertIn(RULES.RESULT_STRUCTURED_VIEW_CORRUPT, evaluation.reasons[0])
        self.assertIn("hashes to sha256:beef", evaluation.reasons[0])

    def test_a_pointer_reaching_a_subtree_fails_closed(self):
        rule = parse_one({"equals": {"field": "record/supplier_quote", "value": "B0ABC123"}})
        evaluation = RULES.evaluate_rule(
            rule, context(structured_view={"supplier_quote": {"sku": "B0ABC123"}})
        )
        self.assertFalse(evaluation.passed)
        self.assertIn(RULES.REASON_FIELD_UNRESOLVED, evaluation.reasons[0])
        self.assertIn("object", evaluation.reasons[0])

    def test_resolve_field_reports_the_same_verdicts_directly(self):
        ref = RULES.FieldRef("record", "/supplier_quote/sku")
        resolved = RULES.resolve_field(context(structured_view={"supplier_quote": {"sku": "B0"}}), ref)
        self.assertIsInstance(resolved, RULES.ResolvedValue)
        self.assertEqual("B0", resolved.value)
        failure = RULES.resolve_field(context(structured_view={}), ref)
        self.assertIsInstance(failure, RULES.ResolutionFailure)
        self.assertEqual(RULES.REASON_FIELD_UNRESOLVED, failure.code)


class CompositionTests(unittest.TestCase):
    def price_between(self, minimum: int, maximum: int) -> dict[str, Any]:
        return {"numeric_range": {"field": "record/price", "min": minimum, "max": maximum}}

    def test_all_of_collects_every_failing_leaf(self):
        rule = parse_one(
            {"equals": {"field": "record/sku", "value": "B0ABC123"}},
            {"regex": {"field": "record/sku", "pattern": "ZZ.*"}},
            {"max_age": {"field": "provenance/retrieved_at", "hours": 1}},
        )
        evaluation = RULES.evaluate_rule(
            rule, context(structured_view={"sku": "B0XYZ999"}, provenance=retrieved_hours_ago(10))
        )
        self.assertFalse(evaluation.passed)
        self.assertEqual(3, len(evaluation.reasons), evaluation.reasons)
        self.assertEqual(
            [RULES.REASON_VALUE_MISMATCH, RULES.REASON_REGEX_MISMATCH, RULES.REASON_STALE],
            [reason.split(":", 1)[0] for reason in evaluation.reasons],
        )

    def test_a_passing_all_of_reports_its_satisfied_leaves_without_a_failure_prefix(self):
        rule = parse_one(
            {"equals": {"field": "record/sku", "value": "B0ABC123"}},
            {"max_age": {"field": "provenance/retrieved_at", "hours": 48}},
        )
        evaluation = RULES.evaluate_rule(
            rule, context(structured_view={"sku": "B0ABC123"}, provenance=retrieved_hours_ago(1))
        )
        self.assertTrue(evaluation.passed)
        self.assertEqual(2, len(evaluation.reasons))
        for reason in evaluation.reasons:
            self.assertFalse(reason.startswith(RULES.RULE_REASON_PREFIXES), reason)

    def test_any_of_short_circuits_and_records_the_winning_branch(self):
        declaration = pack(
            {
                "pack:market-data/quote-48h": {
                    "any_of": [
                        {"equals": {"field": "record/sku", "value": "NEVER"}},
                        {"equals": {"field": "record/sku", "value": "B0ABC123"}},
                        {"equals": {"field": "record/sku", "value": "ALSO-NEVER"}},
                    ]
                }
            }
        )
        self.assertEqual([], RULES.declaration_errors(declaration))
        rule = RULES.pack_policy_rules({"domain_pack": declaration})["pack:market-data/quote-48h"]
        evaluation = RULES.evaluate_rule(rule, context(structured_view={"sku": "B0ABC123"}))
        self.assertTrue(evaluation.passed)
        self.assertEqual(1, len(evaluation.reasons))
        self.assertIn("B0ABC123", evaluation.reasons[0])

    def test_a_failing_any_of_reports_every_branch(self):
        declaration = pack(
            {
                "pack:market-data/quote-48h": {
                    "any_of": [
                        {"equals": {"field": "record/sku", "value": "NEVER"}},
                        {"equals": {"field": "record/sku", "value": "ALSO-NEVER"}},
                    ]
                }
            }
        )
        rule = RULES.pack_policy_rules({"domain_pack": declaration})["pack:market-data/quote-48h"]
        evaluation = RULES.evaluate_rule(rule, context(structured_view={"sku": "B0ABC123"}))
        self.assertFalse(evaluation.passed)
        self.assertEqual(2, len(evaluation.reasons))

    def test_nested_all_of_over_any_of_follows_the_truth_table(self):
        rule = parse_one(
            {"max_age": {"field": "provenance/retrieved_at", "hours": 48}},
            {"any_of": [self.price_between(1, 10), self.price_between(100, 200)]},
        )
        cases = {
            (True, 5): True,
            (True, 150): True,
            (True, 50): False,
            (False, 5): False,
            (False, 50): False,
        }
        for (fresh, price), expected in cases.items():
            with self.subTest(fresh=fresh, price=price):
                evaluation = RULES.evaluate_rule(
                    rule,
                    context(
                        structured_view={"price": price},
                        provenance=retrieved_hours_ago(1 if fresh else 100),
                    ),
                )
                self.assertEqual(expected, evaluation.passed, evaluation.reasons)

    def test_every_failure_reason_carries_a_stable_prefix(self):
        rule = parse_one(
            {"max_age": {"field": "provenance/retrieved_at", "hours": 1}},
            {"equals": {"field": "record/sku", "value": "B0ABC123"}},
            {"numeric_range": {"field": "record/price", "min": 1000}},
            {"regex": {"field": "record/sku", "pattern": "ZZ.*"}},
            {"one_of_provenance": {"providers": ["nobody"]}},
        )
        evaluation = RULES.evaluate_rule(
            rule,
            context(structured_view={"sku": "B0XYZ", "price": 5}, provenance=retrieved_hours_ago(99)),
        )
        self.assertFalse(evaluation.passed)
        self.assertEqual(5, len(evaluation.reasons))
        for reason in evaluation.reasons:
            self.assertTrue(reason.startswith(RULES.RULE_REASON_PREFIXES), reason)
            self.assertIn("src-x", reason)


class ContextTests(unittest.TestCase):
    def test_a_naive_clock_is_read_as_utc(self):
        naive = context(now=datetime(2026, 8, 10, 12, 0))
        self.assertEqual(NOW, naive.now)

    def test_a_clock_that_is_not_a_datetime_is_refused_at_construction(self):
        with self.assertRaises(TypeError):
            context(now="2026-08-10T12:00:00Z")

    def test_provider_ids_are_normalized_to_a_tuple(self):
        self.assertEqual(("a", "b"), context(provider_ids=["a", "b"]).provider_ids)


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
