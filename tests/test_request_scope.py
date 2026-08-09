"""Unit tests for structured request-scope parsing and matching (CR-4 foundation)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

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


SCOPE = load_script_module("research_request_scope", "_request_scope.py")


class ParseScopePairsTests(unittest.TestCase):
    def test_absent_option_yields_no_scope(self):
        self.assertEqual({}, SCOPE.parse_scope_pairs(None))
        self.assertEqual({}, SCOPE.parse_scope_pairs([]))

    def test_pairs_are_parsed_and_stripped(self):
        self.assertEqual(
            {"facet_id": "supplier_quote", "candidate": "acme-widget"},
            SCOPE.parse_scope_pairs([" facet_id = supplier_quote ", "candidate=acme-widget"]),
        )

    def test_value_may_contain_equals_signs(self):
        self.assertEqual({"query": "a=b"}, SCOPE.parse_scope_pairs(["query=a=b"]))

    def test_missing_equals_is_refused(self):
        with self.assertRaises(SCOPE.RequestScopeError) as ctx:
            SCOPE.parse_scope_pairs(["facet_id"])
        self.assertEqual("REQUEST_SCOPE_INVALID", ctx.exception.error_code)

    def test_empty_value_is_refused(self):
        with self.assertRaises(SCOPE.RequestScopeError) as ctx:
            SCOPE.parse_scope_pairs(["facet_id="])
        self.assertEqual("REQUEST_SCOPE_INVALID", ctx.exception.error_code)

    def test_malformed_keys_are_refused(self):
        for bad in ("Facet=x", "=x", "fa cet=x", "-facet=x"):
            with self.subTest(pair=bad):
                with self.assertRaises(SCOPE.RequestScopeError) as ctx:
                    SCOPE.parse_scope_pairs([bad])
                self.assertEqual("REQUEST_SCOPE_INVALID", ctx.exception.error_code)

    def test_duplicate_keys_are_refused(self):
        with self.assertRaises(SCOPE.RequestScopeError) as ctx:
            SCOPE.parse_scope_pairs(["facet_id=a", "facet_id=b"])
        self.assertIn("more than once", str(ctx.exception))

    def test_option_name_appears_in_the_message(self):
        with self.assertRaises(SCOPE.RequestScopeError) as ctx:
            SCOPE.parse_scope_pairs(["nope"], option="--match-scope")
        self.assertIn("--match-scope", str(ctx.exception))


class NormalizeScopeTests(unittest.TestCase):
    def test_non_mappings_normalize_to_empty(self):
        for value in (None, "facet_id=x", ["facet_id"], 3):
            with self.subTest(value=value):
                self.assertEqual({}, SCOPE.normalize_scope(value))

    def test_scalars_are_coerced_to_strings(self):
        self.assertEqual({"count": "3", "ratio": "1.5"}, SCOPE.normalize_scope({"count": 3, "ratio": 1.5}))

    def test_nonconforming_entries_are_dropped_not_fatal(self):
        """Sidecars are written by another party; one bad key must not cost the whole record."""
        normalized = SCOPE.normalize_scope(
            {"facet_id": "supplier_quote", "Bad Key": "x", "empty": "  ", "nested": {"a": 1}, "flag": True}
        )
        self.assertEqual({"facet_id": "supplier_quote"}, normalized)


class ScopeMatchTests(unittest.TestCase):
    def test_agreeing_scopes_have_no_conflicts_or_absences(self):
        conflicts, absences = SCOPE.scope_match({"facet_id": "a"}, {"facet_id": "a", "extra": "b"})
        self.assertEqual([], conflicts)
        self.assertEqual([], absences)

    def test_disagreeing_shared_key_is_a_conflict(self):
        conflicts, absences = SCOPE.scope_match({"facet_id": "a"}, {"facet_id": "b"})
        self.assertEqual(["facet_id"], conflicts)
        self.assertEqual([], absences)

    def test_request_key_absent_from_source_is_an_absence_not_a_conflict(self):
        conflicts, absences = SCOPE.scope_match({"facet_id": "a", "candidate": "c"}, {"facet_id": "a"})
        self.assertEqual([], conflicts)
        self.assertEqual(["candidate"], absences)

    def test_source_only_keys_are_ignored_entirely(self):
        conflicts, absences = SCOPE.scope_match({}, {"facet_id": "a"})
        self.assertEqual([], conflicts)
        self.assertEqual([], absences)

    def test_unscoped_pairs_never_contradict(self):
        self.assertEqual(([], []), SCOPE.scope_match({}, {}))
        self.assertEqual(([], []), SCOPE.scope_match(None, None))

    def test_results_are_sorted_for_deterministic_messages(self):
        conflicts, _ = SCOPE.scope_match({"z": "1", "a": "1"}, {"z": "2", "a": "2"})
        self.assertEqual(["a", "z"], conflicts)

    def test_values_are_compared_after_stripping(self):
        self.assertEqual(([], []), SCOPE.scope_match({"facet_id": " a "}, {"facet_id": "a"}))


class ScopeEqualTests(unittest.TestCase):
    def test_absent_scopes_are_equal(self):
        self.assertTrue(SCOPE.scope_equal(None, {}))

    def test_same_pairs_are_equal_regardless_of_insertion_order(self):
        self.assertTrue(SCOPE.scope_equal({"a": "1", "b": "2"}, {"b": "2", "a": "1"}))

    def test_different_pairs_are_not_equal(self):
        self.assertFalse(SCOPE.scope_equal({"a": "1"}, {"a": "2"}))
        self.assertFalse(SCOPE.scope_equal({"a": "1"}, {"a": "1", "b": "2"}))


class RenderingTests(unittest.TestCase):
    def test_conflict_details_name_both_values(self):
        details = SCOPE.conflict_details({"facet_id": "a"}, {"facet_id": "b"}, ["facet_id"])
        self.assertEqual([{"key": "facet_id", "request_value": "a", "source_value": "b"}], details)

    def test_format_scope_is_sorted_and_compact(self):
        self.assertEqual("candidate=c, facet_id=a", SCOPE.format_scope({"facet_id": "a", "candidate": "c"}))

    def test_format_scope_of_nothing_is_empty(self):
        self.assertEqual("", SCOPE.format_scope(None))


if __name__ == "__main__":
    unittest.main()
