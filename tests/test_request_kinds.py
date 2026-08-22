"""Unit tests for the shared source-request kind registry (CR-4 foundation)."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


KINDS = load_script_module("research_request_kinds", "_request_kinds.py")


def config_with(declarations, *, pack_name: str = "market-data") -> dict:
    domain_pack: dict = {"name": pack_name}
    if declarations is not None:
        domain_pack["request_kinds"] = declarations
    return {"domain_pack": domain_pack}


def declaration(kind_id: str = "pack:market-data/supplier_quote") -> dict:
    return {
        "id": kind_id,
        "label": "Supplier quote",
        "description": "Live SKU price from a named supplier.",
    }


class BuiltinKindTests(unittest.TestCase):
    def test_builtins_are_accepted_without_any_pack(self):
        for kind in KINDS.builtin_kinds():
            self.assertEqual(kind, KINDS.validate_kind(kind, {}))

    def test_structured_data_is_a_builtin(self):
        self.assertIn("structured_data", KINDS.builtin_kinds())
        self.assertEqual("structured_data", KINDS.validate_kind("structured_data", {}))

    def test_builtins_stay_valid_when_a_pack_declares_kinds(self):
        config = config_with([declaration()])
        self.assertEqual("paper", KINDS.validate_kind("paper", config))

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual("paper", KINDS.validate_kind("  paper  ", {}))

    def test_empty_kind_is_invalid(self):
        with self.assertRaises(KINDS.RequestKindError) as ctx:
            KINDS.validate_kind("   ", {})
        self.assertEqual("REQUEST_KIND_INVALID", ctx.exception.error_code)


class PackKindValidationTests(unittest.TestCase):
    def test_declared_pack_kind_is_accepted(self):
        config = config_with([declaration()])
        self.assertEqual(
            "pack:market-data/supplier_quote",
            KINDS.validate_kind("pack:market-data/supplier_quote", config),
        )

    def test_undeclared_namespaced_kind_is_refused_as_undeclared(self):
        with self.assertRaises(KINDS.RequestKindError) as ctx:
            KINDS.validate_kind("pack:market-data/price_history", config_with([declaration()]))
        self.assertEqual("REQUEST_KIND_UNDECLARED", ctx.exception.error_code)

    def test_pack_kind_is_refused_when_no_pack_declares_it(self):
        with self.assertRaises(KINDS.RequestKindError) as ctx:
            KINDS.validate_kind("pack:market-data/supplier_quote", {})
        self.assertEqual("REQUEST_KIND_UNDECLARED", ctx.exception.error_code)

    def test_bare_id_is_invalid_and_names_the_prefixed_id_it_meant(self):
        """The change request's own examples omit the prefix, so the near-miss must self-heal."""
        config = config_with([declaration()])
        with self.assertRaises(KINDS.RequestKindError) as ctx:
            KINDS.validate_kind("market-data/supplier_quote", config)
        self.assertEqual("REQUEST_KIND_INVALID", ctx.exception.error_code)
        self.assertIn("pack:market-data/supplier_quote", str(ctx.exception))

    def test_bare_id_for_an_undeclared_kind_still_explains_the_namespace_rule(self):
        with self.assertRaises(KINDS.RequestKindError) as ctx:
            KINDS.validate_kind("market-data/unknown_thing", config_with([declaration()]))
        self.assertEqual("REQUEST_KIND_INVALID", ctx.exception.error_code)
        self.assertIn("pack:market-data/unknown_thing", str(ctx.exception))

    def test_malformed_ids_are_invalid(self):
        config = config_with([declaration()])
        for bad in ("pack:Market-Data/supplier_quote", "pack:/supplier_quote", "pack:market-data/", "pack:market-data", "PAPER"):
            with self.subTest(kind=bad):
                with self.assertRaises(KINDS.RequestKindError) as ctx:
                    KINDS.validate_kind(bad, config)
                self.assertEqual("REQUEST_KIND_INVALID", ctx.exception.error_code)

    def test_error_details_list_the_valid_kinds(self):
        with self.assertRaises(KINDS.RequestKindError) as ctx:
            KINDS.validate_kind("pack:market-data/nope", config_with([declaration()]))
        self.assertIn("pack:market-data/supplier_quote", ctx.exception.details["valid_kinds"])


class DeclarationErrorTests(unittest.TestCase):
    def test_absent_declaration_is_valid_and_empty(self):
        self.assertEqual([], KINDS.declaration_errors({"name": "market-data"}))
        self.assertEqual({}, KINDS.declared_pack_kinds(config_with(None)))

    def test_valid_declaration_parses_label_and_description(self):
        declared = KINDS.declared_pack_kinds(config_with([declaration()]))
        self.assertEqual(
            {"label": "Supplier quote", "description": "Live SKU price from a named supplier."},
            declared["pack:market-data/supplier_quote"],
        )

    def test_declaration_must_be_a_list(self):
        errors = KINDS.declaration_errors({"name": "market-data", "request_kinds": {"id": "x"}})
        self.assertEqual(1, len(errors))
        self.assertIn("must be a list", errors[0])

    def test_missing_label_or_description_is_an_error(self):
        for missing in ("label", "description", "id"):
            with self.subTest(field=missing):
                entry = declaration()
                del entry[missing]
                errors = KINDS.declaration_errors({"name": "market-data", "request_kinds": [entry]})
                self.assertTrue(any(missing in error for error in errors), errors)

    def test_namespace_must_match_the_pack_name(self):
        errors = KINDS.declaration_errors(
            {"name": "market-data", "request_kinds": [declaration("pack:other-pack/supplier_quote")]}
        )
        self.assertEqual(1, len(errors))
        self.assertIn("other-pack", errors[0])

    def test_duplicate_ids_are_an_error(self):
        errors = KINDS.declaration_errors(
            {"name": "market-data", "request_kinds": [declaration(), declaration()]}
        )
        self.assertEqual(1, len(errors))
        self.assertIn("more than once", errors[0])

    def test_builtin_cannot_be_redeclared(self):
        errors = KINDS.declaration_errors({"name": "market-data", "request_kinds": [declaration("paper")]})
        self.assertEqual(1, len(errors))
        self.assertIn("reserved", errors[0])

    def test_unprefixed_id_is_an_error_naming_the_prefixed_form(self):
        errors = KINDS.declaration_errors(
            {"name": "market-data", "request_kinds": [declaration("market-data/supplier_quote")]}
        )
        self.assertEqual(1, len(errors))
        self.assertIn("pack:market-data/supplier_quote", errors[0])

    def test_unknown_declaration_keys_are_reported(self):
        entry = declaration()
        entry["provider"] = "keepa"
        errors = KINDS.declaration_errors({"name": "market-data", "request_kinds": [entry]})
        self.assertTrue(any("provider" in error for error in errors), errors)

    def test_malformed_declaration_raises_rather_than_narrowing_the_valid_set(self):
        """A broken pack must fail the command, not silently refuse its own kinds."""
        config = {"domain_pack": {"name": "market-data", "request_kinds": "nope"}}
        with self.assertRaises(KINDS.RequestKindError) as ctx:
            KINDS.declared_pack_kinds(config)
        self.assertEqual("CONFIG_INVALID", ctx.exception.error_code)


class ValidKindsTests(unittest.TestCase):
    def test_valid_kinds_are_builtins_plus_declared_pack_kinds(self):
        config = config_with([declaration(), declaration("pack:market-data/price_history")])
        self.assertEqual(
            list(KINDS.builtin_kinds()) + ["pack:market-data/price_history", "pack:market-data/supplier_quote"],
            list(KINDS.valid_kinds(config)),
        )

    def test_valid_kinds_without_a_pack_are_the_builtins(self):
        self.assertEqual(list(KINDS.builtin_kinds()), list(KINDS.valid_kinds({})))


if __name__ == "__main__":
    unittest.main()
