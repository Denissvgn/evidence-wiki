"""Contract validation for normalized records (docs/normalized-source-format.md).

The contract only means something if a conforming foreign record is accepted and a
malformed one is named precisely, so every violation code is exercised against a record
that is otherwise valid.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "_normalized_contract.py"
NORMALIZE_PATH = REPO_ROOT / "workspace-template" / "scripts" / "normalize_sources.py"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONTRACT = load_script_module("research_normalized_contract", CONTRACT_PATH)
NORMALIZE = load_script_module("contract_tests_normalize_sources", NORMALIZE_PATH)

SOURCE_ID = "data:keepa-b0abc123"
RECORD_FILENAME = "data--keepa-b0abc123.md"

FOREIGN_FRONTMATTER = """\
type: normalized_source
normalized_format: 1
source_id: data:keepa-b0abc123
source_kind: structured_data
status: content_extracted
evidence_usable: true
unusable_evidence_reasons: null
created: 2026-08-07
updated: 2026-08-07
raw_paths:
  - raw/data/keepa-b0abc123.json
manifest_path: sources/manifest.jsonl
normalizer:
  name: autoseller-normalize
  version: 1.4.0
parse_warnings: []
raw_fingerprint: sha256:abc
"""

BODY = """\

# Keepa price history

## Citation Metadata

- URL: https://api.keepa.com/product/B0ABC123

## Abstract

Price history snapshot.

## Outline

- supplier_quote
- price_history

## Extracted Text

### supplier_quote

- price: 23.99 EUR

## Figures and Tables

- None recorded.

## Links

- None recorded.

## Raw Source Paths

- `raw/data/keepa-b0abc123.json`

## Parse Warnings

- None recorded.
"""


def record_text(frontmatter: str = FOREIGN_FRONTMATTER, body: str = BODY) -> str:
    return f"---\n{frontmatter}---\n{body}"


class NormalizedContractTests(unittest.TestCase):
    def manifest(self, **overrides):
        record = {
            "id": SOURCE_ID,
            "kind": "structured_data",
            "raw_paths": ["raw/data/keepa-b0abc123.json"],
            "raw_fingerprint": "sha256:abc",
        }
        record.update(overrides)
        return {SOURCE_ID: record}

    def validate(self, text: str, *, manifest=None, filename: str = RECORD_FILENAME):
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_root = Path(tmpdir) / "sources" / "normalized"
            normalized_root.mkdir(parents=True)
            path = normalized_root / filename
            path.write_text(text, encoding="utf-8")
            return CONTRACT.validate_record(
                path,
                manifest_by_id=self.manifest() if manifest is None else manifest,
                normalized_root=normalized_root,
            )

    def codes(self, violations):
        return [violation.code for violation in violations]

    def assertOnlyCode(self, violations, code):
        self.assertEqual([code], self.codes(violations), [v.message for v in violations])

    # -- contract constants ------------------------------------------------------

    def test_accepted_formats_include_the_written_version(self):
        self.assertIn(CONTRACT.NORMALIZED_FORMAT_VERSION, CONTRACT.ACCEPTED_NORMALIZED_FORMATS)
        self.assertNotIn(CONTRACT.LEGACY_NORMALIZED_FORMAT_VERSION, CONTRACT.ACCEPTED_NORMALIZED_FORMATS)

    def test_every_violation_code_is_namespaced_and_unique(self):
        self.assertEqual(len(set(CONTRACT.VIOLATION_CODES)), len(CONTRACT.VIOLATION_CODES))
        for code in CONTRACT.VIOLATION_CODES:
            self.assertTrue(code.startswith("NORMALIZED_CONTRACT_"), code)

    # -- golden paths ------------------------------------------------------------

    def test_conforming_foreign_record_passes(self):
        self.assertEqual([], self.validate(record_text()))

    def test_conforming_native_record_passes(self):
        native = FOREIGN_FRONTMATTER.replace(
            "  name: autoseller-normalize\n  version: 1.4.0\n",
            "  name: normalize_sources.py\n  version: 3\n",
        )
        self.assertEqual([], self.validate(record_text(native)))

    def test_legacy_native_record_without_a_format_version_passes(self):
        # Records this package wrote before the contract was versioned stay readable;
        # re-normalization backfills the field.
        legacy = FOREIGN_FRONTMATTER.replace("normalized_format: 1\n", "").replace(
            "  name: autoseller-normalize\n  version: 1.4.0\n",
            "  name: normalize_sources.py\n  version: 2\n",
        )
        self.assertEqual([], self.validate(record_text(legacy)))

    def test_manual_record_needs_no_manifest_entry(self):
        manual = (
            FOREIGN_FRONTMATTER.replace(
                "source_id: data:keepa-b0abc123", "source_id: manual:lab-notes"
            )
            .replace("raw_fingerprint: sha256:abc\n", "")
            .replace("  - raw/data/keepa-b0abc123.json\n", "")
            .replace("raw_paths:\n", "raw_paths: []\n")
        )
        self.assertEqual(
            [],
            self.validate(record_text(manual), manifest={}, filename="manual--lab-notes.md"),
        )

    def test_unquoted_dates_parse_as_dates_and_are_accepted(self):
        # PyYAML resolves unquoted `2026-08-07` to a date object; both spellings conform.
        quoted = FOREIGN_FRONTMATTER.replace("created: 2026-08-07", "created: '2026-08-07'")
        self.assertEqual([], self.validate(record_text(quoted)))

    # -- frontmatter -------------------------------------------------------------

    def test_record_without_frontmatter_is_reported_once(self):
        violations = self.validate("# Keepa price history\n\nNo frontmatter here.\n")
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_MISSING)

    def test_unterminated_frontmatter_is_reported(self):
        violations = self.validate(f"---\n{FOREIGN_FRONTMATTER}\n# no closing delimiter\n")
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_MISSING)

    def test_invalid_yaml_frontmatter_is_reported(self):
        violations = self.validate(record_text("type: [unclosed\n"))
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_MISSING)

    def test_wrong_record_type_is_reported(self):
        violations = self.validate(record_text(FOREIGN_FRONTMATTER.replace("type: normalized_source", "type: source")))
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_INVALID)
        self.assertEqual("type", violations[0].field)

    def test_unknown_status_is_reported(self):
        violations = self.validate(
            record_text(FOREIGN_FRONTMATTER.replace("status: content_extracted", "status: finished"))
        )
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_INVALID)
        self.assertEqual("status", violations[0].field)

    def test_missing_evidence_usable_is_reported(self):
        # Coverage policies reject unusable evidence, so an omission must not read as
        # usable by default.
        violations = self.validate(record_text(FOREIGN_FRONTMATTER.replace("evidence_usable: true\n", "")))
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_INVALID)
        self.assertEqual("evidence_usable", violations[0].field)

    def test_missing_required_fields_are_each_named(self):
        stripped = FOREIGN_FRONTMATTER.replace("source_kind: structured_data\n", "").replace(
            "manifest_path: sources/manifest.jsonl\n", ""
        )
        violations = self.validate(record_text(stripped))
        self.assertEqual(
            ["source_kind", "manifest_path"],
            [violation.field for violation in violations if violation.code == CONTRACT.FRONTMATTER_INVALID],
        )

    def test_normalizer_block_must_identify_a_tool(self):
        violations = self.validate(
            record_text(FOREIGN_FRONTMATTER.replace("  name: autoseller-normalize\n", ""))
        )
        codes = self.codes(violations)
        self.assertIn(CONTRACT.FRONTMATTER_INVALID, codes)
        self.assertIn("normalizer.name", [violation.field for violation in violations])

    def test_string_normalizer_version_is_accepted(self):
        # An external tool versions itself however it likes; the package stores it.
        self.assertEqual([], self.validate(record_text()))
        integer_version = FOREIGN_FRONTMATTER.replace("version: 1.4.0", "version: 7")
        self.assertEqual([], self.validate(record_text(integer_version)))

    def test_parse_warnings_must_be_a_list_of_strings(self):
        violations = self.validate(record_text(FOREIGN_FRONTMATTER.replace("parse_warnings: []", "parse_warnings: 3")))
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_INVALID)
        self.assertEqual("parse_warnings", violations[0].field)

    # -- format version ----------------------------------------------------------

    def test_foreign_record_without_a_format_version_is_refused(self):
        violations = self.validate(record_text(FOREIGN_FRONTMATTER.replace("normalized_format: 1\n", "")))
        self.assertOnlyCode(violations, CONTRACT.FORMAT_VERSION_UNSUPPORTED)
        self.assertEqual("absent", violations[0].actual)

    def test_unaccepted_format_version_is_refused(self):
        violations = self.validate(record_text(FOREIGN_FRONTMATTER.replace("normalized_format: 1", "normalized_format: 99")))
        self.assertOnlyCode(violations, CONTRACT.FORMAT_VERSION_UNSUPPORTED)

    def test_absent_format_version_reads_as_legacy(self):
        legacy = FOREIGN_FRONTMATTER.replace("normalized_format: 1\n", "")
        frontmatter, _, _ = CONTRACT.split_record(record_text(legacy))
        self.assertEqual(CONTRACT.LEGACY_NORMALIZED_FORMAT_VERSION, CONTRACT.effective_format_version(frontmatter))

    def test_non_integer_format_version_is_refused(self):
        violations = self.validate(
            record_text(FOREIGN_FRONTMATTER.replace("normalized_format: 1", "normalized_format: '1'"))
        )
        self.assertOnlyCode(violations, CONTRACT.FORMAT_VERSION_UNSUPPORTED)

    # -- sections ----------------------------------------------------------------

    def test_missing_section_is_named(self):
        violations = self.validate(record_text(body=BODY.replace("## Outline\n\n- supplier_quote\n- price_history\n\n", "")))
        self.assertOnlyCode(violations, CONTRACT.SECTIONS_INVALID)
        self.assertIn("Outline", violations[0].message)

    def test_out_of_order_sections_are_reported(self):
        reordered = BODY.replace(
            "## Abstract\n\nPrice history snapshot.\n\n## Outline\n\n- supplier_quote\n- price_history\n",
            "## Outline\n\n- supplier_quote\n- price_history\n\n## Abstract\n\nPrice history snapshot.\n",
        )
        violations = self.validate(record_text(body=reordered))
        self.assertOnlyCode(violations, CONTRACT.SECTIONS_INVALID)
        self.assertIn("out of order", violations[0].message)

    def test_extracted_text_may_contain_its_own_level_two_headings(self):
        # A LaTeX `\section` renders as `##` inside Extracted Text, so extra headings
        # are ordinary content, not a contract breach.
        with_body_headings = BODY.replace(
            "### supplier_quote\n\n- price: 23.99 EUR\n",
            "## Introduction\n\nBody text.\n\n## Method\n\nMore body text.\n",
        )
        self.assertEqual([], self.validate(record_text(body=with_body_headings)))

    def test_body_heading_duplicating_a_section_name_is_not_a_breach(self):
        # A LaTeX `\section{Links}` renders as `## Links` inside Extracted Text, ahead of
        # the record's own Links section. Matching by position would call that disorder.
        duplicated = BODY.replace(
            "### supplier_quote\n\n- price: 23.99 EUR\n",
            "## Links\n\nLinks discussed by the source.\n",
        )
        self.assertEqual([], self.validate(record_text(body=duplicated)))

    def test_heading_case_differences_are_tolerated(self):
        self.assertEqual([], self.validate(record_text(body=BODY.replace("## Parse Warnings", "## Parse warnings"))))

    # -- parse-warning consistency -----------------------------------------------

    def test_declared_warning_missing_from_the_section_is_reported(self):
        hidden = FOREIGN_FRONTMATTER.replace(
            "parse_warnings: []", "parse_warnings:\n  - price series truncated at 90 days"
        )
        violations = self.validate(record_text(hidden))
        self.assertOnlyCode(violations, CONTRACT.WARNINGS_INCONSISTENT)
        self.assertEqual("price series truncated at 90 days", violations[0].actual)

    def test_declared_warning_restated_in_the_section_passes(self):
        frontmatter = FOREIGN_FRONTMATTER.replace(
            "parse_warnings: []", "parse_warnings:\n  - price series truncated at 90 days"
        )
        body = BODY.replace("- None recorded.\n", "- price series truncated at 90 days\n")
        self.assertEqual([], self.validate(record_text(frontmatter, body)))

    # -- manifest agreement ------------------------------------------------------

    def test_record_at_the_wrong_path_is_reported(self):
        violations = self.validate(record_text(), filename="keepa.md")
        self.assertOnlyCode(violations, CONTRACT.MANIFEST_MISMATCH)
        self.assertEqual("source_id", violations[0].field)

    def test_source_id_absent_from_the_manifest_is_reported(self):
        violations = self.validate(record_text(), manifest={})
        self.assertOnlyCode(violations, CONTRACT.MANIFEST_MISMATCH)
        self.assertIn("not in the manifest", violations[0].message)

    def test_record_omitting_a_manifest_raw_path_is_reported(self):
        manifest = self.manifest(raw_paths=["raw/data/keepa-b0abc123.json", "raw/data/keepa-offers.json"])
        violations = self.validate(record_text(), manifest=manifest)
        self.assertOnlyCode(violations, CONTRACT.MANIFEST_MISMATCH)
        self.assertEqual("raw_paths", violations[0].field)

    def test_record_may_list_more_raw_paths_than_the_manifest(self):
        # LaTeX includes and a paired PDF are evidence the manifest does not enumerate.
        extra = FOREIGN_FRONTMATTER.replace(
            "  - raw/data/keepa-b0abc123.json\n",
            "  - raw/data/keepa-b0abc123.json\n  - raw/data/keepa-b0abc123.json.provenance.yml\n",
        )
        self.assertEqual([], self.validate(record_text(extra)))

    def test_fingerprint_disagreement_is_reported(self):
        violations = self.validate(record_text(), manifest=self.manifest(raw_fingerprint="sha256:changed"))
        self.assertOnlyCode(violations, CONTRACT.MANIFEST_MISMATCH)
        self.assertEqual("raw_fingerprint", violations[0].field)

    def test_absent_fingerprint_on_either_side_is_not_a_violation(self):
        manifest = self.manifest()
        del manifest[SOURCE_ID]["raw_fingerprint"]
        self.assertEqual([], self.validate(record_text(), manifest=manifest))
        self.assertEqual([], self.validate(record_text(FOREIGN_FRONTMATTER.replace("raw_fingerprint: sha256:abc\n", ""))))

    # -- reporting shape ---------------------------------------------------------

    def test_violations_serialize_with_stable_keys(self):
        violations = self.validate(record_text(), manifest={})
        payload = violations[0].to_dict()
        self.assertEqual(
            {"code", "message", "field", "expected", "actual", "remediation"},
            set(payload),
        )
        self.assertTrue(payload["remediation"])

    def test_unreadable_record_is_reported_rather_than_raised(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_root = Path(tmpdir)
            missing = normalized_root / RECORD_FILENAME
            violations = CONTRACT.validate_record(
                missing,
                manifest_by_id=self.manifest(),
                normalized_root=normalized_root,
            )
        self.assertOnlyCode(violations, CONTRACT.FRONTMATTER_MISSING)


class RenderedCoverageTests(unittest.TestCase):
    """`rendered_coverage` makes a capped rendering say so.

    Grounding is by containment against the body, so whatever a renderer capped away is
    citable but never quotable — silently. The counts are the renderer's own claim,
    which this package cannot recompute for a foreign renderer; what it can check is
    that the claim is internally coherent and describes *this* record's body.
    """

    BODY = "\n# T\n\n## Extracted Text\n\n### supplier_quote\n\n- price: 23.99 EUR\n\n### price_history\n\n- count: 90\n"

    def block(self, **overrides):
        value = {
            "total_values": 100,
            "rendered_values": 40,
            "ratio": 0.4,
            "sections": [
                {"heading": "supplier_quote", "total": 10, "rendered": 10},
                {"heading": "price_history", "total": 90, "rendered": 30, "note": "capped to 90 days"},
            ],
        }
        value.update(overrides)
        return value

    def validate(self, block, body=None):
        return CONTRACT.validate_rendered_coverage_block(block, self.BODY if body is None else body)

    def assertRejected(self, block, *, contains: str, body=None):
        violations = self.validate(block, body)
        self.assertTrue(violations, "expected a rendered-coverage violation")
        for violation in violations:
            self.assertEqual(CONTRACT.RENDERED_COVERAGE_INVALID, violation.code)
        self.assertTrue(
            any(contains in violation.message for violation in violations),
            [violation.message for violation in violations],
        )

    # -- accepted shapes ---------------------------------------------------------

    def test_a_coherent_block_is_accepted(self):
        self.assertEqual([], self.validate(self.block()))

    def test_full_coverage_is_accepted(self):
        self.assertEqual(
            [],
            self.validate(
                self.block(
                    total_values=10,
                    rendered_values=10,
                    ratio=1.0,
                    sections=[{"heading": "supplier_quote", "total": 10, "rendered": 10}],
                )
            ),
        )

    def test_nothing_considered_reads_as_full_coverage(self):
        # A renderer that considered no values has dropped none; the ratio is 1.0
        # rather than an undefined division.
        self.assertEqual([], self.validate({"total_values": 0, "rendered_values": 0, "ratio": 1.0}))

    def test_sections_are_optional(self):
        self.assertEqual([], self.validate({"total_values": 4, "rendered_values": 2, "ratio": 0.5}))

    def test_ratio_rounding_is_tolerated(self):
        # 1/3 reported to two decimal places is still an honest claim.
        self.assertEqual([], self.validate({"total_values": 3, "rendered_values": 1, "ratio": 0.33}))

    def test_x_prefixed_keys_are_tolerated(self):
        self.assertEqual([], self.validate(self.block(**{"x-renderer-note": "internal"})))

    # -- rejected shapes ---------------------------------------------------------

    def test_non_mapping_block_is_rejected(self):
        self.assertRejected([1, 2], contains="must be a mapping")

    def test_unknown_keys_are_rejected(self):
        self.assertRejected(self.block(coverage=1), contains="unknown keys: coverage")

    def test_negative_or_non_integer_counts_are_rejected(self):
        self.assertRejected(self.block(total_values=-1), contains="total_values")
        self.assertRejected(self.block(rendered_values="40"), contains="rendered_values")

    def test_rendering_more_than_considered_is_rejected(self):
        self.assertRejected(
            self.block(total_values=10, rendered_values=20), contains="renders more values than it considered"
        )

    def test_ratio_inconsistent_with_its_own_counts_is_rejected(self):
        self.assertRejected(self.block(ratio=0.95), contains="does not match its own counts")

    def test_non_numeric_ratio_is_rejected(self):
        self.assertRejected(self.block(ratio="0.4"), contains="must be a number")

    def test_a_section_heading_absent_from_the_body_is_rejected(self):
        # A coverage entry naming a heading the body does not have describes some other
        # rendering, and a reader would trust it for this one.
        self.assertRejected(
            self.block(
                sections=[{"heading": "offers_snapshot", "total": 10, "rendered": 10}],
                total_values=10,
                rendered_values=10,
                ratio=1.0,
            ),
            contains="not a heading in the record body",
        )

    def test_a_fully_dropped_facet_needs_no_heading(self):
        # The entry exists to account for content the body does not contain, so
        # demanding a heading for it would forbid the case the block exists to express.
        self.assertEqual(
            [],
            self.validate(
                self.block(
                    total_values=100,
                    rendered_values=10,
                    ratio=0.1,
                    sections=[
                        {"heading": "supplier_quote", "total": 10, "rendered": 10},
                        {"heading": "offers_snapshot", "total": 90, "rendered": 0, "note": "dropped in full"},
                    ],
                )
            ),
        )

    def test_section_counts_may_not_exceed_the_totals(self):
        self.assertRejected(
            self.block(total_values=5, rendered_values=5, ratio=1.0),
            contains="account for more values than the record considered",
        )

    def test_section_rendering_more_than_it_considered_is_rejected(self):
        self.assertRejected(
            self.block(sections=[{"heading": "supplier_quote", "total": 1, "rendered": 5}]),
            contains="renders more values than it considered",
        )

    def test_section_note_must_be_meaningful_when_present(self):
        self.assertRejected(
            self.block(sections=[{"heading": "supplier_quote", "total": 1, "rendered": 1, "note": "  "}]),
            contains="note must be a non-empty string",
        )

    # -- when it is required -----------------------------------------------------

    def test_an_adapter_rendered_record_must_declare_coverage(self):
        violations = CONTRACT.check_rendered_coverage({"extraction_method": "adapter"}, self.BODY)
        self.assertEqual([CONTRACT.RENDERED_COVERAGE_INVALID], [v.code for v in violations])
        self.assertIn("must declare `rendered_coverage`", violations[0].message)

    def test_other_records_may_omit_coverage(self):
        # A hand-written record comes from a tool this package does not control, so the
        # contract can only check the declaration when there is one.
        for method in ("manual", "pdf_text", "table_text", None):
            with self.subTest(extraction_method=method):
                self.assertEqual([], CONTRACT.check_rendered_coverage({"extraction_method": method}, self.BODY))

    def test_a_declared_block_is_checked_whoever_wrote_it(self):
        violations = CONTRACT.check_rendered_coverage(
            {"extraction_method": "manual", "rendered_coverage": self.block(ratio=0.95)}, self.BODY
        )
        self.assertEqual([CONTRACT.RENDERED_COVERAGE_INVALID], [v.code for v in violations])


class NativeOutputConformsTests(unittest.TestCase):
    """The normalizer's own output must satisfy the contract it publishes.

    This is the check that keeps the writer and the validator from drifting: if the
    package emits records that fail its own contract, a host targeting the contract has
    been told the wrong thing.
    """

    def write_and_validate(self, workspace: Path, record: dict, source) -> list:
        normalized_root = workspace / "sources" / "normalized"
        normalized_root.mkdir(parents=True, exist_ok=True)
        path, _ = NORMALIZE.write_normalized_source(
            source,
            normalized_root,
            "sources/manifest.jsonl",
            "2026-08-07",
            project_root=workspace,
        )
        return CONTRACT.validate_record(
            path,
            manifest_by_id={record["id"]: record},
            normalized_root=normalized_root,
        )

    def test_link_stub_output_conforms(self):
        record = {
            "id": "link:example-org-a2",
            "kind": "web_link",
            "url": "https://example.org/a2",
            "raw_paths": ["raw/links/a2.txt"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            source = NORMALIZE.normalize_link_record(record)
            self.assertEqual([], self.write_and_validate(workspace, record, source))

    def test_table_output_conforms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            csv_path = workspace / "raw" / "data" / "prices.csv"
            csv_path.parent.mkdir(parents=True)
            csv_path.write_text("sku,price,currency\nB0ABC,23.99,EUR\n", encoding="utf-8")
            record = {
                "id": "raw:prices",
                "kind": "table",
                "raw_paths": ["raw/data/prices.csv"],
            }
            source = NORMALIZE.normalize_table_record(workspace, record)
            self.assertEqual([], self.write_and_validate(workspace, record, source))

    def test_html_output_conforms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            html_path = workspace / "raw" / "web" / "guidance.html"
            html_path.parent.mkdir(parents=True)
            html_path.write_text(
                "<html><head><title>Guidance</title></head><body><h1>G</h1><p>Text.</p></body></html>",
                encoding="utf-8",
            )
            record = {
                "id": "raw:guidance",
                "kind": "html",
                "raw_paths": ["raw/web/guidance.html"],
            }
            source = NORMALIZE.normalize_html_record(workspace, record)
            self.assertEqual([], self.write_and_validate(workspace, record, source))

    def test_output_carrying_parse_warnings_conforms(self):
        # The warning-consistency check must accept what the renderer actually writes.
        record = {
            "id": "link:example-org-warned",
            "kind": "web_link",
            "url": "https://example.org/warned",
            "raw_paths": ["raw/links/warned.txt"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            source = NORMALIZE.normalize_link_record(record)
            source.warnings = ["link target was not fetched", "no title metadata available"]
            self.assertEqual([], self.write_and_validate(workspace, record, source))

    def test_native_writer_and_contract_agree_on_the_path_rule(self):
        self.assertEqual(
            NORMALIZE.safe_source_id("paper:2604.13018v1"),
            CONTRACT.safe_source_id("paper:2604.13018v1"),
        )

    def test_native_writer_stamps_an_accepted_format_version(self):
        self.assertIn(NORMALIZE.NORMALIZED_FORMAT_VERSION, CONTRACT.ACCEPTED_NORMALIZED_FORMATS)
        self.assertIn(NORMALIZE.NORMALIZER_NAME, CONTRACT.NATIVE_NORMALIZER_NAMES)


class SafeSourceIdTests(unittest.TestCase):
    def test_path_rule_matches_the_documented_examples(self):
        self.assertEqual("paper--2604.13018v1", CONTRACT.safe_source_id("paper:2604.13018v1"))
        self.assertEqual(
            "link--github-aweai-team-aiscientist-a1b2c3d4e5",
            CONTRACT.safe_source_id("link:github-aweai-team-aiscientist-a1b2c3d4e5"),
        )
        self.assertEqual("manual--lab-notes-agent-evals", CONTRACT.safe_source_id("manual:lab-notes-agent-evals"))

    def test_expected_record_path_uses_the_path_rule(self):
        root = Path("sources/normalized")
        self.assertEqual(
            root / "paper--2604.13018v1.md",
            CONTRACT.expected_record_path(root, "paper:2604.13018v1"),
        )


class CommittedRecordsConformTests(unittest.TestCase):
    """Every normalized record checked into this repo passes its own contract.

    The example workspace is shipped, and fixtures are what a reader opens to learn the
    format — a record here that fails `normalize_verify.py` teaches the wrong shape and
    makes the verifier look broken. Three of them did, in ways nothing caught: lint only
    holds *externally* produced records to the contract, so a native record missing a
    field, or a stub that was never a full record, stayed invisible.

    This walks the repo instead of naming workspaces, so a new fixture is enrolled by
    existing rather than by remembering to add it here.
    """

    SEARCH_ROOTS = ("tests/fixtures", "examples", "workspace-template")

    def workspaces(self) -> list[Path]:
        found: list[Path] = []
        for root in self.SEARCH_ROOTS:
            base = REPO_ROOT / root
            for config in sorted(base.rglob("research.yml")):
                workspace = config.parent
                normalized = workspace / "sources" / "normalized"
                if any(normalized.rglob("*.md")):
                    found.append(workspace)
        return found

    def manifest_by_id(self, workspace: Path, manifest_rel: str) -> dict[str, dict]:
        path = workspace / manifest_rel
        if not path.is_file():
            return {}
        indexed: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            source_id = record.get("id")
            if isinstance(source_id, str) and source_id and source_id not in indexed:
                indexed[source_id] = record
        return indexed

    def test_every_committed_normalized_record_conforms(self):
        workspaces = self.workspaces()
        # A silent zero would make this pass forever if the layout ever moves.
        self.assertGreaterEqual(len(workspaces), 4, "no workspaces with committed records found")

        for workspace in workspaces:
            config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8")) or {}
            manifest_rel, normalized_rel = NORMALIZE.source_paths(config)
            normalized_root = workspace / normalized_rel
            manifest_by_id = self.manifest_by_id(workspace, manifest_rel)
            for record in sorted(normalized_root.rglob("*.md")):
                label = record.relative_to(REPO_ROOT).as_posix()
                with self.subTest(record=label):
                    violations = CONTRACT.validate_record(
                        record,
                        manifest_by_id=manifest_by_id,
                        normalized_root=normalized_root,
                    )
                    self.assertEqual(
                        [],
                        [f"{v.code} ({v.field}): {v.message}" for v in violations],
                        f"{label} does not match {CONTRACT.CONTRACT_DOCUMENT}",
                    )


if __name__ == "__main__":
    unittest.main()
