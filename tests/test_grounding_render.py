"""Canonical frontmatter serialization for `grounding` entries (CR-7 T6).

The byte layout asserted in `CanonicalGroundingBytesTests` is normative: the supported
write path is specified against it, and a hand edit is "compliant" exactly when it
matches these bytes. Everything else in this file guards the two properties that make
the layout worth pinning — it reloads through `yaml.safe_load` as the same data, and it
never silently retypes a value.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

import yaml

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


RESOLVE = load_script_module("cr7_grounding_render_resolve", "question_resolve.py")
CLAIM = load_script_module("cr7_grounding_render_claim", "question_claim.py")

QUOTE_ENTRY = {
    "claim": "The product spec is vendor-controlled.",
    "source_id": "web:vendor-official-product-spec",
    "quote": "Vendor-controlled product specification.",
    "location_hint": "Official product spec",
}
ANCHOR_ENTRY = {
    "claim": "Current supplier price is 23.99 EUR",
    "source_id": "data--keepa--b0abc123",
    "anchor": {"pointer": "supplier_quote/price", "expected": "23.99 EUR"},
}


def render(entries: list[dict]) -> str:
    return "\n".join(RESOLVE.render_grounding_sequence("grounding", entries))


def reload_entries(block: str) -> list[dict]:
    return yaml.safe_load(block)["grounding"]


class CanonicalGroundingBytesTests(unittest.TestCase):
    """The normative example. Changing these bytes changes the published contract."""

    CANONICAL = (
        "grounding:\n"
        '  - claim: "Current supplier price is 23.99 EUR"\n'
        "    source_id: data--keepa--b0abc123\n"
        "    anchor:\n"
        '      pointer: "supplier_quote/price"\n'
        '      expected: "23.99 EUR"\n'
        '  - claim: "The product spec is vendor-controlled."\n'
        '    source_id: "web:vendor-official-product-spec"\n'
        '    quote: "Vendor-controlled product specification."\n'
        '    location_hint: "Official product spec"'
    )

    def test_mixed_anchor_and_quote_block_is_byte_exact(self):
        self.assertEqual(self.CANONICAL, render([ANCHOR_ENTRY, QUOTE_ENTRY]))

    def test_canonical_bytes_reload_as_the_input_entries(self):
        self.assertEqual([ANCHOR_ENTRY, QUOTE_ENTRY], reload_entries(self.CANONICAL))

    def test_indentation_is_two_four_six(self):
        lines = self.CANONICAL.split("\n")
        self.assertEqual("grounding:", lines[0])
        self.assertTrue(lines[1].startswith("  - claim: "), lines[1])
        self.assertTrue(lines[2].startswith("    source_id: "), lines[2])
        self.assertEqual("    anchor:", lines[3])
        self.assertTrue(lines[4].startswith("      pointer: "), lines[4])
        self.assertTrue(lines[5].startswith("      expected: "), lines[5])

    def test_key_order_is_canonical_regardless_of_input_order(self):
        scrambled = {
            "anchor": {"expected": "23.99 EUR", "pointer": "supplier_quote/price"},
            "source_id": "data--keepa--b0abc123",
            "claim": "Current supplier price is 23.99 EUR",
        }
        self.assertEqual(render([ANCHOR_ENTRY]), render([scrambled]))


class GroundingRoundTripTests(unittest.TestCase):
    def test_quote_form_round_trips(self):
        entries = [QUOTE_ENTRY]
        self.assertEqual(entries, reload_entries(render(entries)))

    def test_quote_form_without_location_hint_round_trips(self):
        entries = [{k: v for k, v in QUOTE_ENTRY.items() if k != "location_hint"}]
        self.assertEqual(entries, reload_entries(render(entries)))

    def test_anchor_form_round_trips(self):
        entries = [ANCHOR_ENTRY]
        self.assertEqual(entries, reload_entries(render(entries)))

    def test_mixed_list_round_trips(self):
        entries = [ANCHOR_ENTRY, QUOTE_ENTRY, ANCHOR_ENTRY]
        self.assertEqual(entries, reload_entries(render(entries)))

    def test_none_valued_optional_fields_count_as_absent(self):
        entries = [{**QUOTE_ENTRY, "location_hint": None, "anchor": None}]
        self.assertEqual(
            [{k: v for k, v in QUOTE_ENTRY.items() if k != "location_hint"}],
            reload_entries(render(entries)),
        )

    def test_empty_grounding_list_renders_an_explicit_empty_sequence(self):
        block = "\n".join(RESOLVE.render_frontmatter_value("grounding", [], False))
        self.assertEqual("grounding: []", block)
        self.assertEqual([], yaml.safe_load(block)["grounding"])


class GroundingScalarTypingTests(unittest.TestCase):
    """`expected` is canonically a string; an unquoted `23.99` would change its meaning."""

    YAML_TYPED_LITERALS = ("23.99", "true", "false", "null", "007", "0x1f", "2026-08-09", "-", "+5", ".inf", "no")

    def test_expected_literals_that_yaml_would_retype_stay_strings(self):
        for literal in self.YAML_TYPED_LITERALS:
            with self.subTest(expected=literal):
                entries = [{"claim": "c", "source_id": "s", "anchor": {"pointer": "p", "expected": literal}}]
                loaded = reload_entries(render(entries))[0]["anchor"]["expected"]
                self.assertIsInstance(loaded, str)
                self.assertEqual(literal, loaded)

    def test_expected_is_always_emitted_quoted(self):
        entries = [{"claim": "c", "source_id": "s", "anchor": {"pointer": "p", "expected": "plainword"}}]
        self.assertIn('      expected: "plainword"', render(entries))

    def test_claim_literals_that_yaml_would_retype_stay_strings(self):
        for literal in self.YAML_TYPED_LITERALS:
            with self.subTest(claim=literal):
                entries = [{"claim": literal, "source_id": "s", "quote": literal, "location_hint": literal}]
                self.assertEqual(entries, reload_entries(render(entries)))


class GroundingEscapingTests(unittest.TestCase):
    HOSTILE_VALUES = (
        "colon: and space",
        'double "quotes" inside',
        "single 'quotes' inside",
        "unicode café — em dash — 日本語",
        "line one\nline two",
        "tab\tseparated",
        "  leading and trailing spaces  ",
        "",
        "- looks like a sequence item",
        "# looks like a comment",
        "{braces} [brackets] & *anchors",
        "trailing backslash \\",
        "%directive",
    )

    def test_hostile_values_round_trip_in_every_field(self):
        for value in self.HOSTILE_VALUES:
            with self.subTest(value=value):
                entries = [
                    {"claim": value, "source_id": "web:x", "quote": value, "location_hint": value},
                    {"claim": value, "source_id": "web:y", "anchor": {"pointer": value, "expected": value}},
                ]
                self.assertEqual(entries, reload_entries(render(entries)))

    def test_leading_and_trailing_whitespace_survives(self):
        entries = [{"claim": "  padded  ", "source_id": "s", "quote": "\tboth\t"}]
        loaded = reload_entries(render(entries))[0]
        self.assertEqual("  padded  ", loaded["claim"])
        self.assertEqual("\tboth\t", loaded["quote"])

    def test_source_id_with_a_colon_is_quoted(self):
        entries = [{**QUOTE_ENTRY, "source_id": "web:has:colons"}]
        self.assertIn('    source_id: "web:has:colons"', render(entries))
        self.assertEqual(entries, reload_entries(render(entries)))

    def test_identifier_shaped_source_id_stays_bare(self):
        entries = [{**QUOTE_ENTRY, "source_id": "data--keepa--b0abc123"}]
        self.assertIn("    source_id: data--keepa--b0abc123", render(entries))

    def test_whitespace_padded_source_id_is_quoted_rather_than_silently_stripped(self):
        entries = [{**QUOTE_ENTRY, "source_id": " padded-id "}]
        self.assertIn('    source_id: " padded-id "', render(entries))
        self.assertEqual(entries, reload_entries(render(entries)))


class GroundingShapeRefusalTests(unittest.TestCase):
    """The renderer refuses rather than emitting bytes that reload as a different shape."""

    def test_entry_with_both_forms_refuses(self):
        entries = [{**QUOTE_ENTRY, "anchor": {"pointer": "p", "expected": "e"}}]
        with self.assertRaises(ValueError) as caught:
            render(entries)
        self.assertIn("carries both forms", str(caught.exception))
        self.assertIn("exactly one of quote or anchor", str(caught.exception))

    def test_entry_with_neither_form_refuses(self):
        with self.assertRaises(ValueError) as caught:
            render([{"claim": "c", "source_id": "s"}])
        self.assertIn("carries no form", str(caught.exception))
        self.assertIn("exactly one of quote or anchor", str(caught.exception))

    def test_location_hint_beside_anchor_refuses(self):
        entries = [{**ANCHOR_ENTRY, "location_hint": "Official product spec"}]
        with self.assertRaises(ValueError) as caught:
            render(entries)
        self.assertIn("location_hint beside anchor", str(caught.exception))

    def test_missing_head_field_refuses(self):
        with self.assertRaises(ValueError) as caught:
            render([{k: v for k, v in QUOTE_ENTRY.items() if k != "source_id"}])
        self.assertIn("missing required source_id", str(caught.exception))

    def test_unknown_entry_key_refuses(self):
        with self.assertRaises(ValueError) as caught:
            render([{**QUOTE_ENTRY, "policy": "retained_quote_evidence"}])
        self.assertIn("unsupported key(s): policy", str(caught.exception))

    def test_unknown_anchor_key_refuses(self):
        entries = [{**ANCHOR_ENTRY, "anchor": {"pointer": "p", "expected": "e", "offset": "3"}}]
        with self.assertRaises(ValueError) as caught:
            render(entries)
        self.assertIn("anchor has unsupported key(s): offset", str(caught.exception))

    def test_incomplete_anchor_refuses(self):
        with self.assertRaises(ValueError) as caught:
            render([{"claim": "c", "source_id": "s", "anchor": {"pointer": "p"}}])
        self.assertIn("anchor is missing required expected", str(caught.exception))

    def test_non_mapping_anchor_refuses(self):
        with self.assertRaises(ValueError) as caught:
            render([{"claim": "c", "source_id": "s", "anchor": "supplier_quote/price"}])
        self.assertIn("anchor must be a mapping", str(caught.exception))

    def test_non_mapping_entry_refuses(self):
        with self.assertRaises(ValueError) as caught:
            render(["claim: c"])
        self.assertIn("grounding[0] must be a mapping", str(caught.exception))

    def test_non_string_scalar_refuses_instead_of_emitting_a_python_repr(self):
        # The failure this guard exists for: `render_mapping_sequence` would have written
        # `str({'pointer': ...})` into a YAML string and reloaded silently as the wrong type.
        with self.assertRaises(ValueError) as caught:
            render([{"claim": {"pointer": "a/b"}, "source_id": "s", "quote": "q"}])
        self.assertIn("must be a string", str(caught.exception))

    def test_numeric_expected_refuses_rather_than_guessing_a_canonical_form(self):
        with self.assertRaises(ValueError) as caught:
            render([{"claim": "c", "source_id": "s", "anchor": {"pointer": "p", "expected": 23.99}}])
        self.assertIn("anchor.expected must be a string", str(caught.exception))


class RendererRoutingTests(unittest.TestCase):
    def test_grounding_routes_to_the_nested_renderer(self):
        self.assertEqual(
            RESOLVE.render_grounding_sequence("grounding", [ANCHOR_ENTRY]),
            RESOLVE.render_frontmatter_value("grounding", [ANCHOR_ENTRY], False),
        )

    def test_grounding_does_not_route_to_the_flat_mapping_renderer(self):
        flat = RESOLVE.render_mapping_sequence("grounding", [ANCHOR_ENTRY])
        self.assertIn("{'pointer'", "\n".join(flat), "precondition: the flat renderer still stringifies mappings")
        self.assertNotEqual(flat, RESOLVE.render_frontmatter_value("grounding", [ANCHOR_ENTRY], False))


class HumanReviewsRegressionTests(unittest.TestCase):
    """`human_reviews` is live on `render_mapping_sequence`; CR-7 must not move it."""

    ENTRIES = [
        {
            "policy": "legal-signoff",
            "verdict": "accepted",
            "reviewed_by": "counsel",
            "review_ref": "TICKET-14",
            "note": "Reviewed: matches the 2026 policy.",
            "reviewed_at": "2026-08-09T10:11:12Z",
        },
        {"policy": "safety", "verdict": "rejected", "reviewed_by": "reviewer-b", "reviewed_at": "2026-08-09T11:00:00Z"},
    ]
    EXPECTED = (
        "human_reviews:\n"
        "  - policy: legal-signoff\n"
        "    verdict: accepted\n"
        "    reviewed_by: counsel\n"
        "    review_ref: TICKET-14\n"
        '    note: "Reviewed: matches the 2026 policy."\n'
        '    reviewed_at: "2026-08-09T10:11:12Z"\n'
        "  - policy: safety\n"
        "    verdict: rejected\n"
        "    reviewed_by: reviewer-b\n"
        '    reviewed_at: "2026-08-09T11:00:00Z"'
    )

    def test_human_reviews_bytes_are_unchanged(self):
        self.assertEqual(self.EXPECTED, "\n".join(RESOLVE.render_mapping_sequence("human_reviews", self.ENTRIES)))

    def test_human_reviews_still_routes_through_render_frontmatter_value(self):
        self.assertEqual(
            self.EXPECTED,
            "\n".join(RESOLVE.render_frontmatter_value("human_reviews", self.ENTRIES, False)),
        )

    def test_human_reviews_round_trips(self):
        self.assertEqual(self.ENTRIES, yaml.safe_load(self.EXPECTED)["human_reviews"])


class GroundingBlockRemovalTests(unittest.TestCase):
    """`remove_frontmatter_field_block` must eat a nested block whole, and nothing else."""

    FRONTMATTER = [
        "type: question",
        "slug: supplier-price",
        "status: answered",
        "grounding_required: true",
        "grounding:",
        '  - claim: "Current supplier price is 23.99 EUR"',
        "    source_id: data--keepa--b0abc123",
        "    anchor:",
        '      pointer: "supplier_quote/price"',
        '      expected: "23.99 EUR"',
        "grounding_verified_at: \"2026-08-09T10:11:12Z\"",
        "updated: \"2026-08-09\"",
    ]

    def test_nested_grounding_block_is_consumed_at_every_depth(self):
        remaining = RESOLVE.remove_frontmatter_field_block(list(self.FRONTMATTER), "grounding")
        self.assertEqual(
            [
                "type: question",
                "slug: supplier-price",
                "status: answered",
                "grounding_required: true",
                'grounding_verified_at: "2026-08-09T10:11:12Z"',
                'updated: "2026-08-09"',
            ],
            remaining,
        )

    def test_removing_grounding_leaves_the_adjacent_grounding_required_key(self):
        remaining = RESOLVE.remove_frontmatter_field_block(list(self.FRONTMATTER), "grounding")
        loaded = yaml.safe_load("\n".join(remaining))
        self.assertNotIn("grounding", loaded)
        self.assertIs(True, loaded["grounding_required"])
        self.assertEqual("2026-08-09T10:11:12Z", loaded["grounding_verified_at"])

    def test_removing_grounding_required_leaves_the_grounding_block(self):
        remaining = RESOLVE.remove_frontmatter_field_block(list(self.FRONTMATTER), "grounding_required")
        loaded = yaml.safe_load("\n".join(remaining))
        self.assertNotIn("grounding_required", loaded)
        self.assertEqual(1, len(loaded["grounding"]))
        self.assertEqual("supplier_quote/price", loaded["grounding"][0]["anchor"]["pointer"])

    def test_zero_indented_hand_edited_entries_are_also_consumed(self):
        # Hosts have been hand-editing grounding; YAML also allows the sequence items at
        # column 0. Removal must eat those too, or a rewrite would leave a duplicate key.
        hand_edited = [
            "type: question",
            "grounding_required: true",
            "grounding:",
            "- claim: The product spec is vendor-controlled.",
            "  source_id: web:vendor-official-product-spec",
            "  quote: Vendor-controlled product specification.",
            'updated: "2026-08-09"',
        ]
        remaining = RESOLVE.remove_frontmatter_field_block(hand_edited, "grounding")
        self.assertEqual(["type: question", "grounding_required: true", 'updated: "2026-08-09"'], remaining)

    def test_replacing_grounding_leaves_exactly_one_block(self):
        replaced = RESOLVE.set_frontmatter_field_block(list(self.FRONTMATTER), "grounding", [QUOTE_ENTRY])
        loaded = yaml.safe_load("\n".join(replaced))
        self.assertEqual([QUOTE_ENTRY], loaded["grounding"])
        self.assertEqual(1, sum(1 for line in replaced if line.startswith("grounding:")))
        self.assertIs(True, loaded["grounding_required"])


class ApplyResolutionEditsTests(unittest.TestCase):
    """The renderer through the real frontmatter write helper, on a realistic page."""

    PAGE = "\n".join(
        [
            "---",
            "type: question",
            "slug: supplier-price",
            "status: answered",
            "grounding_required: true",
            "human_reviews:",
            "  - policy: legal-signoff",
            "    verdict: accepted",
            "    reviewed_by: counsel",
            '    reviewed_at: "2026-08-09T10:11:12Z"',
            "source_ids:",
            "  - data--keepa--b0abc123",
            "  - web:vendor-official-product-spec",
            "---",
            "",
            "# Supplier price",
            "",
            "Body text stays put.",
            "",
        ]
    )

    def frontmatter_of(self, page: str) -> dict:
        parts = CLAIM.split_frontmatter_lines(page)
        self.assertIsNotNone(parts)
        return CLAIM.frontmatter_mapping(parts[0])

    def test_grounding_block_is_written_and_reloads(self):
        updated = RESOLVE.apply_resolution_edits(self.PAGE, {"grounding": [ANCHOR_ENTRY, QUOTE_ENTRY]}, ())
        frontmatter = self.frontmatter_of(updated)
        self.assertEqual([ANCHOR_ENTRY, QUOTE_ENTRY], frontmatter["grounding"])
        self.assertIs(True, frontmatter["grounding_required"])
        self.assertEqual("data--keepa--b0abc123", frontmatter["source_ids"][0])
        self.assertIn("Body text stays put.", updated)

    def test_human_reviews_survive_a_grounding_write(self):
        updated = RESOLVE.apply_resolution_edits(self.PAGE, {"grounding": [QUOTE_ENTRY]}, ())
        frontmatter = self.frontmatter_of(updated)
        self.assertEqual("legal-signoff", frontmatter["human_reviews"][0]["policy"])
        self.assertEqual("accepted", frontmatter["human_reviews"][0]["verdict"])

    def test_rewriting_grounding_is_idempotent(self):
        once = RESOLVE.apply_resolution_edits(self.PAGE, {"grounding": [ANCHOR_ENTRY]}, ())
        twice = RESOLVE.apply_resolution_edits(once, {"grounding": [ANCHOR_ENTRY]}, ())
        self.assertEqual(once, twice)

    def test_grounding_can_be_removed_without_touching_grounding_required(self):
        written = RESOLVE.apply_resolution_edits(self.PAGE, {"grounding": [ANCHOR_ENTRY]}, ())
        cleared = RESOLVE.apply_resolution_edits(written, {}, ("grounding",))
        frontmatter = self.frontmatter_of(cleared)
        self.assertNotIn("grounding", frontmatter)
        self.assertIs(True, frontmatter["grounding_required"])


if __name__ == "__main__":
    unittest.main()
