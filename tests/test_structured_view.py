"""`_structured_view.py` — the three steps that decide an anchor.

Anchor-form grounding replaces "the record contains this sentence" with "this field of
this record holds this value". These tests encode what that upgrade is worth: a pointer
addresses exactly one field and never a neighbouring one, the sidecar it reads is the
one the record bound by hash, and the comparison is equality — so a claim that quotes a
substring of the evidence, or a number that only looks like the one on file, fails.

The traps are written out on purpose, because each is a way an anchor could pass while
proving nothing: `~01` decoding to `/` instead of the literal `~1`, `True` canonicalizing
to `"1"` because Python makes `bool` a subclass of `int`, and `Decimal(0.1)` comparing a
record's `0.1` against its full binary expansion.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


STRUCTURED_VIEW = load_script_module("cr7_structured_view_module", "_structured_view.py")
CONTRACT = load_script_module("cr7_structured_view_normalized_contract", "_normalized_contract.py")

SOURCE_ID = "raw:raw-data-keepa-40efe41f3b"
SIDECAR_NAME = "raw--raw-data-keepa-40efe41f3b.structured.json"
DECLARED_PATH = f"sources/normalized/{SIDECAR_NAME}"

# A structured view shaped like one a normalizer emits: a facet object with a numeric
# price, a boolean and a null; header-keyed rows; and two keys that exercise RFC 6901
# escaping because facet names may legitimately contain `/` and `~`.
DOCUMENT = {
    "supplier_quote": {
        "price": 23.99,
        "currency": "EUR",
        "in_stock": True,
        "discontinued_on": None,
        "tolerance": 0.1,
    },
    "rows": [
        {"date": "2026-08-01", "price": "24.50"},
        {"date": "2026-08-08", "price": "23.99"},
    ],
    "units/each": {"tilde~name": "kept verbatim"},
    "~1": "literal tilde one",
    "": "the empty key",
    "0": "a key that looks like an index",
}


def sidecar_bytes(document=DOCUMENT) -> bytes:
    return json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def frontmatter_for(payload: bytes, *, declared_hash: str | None = None) -> dict:
    """A normalized record's frontmatter, reduced to the block this module reads."""
    return {
        "structured_view": {
            "path": DECLARED_PATH,
            "content_hash": declared_hash if declared_hash is not None else STRUCTURED_VIEW.content_hash(payload),
        }
    }


def write_sidecar(root: Path, payload: bytes) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = STRUCTURED_VIEW.sidecar_path(root, SOURCE_ID)
    path.write_bytes(payload)
    return path


class SidecarPathTests(unittest.TestCase):
    def test_sidecar_sits_beside_its_record_under_the_same_safe_id(self):
        root = Path("workspace") / "sources" / "normalized"

        path = STRUCTURED_VIEW.sidecar_path(root, SOURCE_ID)
        record = CONTRACT.expected_record_path(root, SOURCE_ID)

        self.assertEqual(SIDECAR_NAME, path.name)
        self.assertEqual(record.parent, path.parent)
        self.assertEqual(record.name.removesuffix(".md"), path.name.removesuffix(STRUCTURED_VIEW.SIDECAR_SUFFIX))

    def test_the_safe_id_rule_is_the_contract_rule_and_not_a_second_one(self):
        for source_id in ("raw:a/b", "data--keepa--b0abc123", "Weird Id:With Spaces", "../escape"):
            with self.subTest(source_id=source_id):
                self.assertEqual(
                    f"{CONTRACT.safe_source_id(source_id)}{STRUCTURED_VIEW.SIDECAR_SUFFIX}",
                    STRUCTURED_VIEW.sidecar_path(Path("n"), source_id).name,
                )


class PointerResolutionTests(unittest.TestCase):
    def resolve(self, pointer: str, document=DOCUMENT):
        return STRUCTURED_VIEW.resolve_pointer(document, pointer)

    def test_empty_pointer_refers_to_the_whole_document(self):
        resolution = self.resolve("")

        self.assertTrue(resolution.ok)
        self.assertEqual("", resolution.pointer)
        self.assertIs(DOCUMENT, resolution.value)

    def test_leading_slash_is_optional_and_the_normalized_form_is_reported(self):
        without = self.resolve("supplier_quote/price")
        with_slash = self.resolve("/supplier_quote/price")

        self.assertTrue(without.ok)
        self.assertTrue(with_slash.ok)
        self.assertEqual(23.99, without.value)
        self.assertEqual("/supplier_quote/price", without.pointer)
        self.assertEqual(without.pointer, with_slash.pointer)

    def test_a_single_slash_addresses_the_empty_key_not_the_document(self):
        resolution = self.resolve("/")

        self.assertTrue(resolution.ok)
        self.assertEqual("the empty key", resolution.value)
        self.assertEqual("/", resolution.pointer)

    def test_tilde_one_addresses_a_key_containing_a_slash(self):
        resolution = self.resolve("/units~1each/tilde~0name")

        self.assertTrue(resolution.ok)
        self.assertEqual("kept verbatim", resolution.value)

    def test_tilde_zero_one_decodes_to_a_literal_tilde_one(self):
        # Unescaping `~0` before `~1` would turn this into the pointer `/1`; the RFC's
        # order makes it the key `~1`, which is the key the document actually has.
        resolution = self.resolve("/~01")

        self.assertTrue(resolution.ok)
        self.assertEqual("literal tilde one", resolution.value)

    def test_invalid_tilde_escapes_do_not_resolve(self):
        for pointer in ("/~2", "/~", "/units~each", "/~~0"):
            with self.subTest(pointer=pointer):
                resolution = self.resolve(pointer)

                self.assertFalse(resolution.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
                self.assertIn("RFC 6901", resolution.detail)

    def test_object_steps_match_a_key_exactly(self):
        for pointer in ("/Supplier_Quote/price", "/supplier_quote/Price", "/supplier", "/supplier_quote/pric"):
            with self.subTest(pointer=pointer):
                resolution = self.resolve(pointer)

                self.assertFalse(resolution.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)

    def test_missing_key_names_where_the_walk_stopped(self):
        resolution = self.resolve("/supplier_quote/vat")

        self.assertFalse(resolution.ok)
        self.assertEqual("/supplier_quote/vat", resolution.pointer)
        self.assertIn("'vat'", resolution.detail)
        self.assertIn("/supplier_quote", resolution.detail)

    def test_missing_members_carry_typed_location_without_changing_public_result(self):
        resolution = self.resolve("/supplier_quote/vat")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
        self.assertEqual(STRUCTURED_VIEW.POINTER_FAILURE_MISSING_MEMBER, resolution.failure_kind)
        self.assertEqual(1, resolution.failed_token_index)
        self.assertEqual("/supplier_quote", resolution.container_path)
        self.assertEqual("object", resolution.container_kind)
        self.assertFalse(resolution.traversed_array)

    def test_a_missing_parent_is_located_before_the_terminal_token(self):
        resolution = self.resolve("/missing_parent/value")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.POINTER_FAILURE_MISSING_MEMBER, resolution.failure_kind)
        self.assertEqual(0, resolution.failed_token_index)
        self.assertEqual("the document root", resolution.container_path)
        self.assertFalse(resolution.traversed_array)

    def test_pointer_failure_kinds_distinguish_every_invalid_walk(self):
        cases = {
            "/~2": STRUCTURED_VIEW.POINTER_FAILURE_INVALID_ESCAPE,
            "/rows/01": STRUCTURED_VIEW.POINTER_FAILURE_INVALID_INDEX,
            "/rows/2": STRUCTURED_VIEW.POINTER_FAILURE_INDEX_OUT_OF_RANGE,
            "/supplier_quote/price/cents": STRUCTURED_VIEW.POINTER_FAILURE_NON_CONTAINER,
        }
        for pointer, expected in cases.items():
            with self.subTest(pointer=pointer):
                resolution = self.resolve(pointer)

                self.assertFalse(resolution.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
                self.assertEqual(expected, resolution.failure_kind)
                self.assertIsNotNone(resolution.failed_token_index)
                self.assertIsNotNone(resolution.container_path)
                self.assertIsNotNone(resolution.container_kind)

    def test_successful_array_traversal_is_remembered_on_a_later_mapping_miss(self):
        resolution = self.resolve("/rows/0/gtin")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.POINTER_FAILURE_MISSING_MEMBER, resolution.failure_kind)
        self.assertEqual(2, resolution.failed_token_index)
        self.assertEqual("/rows/0", resolution.container_path)
        self.assertEqual("object", resolution.container_kind)
        self.assertTrue(resolution.traversed_array)

    def test_successful_array_traversal_is_also_visible_on_success(self):
        resolution = self.resolve("/rows/0/date")

        self.assertTrue(resolution.ok)
        self.assertTrue(resolution.traversed_array)
        self.assertIsNone(resolution.failure_kind)

    def test_deep_pointer_walks_objects_and_arrays_together(self):
        resolution = self.resolve("rows/1/price")

        self.assertTrue(resolution.ok)
        self.assertEqual("23.99", resolution.value)
        self.assertEqual("/rows/1/price", resolution.pointer)

    def test_array_index_zero_resolves(self):
        resolution = self.resolve("/rows/0/date")

        self.assertTrue(resolution.ok)
        self.assertEqual("2026-08-01", resolution.value)

    def test_array_steps_reject_everything_that_is_not_a_decimal_index(self):
        for step in ("01", "-1", "+1", "-", "1.0", "one", "", " 1", "0x1"):
            with self.subTest(step=step):
                resolution = self.resolve(f"/rows/{step}")

                self.assertFalse(resolution.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
                self.assertIn("decimal index", resolution.detail)

    def test_array_index_past_the_end_has_no_referent(self):
        resolution = self.resolve("/rows/2")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
        self.assertIn("2 entries", resolution.detail)

    def test_an_index_shaped_key_still_reads_as_a_key_on_an_object(self):
        resolution = self.resolve("/0")

        self.assertTrue(resolution.ok)
        self.assertEqual("a key that looks like an index", resolution.value)

    def test_walking_past_a_scalar_resolves_to_nothing(self):
        resolution = self.resolve("/supplier_quote/price/cents")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
        self.assertIn("number", resolution.detail)

    def test_a_resolved_null_is_a_value_not_a_miss(self):
        resolution = self.resolve("/supplier_quote/discontinued_on")

        self.assertTrue(resolution.ok)
        self.assertIsNone(resolution.value)

    def test_a_pointer_that_is_not_a_string_resolves_to_nothing(self):
        for pointer in (None, 7, ["supplier_quote", "price"]):
            with self.subTest(pointer=pointer):
                resolution = self.resolve(pointer)

                self.assertFalse(resolution.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
                self.assertIn("must be a string", resolution.detail)

    def test_normalize_pointer_leaves_the_root_pointer_empty(self):
        self.assertEqual("", STRUCTURED_VIEW.normalize_pointer(""))
        self.assertEqual("/a", STRUCTURED_VIEW.normalize_pointer("a"))
        self.assertEqual("/a", STRUCTURED_VIEW.normalize_pointer("/a"))

    def test_unescape_token_reports_malformed_escaping_rather_than_guessing(self):
        self.assertEqual("a/b", STRUCTURED_VIEW.unescape_token("a~1b"))
        self.assertEqual("a~b", STRUCTURED_VIEW.unescape_token("a~0b"))
        self.assertEqual("~1", STRUCTURED_VIEW.unescape_token("~01"))
        self.assertIsNone(STRUCTURED_VIEW.unescape_token("~2"))
        self.assertIsNone(STRUCTURED_VIEW.unescape_token("trailing~"))


class CanonicalScalarTests(unittest.TestCase):
    def test_booleans_canonicalize_before_numbers(self):
        # `isinstance(True, int)` is true in Python. A number-first branch would render
        # `True` as "1" and let an anchor expecting the number one match a flag.
        self.assertEqual("true", STRUCTURED_VIEW.canonical_scalar(True))
        self.assertEqual("false", STRUCTURED_VIEW.canonical_scalar(False))
        self.assertEqual("1", STRUCTURED_VIEW.canonical_scalar(1))
        self.assertEqual("0", STRUCTURED_VIEW.canonical_scalar(0))

    def test_null_canonicalizes_to_null(self):
        self.assertEqual("null", STRUCTURED_VIEW.canonical_scalar(None))

    def test_numbers_and_strings_render_as_written(self):
        self.assertEqual("23.99", STRUCTURED_VIEW.canonical_scalar(23.99))
        self.assertEqual("0.1", STRUCTURED_VIEW.canonical_scalar(0.1))
        self.assertEqual("23.99 EUR", STRUCTURED_VIEW.canonical_scalar("23.99 EUR"))

    def test_subtrees_have_no_canonical_scalar_form(self):
        for value in ({"price": 23.99}, [23.99], []):
            with self.subTest(value=value):
                self.assertIsNone(STRUCTURED_VIEW.canonical_scalar(value))
                self.assertFalse(STRUCTURED_VIEW.is_scalar(value))

    def test_is_scalar_admits_exactly_the_json_scalars(self):
        for value in ("text", "", 0, 1, -3, 23.99, True, False, None):
            with self.subTest(value=value):
                self.assertTrue(STRUCTURED_VIEW.is_scalar(value))

    def test_parse_decimal_refuses_the_decimal_special_forms(self):
        for text in ("NaN", "-NaN", "sNaN", "Infinity", "-Infinity", "inf", "1_000", "", "23.99 EUR", "0x10"):
            with self.subTest(text=text):
                self.assertIsNone(STRUCTURED_VIEW.parse_decimal(text))
        self.assertEqual(0, STRUCTURED_VIEW.parse_decimal(" 23.990 ").compare(STRUCTURED_VIEW.parse_decimal("23.99")))


class ExpectedMatchesTests(unittest.TestCase):
    def assert_match(self, target, expected, matches: bool):
        self.assertEqual(matches, STRUCTURED_VIEW.expected_matches(target, expected))

    def test_string_targets_compare_after_text_normalization(self):
        for expected, matches in (
            ("23.99 EUR", True),
            ("23.99 eur", True),
            ("  23.99   EUR ", True),
            ("23.99EUR", False),
            ("23.99", False),
            ("EUR", False),
        ):
            with self.subTest(expected=expected):
                self.assert_match("23.99 EUR", expected, matches)

    def test_string_comparison_is_equality_and_never_containment(self):
        # The whole point of anchors: a claim quoting part of the field does not pass.
        self.assert_match("Supplier price is 23.99 EUR including VAT", "23.99 EUR", False)
        self.assert_match("23.99", "Supplier price is 23.99 EUR", False)

    def test_string_targets_fold_the_unicode_artifacts_the_quote_path_folds(self):
        self.assert_match("don’t ship", "don't ship", True)
        self.assert_match("2026–08–01", "2026-08-01", True)
        self.assert_match("ﬁnal price", "final price", True)

    def test_number_targets_compare_as_decimals(self):
        for expected, matches in (
            ("23.99", True),
            ("23.990", True),
            ("+23.99", True),
            (23.99, True),
            ("23.9", False),
            ("23.99 EUR", False),
            ("", False),
        ):
            with self.subTest(expected=expected):
                self.assert_match(23.99, expected, matches)

    def test_float_targets_compare_as_written_not_as_binary_expansion(self):
        # `Decimal(0.1)` is 0.1000000000000000055511151231257827; `Decimal(str(0.1))`
        # is the 0.1 the record shows a reader.
        self.assert_match(0.1, "0.1", True)
        self.assert_match(0.1, "0.1000000000000000055511151231257827", False)
        self.assert_match(0.3, "0.3", True)

    def test_integer_targets_accept_equal_decimal_spellings(self):
        for expected, matches in (("100", True), ("100.00", True), ("1e2", True), ("1E2", True), ("101", False)):
            with self.subTest(expected=expected):
                self.assert_match(100, expected, matches)

    def test_number_targets_never_match_the_decimal_special_forms(self):
        for expected in ("NaN", "sNaN", "Infinity", "-Infinity", "inf"):
            with self.subTest(expected=expected):
                self.assert_match(23.99, expected, False)

    def test_a_non_finite_target_matches_nothing(self):
        # `json.loads` accepts JavaScript's NaN/Infinity extensions, so this value can
        # reach the comparison from a real sidecar.
        not_a_number = json.loads("NaN")
        infinity = json.loads("Infinity")

        self.assert_match(not_a_number, "NaN", False)
        self.assert_match(not_a_number, "0", False)
        self.assert_match(infinity, "Infinity", False)

    def test_boolean_targets_compare_against_true_and_false_only(self):
        for expected, matches in (
            ("true", True),
            ("True", True),
            ("  TRUE  ", True),
            (True, True),
            ("1", False),
            ("yes", False),
            ("false", False),
        ):
            with self.subTest(expected=expected):
                self.assert_match(True, expected, matches)
        self.assert_match(False, "false", True)
        self.assert_match(False, "0", False)
        self.assert_match(False, "", False)

    def test_a_number_target_never_matches_a_boolean_spelling(self):
        self.assert_match(1, "true", False)
        self.assert_match(0, "false", False)
        self.assert_match(1, "1", True)

    def test_a_boolean_target_never_matches_its_integer_value(self):
        self.assert_match(True, "1", False)
        self.assert_match(False, "0", False)

    def test_null_targets_compare_against_null_only(self):
        for expected, matches in (("null", True), ("NULL", True), (" null ", True), ("None", False),
                                  ("", False), ("nil", False), ("0", False), ("false", False)):
            with self.subTest(expected=expected):
                self.assert_match(None, expected, matches)

    def test_a_string_target_holding_a_keyword_uses_the_string_rule(self):
        self.assert_match("true", "true", True)
        self.assert_match("null", "null", True)
        self.assert_match("23.99", "23.990", False)

    def test_subtree_targets_and_subtree_expectations_never_match(self):
        self.assert_match({"price": 23.99}, "23.99", False)
        self.assert_match([23.99], "23.99", False)
        self.assert_match("23.99 EUR", {"price": 23.99}, False)
        self.assert_match("23.99 EUR", ["23.99 EUR"], False)


class SidecarLoadTests(unittest.TestCase):
    def test_content_hash_uses_the_sha256_convention_of_raw_fingerprint(self):
        payload = sidecar_bytes()

        self.assertEqual(f"sha256:{hashlib.sha256(payload).hexdigest()}", STRUCTURED_VIEW.content_hash(payload))
        self.assertIsNotNone(STRUCTURED_VIEW.CONTENT_HASH_RE.fullmatch(STRUCTURED_VIEW.content_hash(payload)))

    def test_a_bound_sidecar_loads(self):
        payload = sidecar_bytes()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_sidecar(Path(tmpdir) / "normalized", payload)
            loaded = STRUCTURED_VIEW.load_sidecar(frontmatter_for(payload), path)

        self.assertTrue(loaded.ok, loaded.detail)
        self.assertIsNone(loaded.result)
        self.assertEqual(DOCUMENT, loaded.document)

    def test_a_record_without_the_block_carries_no_structured_view(self):
        payload = sidecar_bytes()
        for frontmatter in (
            {},
            {"structured_view": None},
            {"structured_view": "sources/normalized/x.structured.json"},
            {"structured_view": []},
            {"structured_view": {"content_hash": STRUCTURED_VIEW.content_hash(payload)}},
            {"structured_view": {"path": DECLARED_PATH}},
            {"structured_view": {"path": "", "content_hash": STRUCTURED_VIEW.content_hash(payload)}},
            {"structured_view": {"path": DECLARED_PATH, "content_hash": "   "}},
            {"structured_view": {"path": 7, "content_hash": STRUCTURED_VIEW.content_hash(payload)}},
            {"structured_view": {"path": DECLARED_PATH, "content_hash": 7}},
        ):
            with self.subTest(frontmatter=frontmatter), tempfile.TemporaryDirectory() as tmpdir:
                path = write_sidecar(Path(tmpdir) / "normalized", payload)
                loaded = STRUCTURED_VIEW.load_sidecar(frontmatter, path)

                self.assertFalse(loaded.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_MISSING, loaded.result)
                self.assertIsNone(loaded.document)

    def test_a_declared_but_absent_sidecar_is_missing_not_corrupt(self):
        payload = sidecar_bytes()
        with tempfile.TemporaryDirectory() as tmpdir:
            absent = STRUCTURED_VIEW.sidecar_path(Path(tmpdir), SOURCE_ID)
            loaded = STRUCTURED_VIEW.load_sidecar(frontmatter_for(payload), absent)

        self.assertFalse(loaded.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_MISSING, loaded.result)
        self.assertIn(DECLARED_PATH, loaded.detail)

    def test_a_malformed_content_hash_is_corrupt(self):
        payload = sidecar_bytes()
        for declared_hash in (
            "abc123",
            "sha256:zz",
            f"sha256:{hashlib.sha256(payload).hexdigest().upper()}",
            f"sha1:{hashlib.sha256(payload).hexdigest()}",
            f"sha256:{hashlib.sha256(payload).hexdigest()[:63]}",
            hashlib.sha256(payload).hexdigest(),
        ):
            with self.subTest(content_hash=declared_hash), tempfile.TemporaryDirectory() as tmpdir:
                path = write_sidecar(Path(tmpdir) / "normalized", payload)
                loaded = STRUCTURED_VIEW.load_sidecar(frontmatter_for(payload, declared_hash=declared_hash), path)

                self.assertFalse(loaded.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_CORRUPT, loaded.result)

    def test_tampered_bytes_break_the_binding(self):
        declared = sidecar_bytes()
        tampered = sidecar_bytes({**DOCUMENT, "supplier_quote": {**DOCUMENT["supplier_quote"], "price": 19.99}})
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_sidecar(Path(tmpdir) / "normalized", tampered)
            loaded = STRUCTURED_VIEW.load_sidecar(frontmatter_for(declared), path)

        self.assertFalse(loaded.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_CORRUPT, loaded.result)
        self.assertIn("hashes to", loaded.detail)

    def test_a_sidecar_that_is_not_one_json_object_is_corrupt(self):
        for payload in (
            b'[{"price": 23.99}]\n',
            b'"just a string"\n',
            b"42\n",
            b"null\n",
            b"true\n",
            b'{"price": 23.99} {"price": 24.99}\n',
            b"{not json}\n",
            b"",
        ):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmpdir:
                path = write_sidecar(Path(tmpdir) / "normalized", payload)
                loaded = STRUCTURED_VIEW.load_sidecar(frontmatter_for(payload), path)

                self.assertFalse(loaded.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_CORRUPT, loaded.result)
                self.assertIsNone(loaded.document)

    def test_a_sidecar_that_is_not_utf8_is_corrupt(self):
        payload = b'{"price": "\xff\xfe"}\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            path = write_sidecar(Path(tmpdir) / "normalized", payload)
            loaded = STRUCTURED_VIEW.load_sidecar(frontmatter_for(payload), path)

        self.assertFalse(loaded.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_CORRUPT, loaded.result)


class AnchorResolutionTests(unittest.TestCase):
    def resolve(self, pointer, expected, *, payload=None, frontmatter=None, write=True):
        payload = sidecar_bytes() if payload is None else payload
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "normalized"
            path = write_sidecar(root, payload) if write else STRUCTURED_VIEW.sidecar_path(root, SOURCE_ID)
            record = frontmatter_for(payload) if frontmatter is None else frontmatter
            return STRUCTURED_VIEW.resolve_anchor(record, path, pointer, expected)

    def test_a_matching_anchor_verifies_and_echoes_what_it_resolved(self):
        resolution = self.resolve("supplier_quote/price", "23.99")

        self.assertTrue(resolution.ok, resolution.detail)
        self.assertIsNone(resolution.result)
        self.assertEqual("/supplier_quote/price", resolution.pointer)
        self.assertEqual("23.99", resolution.resolved)
        self.assertIn("23.99", resolution.detail)

    def test_a_row_field_verifies_through_the_string_rule(self):
        resolution = self.resolve("/rows/1/price", "23.99")

        self.assertTrue(resolution.ok, resolution.detail)
        self.assertEqual("23.99", resolution.resolved)

    def test_a_boolean_field_verifies_against_true_and_not_against_one(self):
        verified = self.resolve("/supplier_quote/in_stock", "true")
        refused = self.resolve("/supplier_quote/in_stock", "1")

        self.assertTrue(verified.ok, verified.detail)
        self.assertEqual("true", verified.resolved)
        self.assertFalse(refused.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_VALUE_MISMATCH, refused.result)
        self.assertEqual("true", refused.resolved)

    def test_a_null_field_verifies_against_null(self):
        resolution = self.resolve("/supplier_quote/discontinued_on", "null")

        self.assertTrue(resolution.ok, resolution.detail)
        self.assertEqual("null", resolution.resolved)

    def test_a_mismatched_value_reports_what_the_record_actually_holds(self):
        resolution = self.resolve("/supplier_quote/price", "19.99")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_VALUE_MISMATCH, resolution.result)
        self.assertEqual("/supplier_quote/price", resolution.pointer)
        self.assertEqual("23.99", resolution.resolved)
        self.assertIn("19.99", resolution.detail)

    def test_a_unit_suffix_against_a_numeric_field_is_a_mismatch_not_a_crash(self):
        resolution = self.resolve("/supplier_quote/price", "23.99 EUR")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_VALUE_MISMATCH, resolution.result)

    def test_a_pointer_that_reaches_nothing_stops_at_pointer_not_found(self):
        resolution = self.resolve("/supplier_quote/vat", "0.19")

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_POINTER_NOT_FOUND, resolution.result)
        self.assertEqual("/supplier_quote/vat", resolution.pointer)
        self.assertIsNone(resolution.resolved)

    def test_a_pointer_to_a_subtree_is_refused_as_not_scalar(self):
        for pointer in ("/supplier_quote", "/rows", "/rows/0", ""):
            with self.subTest(pointer=pointer):
                resolution = self.resolve(pointer, "23.99")

                self.assertFalse(resolution.ok)
                self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_TARGET_NOT_SCALAR, resolution.result)
                self.assertIsNone(resolution.resolved)

    def test_a_record_without_a_structured_view_refuses_before_resolving(self):
        resolution = self.resolve("/supplier_quote/price", "23.99", frontmatter={"title": "A text record"})

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_MISSING, resolution.result)
        self.assertEqual("/supplier_quote/price", resolution.pointer)
        self.assertIsNone(resolution.resolved)

    def test_a_declared_but_absent_sidecar_refuses_before_resolving(self):
        resolution = self.resolve("/supplier_quote/price", "23.99", write=False)

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_MISSING, resolution.result)

    def test_a_tampered_sidecar_refuses_before_resolving(self):
        payload = sidecar_bytes()
        frontmatter = frontmatter_for(sidecar_bytes({"supplier_quote": {"price": 19.99}}))

        resolution = self.resolve("/supplier_quote/price", "23.99", payload=payload, frontmatter=frontmatter)

        self.assertFalse(resolution.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_STRUCTURED_VIEW_CORRUPT, resolution.result)
        self.assertIsNone(resolution.resolved)

    def test_every_failure_carries_a_stable_result_and_a_reason(self):
        failures = [
            self.resolve("/supplier_quote/price", "23.99", frontmatter={}),
            self.resolve("/supplier_quote/price", "23.99", payload=b"[]\n"),
            self.resolve("/nope", "23.99"),
            self.resolve("/supplier_quote", "23.99"),
            self.resolve("/supplier_quote/price", "19.99"),
        ]

        self.assertEqual(
            list(STRUCTURED_VIEW.ANCHOR_RESULTS),
            [failure.result for failure in failures],
        )
        for failure in failures:
            with self.subTest(result=failure.result):
                self.assertFalse(failure.ok)
                self.assertTrue(failure.detail)
                self.assertTrue(failure.detail.endswith("."))


if __name__ == "__main__":
    unittest.main()
