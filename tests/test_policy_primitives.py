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
import json
import subprocess
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

#: Printed by the child below once every pattern has matched, so the parent can tell
#: "finished" from "was killed partway".
ADVERSARIAL_MATCH_DONE = "__all-patterns-matched__"

#: Runs the accepted patterns against adversarial text in a separate process, so a
#: pattern that backtracks catastrophically is killed by the parent's timeout instead
#: of hanging the suite. Each pattern is announced before it is tried, and stdout is
#: unbuffered, so the parent can name the pattern that hung from the partial output.
ADVERSARIAL_MATCH_CHILD = f"""
import json, re, sys

payload = json.load(sys.stdin)
for pattern in payload["patterns"]:
    compiled = re.compile(pattern)
    for text in payload["texts"]:
        print(pattern, flush=True)
        compiled.fullmatch(text)
print({ADVERSARIAL_MATCH_DONE!r}, flush=True)
"""


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

    def test_when_absent_is_accepted_only_on_record_field_leaves(self):
        leaves = (
            {"max_age": {"field": "record/published_at", "hours": 1}},
            {"equals": {"field": "record/sku", "value": "A"}},
            {"numeric_range": {"field": "record/price", "min": 1}},
            {"regex": {"field": "record/sku", "pattern": "A"}},
        )
        for leaf in leaves:
            with self.subTest(leaf=next(iter(leaf))):
                body = next(iter(leaf.values()))
                body["when_absent"] = "manual_review"
                rule = parse_one(leaf)
                self.assertEqual(
                    RULES.WHEN_ABSENT_MANUAL_REVIEW,
                    rule.composition.children[0].when_absent,
                )

        explicit_fail = parse_one(
            {
                "equals": {
                    "field": "record/sku",
                    "value": "A",
                    "when_absent": "fail",
                }
            }
        )
        self.assertEqual(RULES.WHEN_ABSENT_FAIL, explicit_fail.composition.children[0].when_absent)

    def test_when_absent_defaults_to_fail_and_rejects_unknown_or_pass_values(self):
        default = parse_one({"equals": {"field": "record/sku", "value": "A"}})
        self.assertEqual(RULES.WHEN_ABSENT_FAIL, default.composition.children[0].when_absent)

        for value in ("pass", "review", True, None, ["manual_review"]):
            with self.subTest(value=value):
                message = self.only_error(
                    {
                        "equals": {
                            "field": "record/sku",
                            "value": "A",
                            "when_absent": value,
                        }
                    }
                )
                self.assertIn("pack:market-data/quote-48h", message)
                self.assertIn(".equals.when_absent", message)
                self.assertIn("fail, manual_review", message)

    def test_when_absent_rejects_provenance_and_non_field_primitives(self):
        provenance = self.only_error(
            {
                "max_age": {
                    "field": "provenance/retrieved_at",
                    "hours": 1,
                    "when_absent": "manual_review",
                }
            }
        )
        self.assertIn("only when", provenance)
        self.assertIn("record/", provenance)

        one_of = self.only_error(
            {
                "one_of_provenance": {
                    "providers": ["supplier"],
                    "when_absent": "manual_review",
                }
            }
        )
        self.assertIn("unknown key(s): when_absent", one_of)

        top_level = RULES.declaration_errors(
            pack(
                freshness_rule(
                    {"equals": {"field": "record/sku", "value": "A"}},
                    when_absent="manual_review",
                )
            )
        )
        self.assertEqual(1, len(top_level), top_level)
        self.assertIn("unknown key(s): when_absent", top_level[0])

    def test_rule_summary_reports_nested_manual_review_on_absence(self):
        deterministic = parse_one({"equals": {"field": "record/sku", "value": "A"}})
        self.assertEqual(
            {
                "primitives": ["all_of", "equals"],
                "manual_review_required": False,
                "manual_review_on_absence": False,
                "record_fields_that_may_traverse_arrays": [],
                "section": "freshness_policy",
            },
            RULES.rule_summary(deterministic),
        )

        conditional = parse_one(
            {
                "any_of": [
                    {"equals": {"field": "record/sku", "value": "A"}},
                    {
                        "all_of": [
                            {
                                "regex": {
                                    "field": "record/gtin",
                                    "pattern": "[0-9]+",
                                    "when_absent": "manual_review",
                                }
                            }
                        ]
                    },
                ]
            }
        )
        self.assertTrue(RULES.rule_summary(conditional)["manual_review_on_absence"])

    def array_candidates(self, *primitives: dict[str, Any]) -> list[str]:
        return RULES.rule_summary(parse_one(*primitives))["record_fields_that_may_traverse_arrays"]

    def test_rule_summary_names_record_paths_that_could_reach_through_an_array(self):
        self.assertEqual(
            ["record/price_history/series/0/close"],
            self.array_candidates({"equals": {"field": "record/price_history/series/0/close", "value": "1"}}),
        )
        # Every depth of a composition, deduplicated across primitives that read the same
        # field, and ordered so two runs of the same pack report the same list.
        self.assertEqual(
            ["record/offers/0/price", "record/offers/12/price"],
            self.array_candidates(
                {"regex": {"field": "record/offers/12/price", "pattern": "[0-9]+"}},
                {
                    "any_of": [
                        {"equals": {"field": "record/offers/0/price", "value": "1"}},
                        {"numeric_range": {"field": "record/offers/0/price", "min": 1}},
                    ]
                },
            ),
        )

    def test_rule_summary_names_no_path_that_the_record_rule_can_refuse(self):
        # `0` is the boundary the walk accepts; a leading zero, a sign and the RFC's
        # append token are not array steps, so a rule using them cannot be refused for
        # traversing one and must not be reported as if it could.
        self.assertEqual([], self.array_candidates({"equals": {"field": "record/series/00/x", "value": "1"}}))
        self.assertEqual([], self.array_candidates({"equals": {"field": "record/series/-/x", "value": "1"}}))
        self.assertEqual([], self.array_candidates({"equals": {"field": "record/series/1.0/x", "value": "1"}}))
        self.assertEqual(
            ["record/series/0/x"],
            self.array_candidates({"equals": {"field": "record/series/0/x", "value": "1"}}),
        )
        # The mapping-only rule governs `record` alone, so naming any other root would
        # send an author to check a path no hardening can refuse.
        self.assertEqual([], self.array_candidates({"max_age": {"field": "provenance/0/retrieved_at", "hours": 1}}))
        self.assertEqual(
            [],
            self.array_candidates({"equals": {"field": "record/sku", "question_field": "metadata/skus/0/id"}}),
        )

    def test_every_rule_the_record_hardening_refuses_is_named_by_its_summary(self):
        """The property the report is worth trusting for: no silent affected rule.

        A pack upgrading from an array-backed declaration reads this list to decide
        whether the mapping-only record rule reaches it. If a refusal could happen
        without the path appearing here, an empty list would be the same false
        reassurance as reporting nothing at all.
        """
        view = {"price_history": {"series": [{"close": "10"}]}}
        rule = parse_one({"equals": {"field": "record/price_history/series/0/close", "value": "10"}})

        evaluation = RULES.evaluate_rule(rule, context(structured_view=view))
        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome, evaluation)
        self.assertTrue(
            any("traverses a JSON array" in reason for reason in evaluation.reasons),
            evaluation.reasons,
        )
        self.assertIn(
            "record/price_history/series/0/close",
            RULES.rule_summary(rule)["record_fields_that_may_traverse_arrays"],
        )

        # The same path is correct, and still reported, when the step is a mapping key.
        # This is why the report cannot become a refusal.
        mapping_view = {"price_history": {"series": {"0": {"close": "10"}}}}
        self.assertTrue(RULES.evaluate_rule(rule, context(structured_view=mapping_view)).passed)

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

    def test_a_catastrophically_backtracking_pattern_is_refused(self):
        """`(a+)+` and `(a|a)+` are six characters, so a length cap cannot bound this.

        Both exponential families: a repeated group whose body also repeats without
        bound, and one whose alternatives can match the same text.
        """
        for pattern in ("(a+)+$", "([A-Za-z0-9]+ ?)+", "(a*)*", "(x{2,})+", "(a|a)+$", "(a|ab)*$"):
            with self.subTest(pattern=pattern):
                self.assertIn(
                    "repeats a group that already repeats",
                    self.only_error({"regex": {"field": "record/sku", "pattern": pattern}}),
                )

    def test_alternative_overlap_is_refused_in_every_spelling(self):
        """The overlap family must be refused however the group or its leads are spelled.

        Each of these timed exponential against `re.fullmatch` while an earlier guard
        accepted it: a group-modifier prefix read as the first alternative's lead
        (`(?:a|a)+`, `(?i:a|A)+` — IGNORECASE makes case-distinct leads the same text —
        and `(?P<x>a|a)+`), an alternative that is itself a group (`((a)|a)+`), and an
        alternative opening with an escape the syntax scan cannot see through
        (`(\\da|1a)+` — `\\d` begins like `1`, but the scan compared `a` against `1`).
        An empty alternative and a `?`-group with no ordinary body are refused on the
        same conservative footing rather than proven exponential.
        """
        for pattern in (
            "(?:a|a)+",
            "(?i:a|A)+",
            "(?P<x>a|a)+",
            "((a)|a)+",
            r"(\da|1a)+",
            "(|a)+",
            "(a||b)+",
            "(?=a|b)+",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(
                    "repeats a group that already repeats",
                    self.only_error({"regex": {"field": "record/sku", "pattern": pattern}}),
                )

    def test_an_optional_lead_does_not_hide_an_overlapping_alternative(self):
        """`b?` can match empty, so `(b?a|a)+` really has two branches beginning `a`.

        A guard comparing only the written first token found `b` and `a` distinct and
        accepted these; each then timed exponential against `re.fullmatch` on a field of
        26 characters (`(b?a|a)+c`: 4655 ms, doubling per character added).
        """
        for pattern in ("(b?a|a)+c", "(b{0,3}a|a)+c", "(a|b?a)+c", "(b{,3}a|a)+c"):
            with self.subTest(pattern=pattern):
                self.assertIn(
                    "repeats a group that already repeats",
                    self.only_error({"regex": {"field": "record/sku", "pattern": pattern}}),
                )

    def test_an_optional_lead_is_refused_even_where_it_was_safe(self):
        """The documented price of the rule above, pinned so it is a choice not a drift.

        An optional lead reports "unknowable" rather than resolving the set of tokens
        the alternative can really start with, so `(b?a|c)+` — whose branches cannot
        both match the same text — is refused with the exponential ones. Conservative
        in the direction this module always chooses, and cheap to rewrite; recorded
        here so a future contributor sees the cost and can decide to pay it down.
        """
        self.assertIn(
            "repeats a group that already repeats",
            self.only_error({"regex": {"field": "record/sku", "pattern": "(b?a|c)+"}}),
        )

    def test_a_wrapping_group_does_not_hide_an_overlapping_alternative(self):
        """Ambiguity nested one group deeper is the same ambiguity.

        `((a|a))+c` blows up exactly as `(a|a)+c` does — each repetition still chooses
        between the branches — but a check reading only the repeated group's own
        top-level `|` saw none and accepted it (3017 ms on 26 characters; `(?:(a|a))+c`
        2536 ms). The alternation scan is depth-agnostic, as the repeat scan beside it
        has always been.
        """
        for pattern in ("((a|a))+c", "(?:(a|a))+c", "(x(a|a))+c", "((((a|a))))+c"):
            with self.subTest(pattern=pattern):
                self.assertIn(
                    "repeats a group that already repeats",
                    self.only_error({"regex": {"field": "record/sku", "pattern": pattern}}),
                )

    def test_an_atomic_group_is_not_refused_for_its_own_alternatives(self):
        """`(?>...)` is the standard repair for this defect, so it must stay available.

        The engine never re-enters an atomic group on backtracking, so its branches
        cannot multiply an outer repetition's choices: `(?>a|a)+` matches 26 characters
        in 0.018 ms. Refusing the repair alongside the bug would leave a pack author
        with nowhere to go.

        `re` grew atomic groups in 3.11; the companion test below covers what an older
        interpreter does with the same pattern, which is refuse it as invalid syntax.
        """
        if sys.version_info < (3, 11):
            self.skipTest("`re` gained atomic groups in Python 3.11")
        for pattern in ("(?>a|a)+", "(?>(a|a))+"):
            with self.subTest(pattern=pattern):
                self.assertEqual([], self.errors_for({"regex": {"field": "record/sku", "pattern": pattern}}))

    def test_an_atomic_group_is_refused_as_invalid_before_python_311(self):
        """Where `re` has no atomic group, the pattern is refused for the honest reason.

        The guard does not flag `(?>a|a)+` as a nested quantifier on any version — the
        construct cannot backtrack — but before 3.11 `re.compile` does not recognize it
        at all, so `_parse_regex` refuses it as an invalid expression. Fail-closed
        either way; only the message differs.
        """
        if sys.version_info >= (3, 11):
            self.skipTest("this interpreter's `re` supports atomic groups")
        self.assertIn(
            "not a valid regular expression",
            self.only_error({"regex": {"field": "record/sku", "pattern": "(?>a|a)+"}}),
        )

    def test_case_folding_of_leads_is_scoped_to_ignorecase(self):
        """`re` keeps `a` and `A` apart unless a flag says otherwise, and so does this.

        Folding every pattern's leads refused `(a|A)+c` — measured linear — for a
        property it does not have. The fold follows the group's own scope, in both
        directions: a scoped `(?-i:` turns folding back off inside a pattern that
        switched it on globally, exactly as the flag does for `re` itself.
        """
        for pattern in ("(a|A)+c", "(?-i:a|A)+c", "(?i)(?-i:a|A)+c", "(?i:(?-i:a|A))+c", "(?s-i:a|A)+c"):
            with self.subTest(pattern=pattern):
                self.assertEqual([], self.errors_for({"regex": {"field": "record/sku", "pattern": pattern}}))
        for pattern in ("(?i:a|A)+c", "(?i)(a|A)+c", "(?i)(?:a|A)+c", "(?i-s:a|A)+c"):
            with self.subTest(pattern=pattern):
                self.assertIn(
                    "repeats a group that already repeats",
                    self.only_error({"regex": {"field": "record/sku", "pattern": pattern}}),
                )

    def test_every_accepted_pattern_matches_adversarial_input_quickly(self):
        """The property the refusals exist for, asserted on what actually ships.

        Enumerating exponential spellings only ever catches the ones somebody thought
        of — which is how three of them survived a previous round. This asserts the
        complement instead: whatever the guard *accepts* must not blow up on
        source-controlled text.

        The matching runs in a child process under a hard timeout, because the failure
        being guarded against is unbounded: an accepted pattern that backtracks
        catastrophically would hang an in-process `fullmatch` forever, and a wall-clock
        assertion after the call never runs. Killing the child turns that hang into a
        fast, named failure on every platform the matrix covers. The budget is generous
        against patterns that finish in microseconds, so it does not flake on a slow
        runner, and the child reports each pattern before trying it so a timeout names
        the one that hung.
        """
        patterns = [
            r"(B0|B1)[A-Z0-9]{8}",
            r"(?:sku-)?\d+",
            r"(\d{2}-)+",
            r"(a{1,3})+",
            r"(foo|bar)+",
            r"(?:foo|bar)+",
            r"([]+]a)+",
            r"(a\|a)+",
            r"(a|A)+c",
        ]
        if sys.version_info >= (3, 11):
            patterns.append(r"(?>a|a)+")
        for pattern in patterns:
            self.assertEqual([], self.errors_for({"regex": {"field": "record/sku", "pattern": pattern}}))
        payload = json.dumps(
            {
                "patterns": patterns,
                "texts": ["a" * 3000, "ab" * 1500, "B0" * 1500, "1" * 3000, "foo" * 1000, "sku-" * 750],
            }
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", ADVERSARIAL_MATCH_CHILD],
                input=payload,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as expired:
            # `text=True` does not reach the timeout path on every version, so the
            # partial output can arrive as bytes; decoding here keeps the failure
            # message readable instead of printing a bytes repr.
            partial = expired.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            attempted = partial.strip().splitlines()
            self.fail(
                "an accepted pattern did not finish matching adversarial input; "
                f"last pattern attempted: {attempted[-1] if attempted else '<none reported>'}"
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(ADVERSARIAL_MATCH_DONE, completed.stdout)

    def test_ordinary_grouped_patterns_are_still_accepted(self):
        """A bounded inner repeat and disjoint alternatives are not catastrophic.

        `(\\d{2}-)+` gives the outer quantifier one way to match, and `(foo|bar)+`
        alternatives cannot both match the same text — refusing either would block a
        pack author from a pattern that was always safe. `([]+]a)+` is here because a
        `]` first in a class is a literal member, not the class close.
        """
        for pattern in (
            r"(B0|B1)[A-Z0-9]{8}",
            r"(?:sku-)?\d+",
            r"[A-Za-z]+(-[A-Za-z]+)?",
            r"(\d{2}-)+",
            r"(a{2})+",
            r"(a{1,3})+",
            r"(foo|bar)+",
            r"(?:foo|bar)+",
            r"(?P<x>foo|bar)+",
            r"([]+]a)+",
            r"(a\|a)+",
        ):
            with self.subTest(pattern=pattern):
                self.assertEqual([], self.errors_for({"regex": {"field": "record/sku", "pattern": pattern}}))

    def test_an_oversized_repetition_count_is_a_finding_not_a_traceback(self):
        """`re.compile` signals this with OverflowError, which is not an `re.error`."""
        self.assertIn(
            "not a valid regular expression",
            self.only_error({"regex": {"field": "record/sku", "pattern": "a{4294967296}"}}),
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

    def test_an_offsetless_timestamp_is_read_at_its_earliest_possible_instant(self):
        """A host east of UTC must not be able to make stale evidence look fresh.

        `2026-08-10T09:00:00` stamped at +14:00 is really `2026-08-09T19:00:00Z`. Reading
        it as UTC would report it 14 hours fresher than it is, which is a false pass;
        reading it at the earliest offset any zone uses can only overstate its age.
        """
        self.assertEqual(
            datetime(2026, 8, 9, 19, 0, tzinfo=timezone.utc),
            RULES.datetime_from_value("2026-08-10T09:00:00"),
        )

    def test_a_date_only_value_is_read_at_its_earliest_possible_instant(self):
        """Conservative on purpose: it can only make a source look older."""
        earliest = datetime(2026, 8, 9, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(earliest, RULES.datetime_from_value("2026-08-10"))
        self.assertEqual(earliest, RULES.datetime_from_value(date(2026, 8, 10)))

    def test_a_timestamp_at_the_floor_of_representable_time_decides(self):
        """Shifting it would underflow `datetime.min`; every bound has failed anyway."""
        for value in ("0001-01-01", "0001-01-01T02:00:00", datetime(1, 1, 1)):
            with self.subTest(value=str(value)):
                self.assertIsNotNone(RULES.datetime_from_value(value))

    def test_a_lowercase_zulu_designator_is_accepted(self):
        """RFC 3339 spells its ABNF case-insensitively; CPython's parser does not."""
        expected = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(expected, RULES.datetime_from_value("2026-08-10T09:00:00z"))

    def test_fractional_seconds_parse_on_every_supported_interpreter(self):
        """A pack's verdict must not depend on the interpreter underneath it."""
        expected = datetime(2026, 8, 10, 9, 0, 0, 123456, tzinfo=timezone.utc)
        self.assertEqual(expected, RULES.datetime_from_value("2026-08-10T09:00:00.123456789Z"))
        self.assertEqual(
            datetime(2026, 8, 10, 9, 0, 0, 120000, tzinfo=timezone.utc),
            RULES.datetime_from_value("2026-08-10T09:00:00.12Z"),
        )
        self.assertEqual(expected, RULES.datetime_from_value("2026-08-10T09:00:00.123456+0000"))

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

    def test_an_extreme_bound_decides_rather_than_raising(self):
        """A bound past the decimal context's Emax parses, so it must also evaluate.

        `normalize` raises on it, and the reason text is built before the comparison, so
        an unguarded render would turn a rule that was about to pass into a traceback.
        """
        rule = self.rule(min="1", max="1e1000000")
        evaluation = RULES.evaluate_rule(rule, context(structured_view=self.view("5")))
        self.assertTrue(evaluation.passed, evaluation.reasons)

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

    def test_a_null_field_never_satisfies_a_pattern(self):
        """`null` is what a normalizer writes when it could not extract a value.

        Matching its rendering would pass an identity check on precisely the evidence
        that is missing, so the permissive patterns a pack actually writes must not.
        """
        for pattern in ("[a-z]+", ".+", r"[\w-]+"):
            with self.subTest(pattern=pattern):
                evaluation = RULES.evaluate_rule(
                    self.rule(pattern), context(structured_view=self.view(None))
                )
                self.assertFalse(evaluation.passed, evaluation.reasons)
                self.assertIn(RULES.REASON_REGEX_MISMATCH, evaluation.reasons[0])

    def test_a_boolean_field_never_satisfies_a_pattern(self):
        evaluation = RULES.evaluate_rule(self.rule("[a-z]+"), context(structured_view=self.view(True)))
        self.assertFalse(evaluation.passed, evaluation.reasons)


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

    def test_a_provider_allowlist_ignores_case_and_nothing_else(self):
        """An identity allowlist is where prose folding must not apply — bar case.

        `expected_matches` folds case, NFKC, dashes and whitespace, which is right for
        grounding a quote against a record and wrong here: a fullwidth or en-dash
        lookalike is a different id. Case is the one fold worth keeping, because registry
        metadata spells the same provider `ISO` or `iso` depending on who wrote it.
        """
        rule = self.rule(providers=["partner-catalog"])
        for observed in ("PARTNER-CATALOG", "Partner-Catalog"):
            with self.subTest(accepted=observed):
                self.assertTrue(RULES.evaluate_rule(rule, context(provider_ids=(observed,))).passed)
        for observed in ("ｐａｒｔｎｅｒ-ｃａｔａｌｏｇ", "partner–catalog", " partner-catalog ", "partner catalog"):
            with self.subTest(refused=observed):
                evaluation = RULES.evaluate_rule(rule, context(provider_ids=(observed,)))
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

    def test_a_present_record_scalar_reached_through_an_array_is_unresolved_in_v1(self):
        ref = RULES.FieldRef("record", "/rows/0/gtin")
        resolved = RULES.resolve_field(
            context(structured_view={"rows": [{"gtin": "123"}]}), ref
        )

        self.assertIsInstance(resolved, RULES.ResolutionFailure)
        self.assertEqual(RULES.REASON_FIELD_UNRESOLVED, resolved.code)
        self.assertFalse(resolved.eligible_absence)
        self.assertIn("traverses a JSON array", resolved.detail)


class AbsenceEvaluationTests(unittest.TestCase):
    def equals_rule(
        self,
        field: str = "record/supplier_quote/gtin",
        *,
        value: Any = "1234567890123",
        question_field: str | None = None,
        when_absent: str | None = "manual_review",
    ):
        body: dict[str, Any] = {"field": field}
        if question_field is None:
            body["value"] = value
        else:
            body["question_field"] = question_field
        if when_absent is not None:
            body["when_absent"] = when_absent
        return parse_one({"equals": body})

    def test_every_field_leaf_can_review_an_eligible_terminal_absence(self):
        leaves = (
            {
                "max_age": {
                    "field": "record/supplier_quote/retrieved_at",
                    "hours": 48,
                    "when_absent": "manual_review",
                }
            },
            {
                "equals": {
                    "field": "record/supplier_quote/gtin",
                    "value": "123",
                    "when_absent": "manual_review",
                }
            },
            {
                "numeric_range": {
                    "field": "record/supplier_quote/price",
                    "min": 1,
                    "when_absent": "manual_review",
                }
            },
            {
                "regex": {
                    "field": "record/supplier_quote/sku",
                    "pattern": ".+",
                    "when_absent": "manual_review",
                }
            },
        )
        for leaf in leaves:
            with self.subTest(leaf=next(iter(leaf))):
                evaluation = RULES.evaluate_rule(
                    parse_one(leaf),
                    context(structured_view={"supplier_quote": {}}),
                )

                self.assertEqual(RULES.OUTCOME_MANUAL_REVIEW, evaluation.outcome)
                self.assertFalse(evaluation.passed)
                self.assertTrue(evaluation.requires_manual_review)
                self.assertEqual(1, len(evaluation.reasons))
                self.assertTrue(
                    evaluation.reasons[0].startswith(
                        f"{RULES.REASON_FIELD_ABSENT}: src-x record/supplier_quote/"
                    ),
                    evaluation.reasons[0],
                )
                self.assertIn("terminal member as optional", evaluation.reasons[0])
                self.assertIn("reviewer", evaluation.reasons[0])

    def test_omitted_and_explicit_fail_retain_the_existing_reason_exactly(self):
        view = {"supplier_quote": {}}
        omitted = RULES.evaluate_rule(
            self.equals_rule(when_absent=None), context(structured_view=view)
        )
        explicit = RULES.evaluate_rule(
            self.equals_rule(when_absent="fail"), context(structured_view=view)
        )

        self.assertEqual(RULES.OUTCOME_FAIL, omitted.outcome)
        self.assertEqual(omitted.reasons, explicit.reasons)
        self.assertTrue(omitted.reasons[0].startswith(RULES.REASON_FIELD_UNRESOLVED))

    def test_only_a_terminal_missing_member_reached_through_mappings_can_review(self):
        cases = (
            (
                "missing sidecar",
                self.equals_rule(),
                context(structured_view=None),
            ),
            (
                "corrupt sidecar",
                self.equals_rule(),
                context(
                    structured_view=None,
                    structured_view_error=(
                        RULES.RESULT_STRUCTURED_VIEW_CORRUPT,
                        "the structured view hash does not match",
                    ),
                ),
            ),
            (
                "missing parent",
                self.equals_rule(),
                context(structured_view={}),
            ),
            (
                "successful array traversal",
                self.equals_rule("record/rows/0/gtin"),
                context(structured_view={"rows": [{}]}),
            ),
            (
                "invalid array index",
                self.equals_rule("record/rows/no/gtin"),
                context(structured_view={"rows": [{}]}),
            ),
            (
                "array index out of range",
                self.equals_rule("record/rows/1/gtin"),
                context(structured_view={"rows": [{}]}),
            ),
            (
                "scalar traversal",
                self.equals_rule(),
                context(structured_view={"supplier_quote": "not an object"}),
            ),
            (
                "non-scalar terminal",
                self.equals_rule(),
                context(structured_view={"supplier_quote": {"gtin": {}}}),
            ),
        )
        for label, rule, rule_context in cases:
            with self.subTest(case=label):
                evaluation = RULES.evaluate_rule(rule, rule_context)

                self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome, evaluation.reasons)
                self.assertFalse(evaluation.requires_manual_review)
                self.assertFalse(
                    evaluation.reasons[0].startswith(RULES.REASON_FIELD_ABSENT),
                    evaluation.reasons,
                )

    def test_question_operand_absence_never_inherits_the_primary_field_behavior(self):
        evaluation = RULES.evaluate_rule(
            self.equals_rule(question_field="metadata/candidate_gtin"),
            context(
                structured_view={"supplier_quote": {"gtin": "1234567890123"}},
                question_frontmatter={"metadata": {}},
            ),
        )

        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome)
        self.assertIn("question/metadata/candidate_gtin", evaluation.reasons[0])
        self.assertTrue(evaluation.reasons[0].startswith(RULES.REASON_FIELD_UNRESOLVED))

    def test_missing_question_operand_dominates_simultaneous_primary_absence(self):
        evaluation = RULES.evaluate_rule(
            self.equals_rule(question_field="metadata/candidate_gtin"),
            context(
                structured_view={"supplier_quote": {}},
                question_frontmatter={"metadata": {}},
            ),
        )

        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome)
        self.assertFalse(evaluation.requires_manual_review)
        self.assertTrue(evaluation.reasons[0].startswith(RULES.REASON_FIELD_UNRESOLVED))
        self.assertIn("question/metadata/candidate_gtin", evaluation.reasons[0])
        self.assertFalse(evaluation.reasons[0].startswith(RULES.REASON_FIELD_ABSENT))

    def test_missing_numeric_bound_dominates_simultaneous_primary_absence(self):
        rule = parse_one(
            {
                "numeric_range": {
                    "field": "record/supplier_quote/price",
                    "min_question_field": "metadata/min_price",
                    "when_absent": "manual_review",
                }
            }
        )
        evaluation = RULES.evaluate_rule(
            rule,
            context(
                structured_view={"supplier_quote": {}},
                question_frontmatter={"metadata": {}},
            ),
        )

        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome)
        self.assertFalse(evaluation.requires_manual_review)
        self.assertTrue(evaluation.reasons[0].startswith(RULES.REASON_FIELD_UNRESOLVED))
        self.assertIn("question/metadata/min_price", evaluation.reasons[0])

    def test_invalid_numeric_bound_dominates_simultaneous_primary_absence(self):
        rule = parse_one(
            {
                "numeric_range": {
                    "field": "record/supplier_quote/price",
                    "max_question_field": "metadata/max_price",
                    "when_absent": "manual_review",
                }
            }
        )
        evaluation = RULES.evaluate_rule(
            rule,
            context(
                structured_view={"supplier_quote": {}},
                question_frontmatter={"metadata": {"max_price": "lots"}},
            ),
        )

        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome)
        self.assertFalse(evaluation.requires_manual_review)
        self.assertTrue(evaluation.reasons[0].startswith(RULES.REASON_OUT_OF_RANGE))
        self.assertIn("not a decimal number", evaluation.reasons[0])

    def test_a_present_matching_record_value_through_an_array_still_hard_fails(self):
        evaluation = RULES.evaluate_rule(
            self.equals_rule("record/rows/0/gtin", value="123"),
            context(structured_view={"rows": [{"gtin": "123"}]}),
        )

        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome)
        self.assertFalse(evaluation.requires_manual_review)
        self.assertTrue(evaluation.reasons[0].startswith(RULES.REASON_FIELD_UNRESOLVED))
        self.assertIn("traverses a JSON array", evaluation.reasons[0])

    def test_present_values_never_invoke_absence_handling(self):
        equals_null = RULES.evaluate_rule(
            self.equals_rule(value=None),
            context(structured_view={"supplier_quote": {"gtin": None}}),
        )
        self.assertEqual(RULES.OUTCOME_PASS, equals_null.outcome)

        empty_mismatch = RULES.evaluate_rule(
            self.equals_rule(),
            context(structured_view={"supplier_quote": {"gtin": ""}}),
        )
        self.assertEqual(RULES.OUTCOME_FAIL, empty_mismatch.outcome)
        self.assertTrue(empty_mismatch.reasons[0].startswith(RULES.REASON_VALUE_MISMATCH))

    def test_invalid_runtime_pointer_escaping_fails_instead_of_reviewing(self):
        leaf = RULES.Primitive(
            name="equals",
            field=RULES.FieldRef("record", "/supplier_quote/bad~2token"),
            operand=RULES.Operand(literal="x"),
            when_absent=RULES.WHEN_ABSENT_MANUAL_REVIEW,
        )
        rule = RULES.Rule(
            policy_id="pack:market-data/direct-construction",
            composition=RULES.Primitive(name="all_of", children=(leaf,)),
        )

        evaluation = RULES.evaluate_rule(
            rule, context(structured_view={"supplier_quote": {}})
        )
        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome)
        self.assertIn("RFC 6901", evaluation.reasons[0])

    def test_rule_evaluation_has_one_validated_outcome_and_conservative_properties(self):
        passed = RULES.RuleEvaluation(RULES.OUTCOME_PASS, ["ok"])
        failed = RULES.RuleEvaluation(RULES.OUTCOME_FAIL, ["no"])
        review = RULES.RuleEvaluation(RULES.OUTCOME_MANUAL_REVIEW, ["review"])

        self.assertEqual((True, False), (passed.passed, passed.requires_manual_review))
        self.assertEqual((False, False), (failed.passed, failed.requires_manual_review))
        self.assertEqual((False, True), (review.passed, review.requires_manual_review))
        with self.assertRaises(ValueError):
            RULES.RuleEvaluation("unknown", [])


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

    def test_all_of_review_retains_review_and_satisfied_reasons_in_declaration_order(self):
        rule = parse_one(
            {"equals": {"field": "record/sku", "value": "A"}},
            {
                "equals": {
                    "field": "record/gtin",
                    "value": "123",
                    "when_absent": "manual_review",
                }
            },
            {"regex": {"field": "record/sku", "pattern": "A"}},
        )

        evaluation = RULES.evaluate_rule(rule, context(structured_view={"sku": "A"}))

        self.assertEqual(RULES.OUTCOME_MANUAL_REVIEW, evaluation.outcome)
        self.assertEqual(3, len(evaluation.reasons))
        self.assertFalse(evaluation.reasons[0].startswith(RULES.RULE_REASON_PREFIXES))
        self.assertTrue(evaluation.reasons[1].startswith(RULES.REASON_FIELD_ABSENT))
        self.assertFalse(evaluation.reasons[2].startswith(RULES.RULE_REASON_PREFIXES))

    def test_all_of_hard_failure_dominates_and_suppresses_review_and_pass_reasons(self):
        rule = parse_one(
            {"equals": {"field": "record/sku", "value": "A"}},
            {
                "equals": {
                    "field": "record/gtin",
                    "value": "123",
                    "when_absent": "manual_review",
                }
            },
            {"regex": {"field": "record/sku", "pattern": "Z+"}},
        )

        evaluation = RULES.evaluate_rule(rule, context(structured_view={"sku": "A"}))

        self.assertEqual(RULES.OUTCOME_FAIL, evaluation.outcome)
        self.assertEqual(1, len(evaluation.reasons))
        self.assertTrue(evaluation.reasons[0].startswith(RULES.REASON_REGEX_MISMATCH))

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

    def test_any_of_does_not_stop_on_review_when_a_later_branch_passes(self):
        declaration = pack(
            {
                "pack:market-data/quote-48h": {
                    "any_of": [
                        {
                            "equals": {
                                "field": "record/gtin",
                                "value": "123",
                                "when_absent": "manual_review",
                            }
                        },
                        {"equals": {"field": "record/sku", "value": "A"}},
                    ]
                }
            }
        )
        rule = RULES.pack_policy_rules({"domain_pack": declaration})[
            "pack:market-data/quote-48h"
        ]

        evaluation = RULES.evaluate_rule(rule, context(structured_view={"sku": "A"}))

        self.assertEqual(RULES.OUTCOME_PASS, evaluation.outcome)
        self.assertEqual(1, len(evaluation.reasons))
        self.assertFalse(evaluation.reasons[0].startswith(RULES.REASON_FIELD_ABSENT))
        self.assertIn("record/sku", evaluation.reasons[0])

    def test_any_of_review_suppresses_failed_alternative_reasons(self):
        declaration = pack(
            {
                "pack:market-data/quote-48h": {
                    "any_of": [
                        {"equals": {"field": "record/sku", "value": "NEVER"}},
                        {
                            "equals": {
                                "field": "record/gtin",
                                "value": "123",
                                "when_absent": "manual_review",
                            }
                        },
                    ]
                }
            }
        )
        rule = RULES.pack_policy_rules({"domain_pack": declaration})[
            "pack:market-data/quote-48h"
        ]

        evaluation = RULES.evaluate_rule(rule, context(structured_view={"sku": "A"}))

        self.assertEqual(RULES.OUTCOME_MANUAL_REVIEW, evaluation.outcome)
        self.assertEqual(1, len(evaluation.reasons))
        self.assertTrue(evaluation.reasons[0].startswith(RULES.REASON_FIELD_ABSENT))

    def test_any_of_retains_every_review_branch_when_none_passes(self):
        rule = parse_one(
            {
                "any_of": [
                    {
                        "equals": {
                            "field": "record/gtin",
                            "value": "123",
                            "when_absent": "manual_review",
                        }
                    },
                    {
                        "equals": {
                            "field": "record/upc",
                            "value": "456",
                            "when_absent": "manual_review",
                        }
                    },
                ]
            }
        )

        evaluation = RULES.evaluate_rule(rule, context(structured_view={}))

        self.assertEqual(RULES.OUTCOME_MANUAL_REVIEW, evaluation.outcome)
        self.assertEqual(
            [
                RULES.REASON_FIELD_ABSENT,
                RULES.REASON_FIELD_ABSENT,
            ],
            [reason.split(":", 1)[0] for reason in evaluation.reasons],
        )
        self.assertIn("record/gtin", evaluation.reasons[0])
        self.assertIn("record/upc", evaluation.reasons[1])

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

    def test_manual_review_propagates_at_the_maximum_composition_depth(self):
        rule = parse_one(
            {"equals": {"field": "record/sku", "value": "A"}},
            {
                "any_of": [
                    {
                        "all_of": [
                            {
                                "equals": {
                                    "field": "record/gtin",
                                    "value": "123",
                                    "when_absent": "manual_review",
                                }
                            },
                            {"regex": {"field": "record/sku", "pattern": "A"}},
                        ]
                    },
                    {"equals": {"field": "record/sku", "value": "NEVER"}},
                ]
            },
        )

        evaluation = RULES.evaluate_rule(rule, context(structured_view={"sku": "A"}))

        self.assertEqual(RULES.OUTCOME_MANUAL_REVIEW, evaluation.outcome)
        self.assertEqual(3, len(evaluation.reasons))
        self.assertIn("record/sku", evaluation.reasons[0])
        self.assertTrue(evaluation.reasons[1].startswith(RULES.REASON_FIELD_ABSENT))
        self.assertIn("record/sku", evaluation.reasons[2])

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
