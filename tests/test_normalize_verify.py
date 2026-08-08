"""`normalize_verify.py` — the entry point that makes the record format checkable.

The acceptance these tests encode is CR-2's: a record written by an external normalizer
is accepted on exactly the same terms as one this package wrote, and a malformed one is
refused with a stable code naming what is wrong.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
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


VERIFY = load_script_module("research_normalize_verify", "normalize_verify.py")

SOURCE_ID = "raw:raw-data-keepa-40efe41f3b"
RECORD_NAME = "raw--raw-data-keepa-40efe41f3b.md"
FINGERPRINT = "sha256:abc123"

FOREIGN_RECORD = f"""\
---
type: normalized_source
normalized_format: 1
source_id: {SOURCE_ID}
source_kind: unknown
status: content_extracted
evidence_usable: true
unusable_evidence_reasons: null
created: 2026-08-07
updated: 2026-08-07
raw_paths:
  - raw/data/keepa.json
manifest_path: sources/manifest.jsonl
normalizer:
  name: autoseller-normalize
  version: 1.4.0
parse_warnings: []
raw_fingerprint: {FINGERPRINT}
---

# Keepa snapshot B0ABC

## Citation Metadata

- URL: https://api.keepa.com/product/B0ABC

## Abstract

Supplier price snapshot.

## Outline

- supplier_quote

## Extracted Text

### supplier_quote

- price: 23.99 EUR

## Figures and Tables

- None recorded.

## Links

- None recorded.

## Raw Source Paths

- `raw/data/keepa.json`

## Parse Warnings

- None recorded.
"""


# A rendering that dropped most of its payload: the case the coverage block exists for.
CAPPED_RECORD = FOREIGN_RECORD.replace(
    f"raw_fingerprint: {FINGERPRINT}",
    "\n".join(
        [
            "rendered_coverage:",
            "  total_values: 40",
            "  rendered_values: 10",
            "  ratio: 0.25",
            "  sections:",
            "    - heading: supplier_quote",
            "      total: 38",
            "      rendered: 8",
            "      note: capped at 8 of 38 price points",
            "    - heading: metadata",
            "      total: 2",
            "      rendered: 2",
            f"raw_fingerprint: {FINGERPRINT}",
        ]
    ),
).replace(
    "- price: 23.99 EUR\n",
    "- price: 23.99 EUR\n\n### metadata\n\n- asin: B0ABC\n",
)


class NormalizeVerifyTests(unittest.TestCase):
    def make_workspace(self, root: Path, *, record: str | None = FOREIGN_RECORD) -> Path:
        target = root / "verify-workspace"
        (target / "sources" / "normalized").mkdir(parents=True)
        (target / "research.yml").write_text(
            "project:\n  name: Verify Fixture\n"
            "sources:\n  manifest_path: sources/manifest.jsonl\n  normalized_dir: sources/normalized\n",
            encoding="utf-8",
        )
        manifest_record = {
            "id": SOURCE_ID,
            "kind": "unknown",
            "raw_paths": ["raw/data/keepa.json"],
            "raw_fingerprint": FINGERPRINT,
        }
        (target / "sources" / "manifest.jsonl").write_text(
            json.dumps(manifest_record) + "\n",
            encoding="utf-8",
        )
        if record is not None:
            (target / "sources" / "normalized" / RECORD_NAME).write_text(record, encoding="utf-8")
        return target

    def run_verify(self, target: Path, *extra: str) -> tuple[int, dict, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY.main(["--project-root", str(target), *extra])
        raw = stdout.getvalue()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        return int(code or 0), payload, raw, stderr.getvalue()

    # -- acceptance --------------------------------------------------------------

    def test_conforming_foreign_record_verifies_with_no_adapter_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        self.assertEqual("verified", payload["overall_result"])
        record = payload["records"][0]
        self.assertEqual(SOURCE_ID, record["source_id"])
        self.assertEqual("external", record["origin"])
        self.assertEqual([], record["violations"])
        self.assertEqual({"name": "autoseller-normalize", "version": "1.4.0"}, record["normalizer"])

    def test_malformed_external_record_fails_with_stable_codes(self):
        broken = FOREIGN_RECORD.replace("normalized_format: 1\n", "").replace(
            "status: content_extracted", "status: done"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=broken)
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        self.assertEqual("not_verified", payload["overall_result"])
        record = payload["records"][0]
        self.assertEqual("invalid", record["result"])
        self.assertEqual(
            [
                "NORMALIZED_CONTRACT_FRONTMATTER_INVALID",
                "NORMALIZED_CONTRACT_FORMAT_VERSION_UNSUPPORTED",
            ],
            [violation["code"] for violation in record["violations"]],
        )
        self.assertEqual("status", record["violations"][0]["field"])
        self.assertTrue(all(violation["remediation"] for violation in record["violations"]))

    def test_native_and_foreign_records_are_reported_side_by_side(self):
        native = FOREIGN_RECORD.replace(
            "  name: autoseller-normalize\n  version: 1.4.0\n",
            "  name: normalize_sources.py\n  version: 3\n",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=native)
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        self.assertEqual("native", payload["records"][0]["origin"])
        self.assertEqual(1, payload["counts"]["native"])
        self.assertEqual(0, payload["counts"]["external"])

    def test_one_bad_record_does_not_hide_the_others(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            (target / "sources" / "normalized" / "orphan.md").write_text(
                "not a record at all\n", encoding="utf-8"
            )
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        self.assertEqual(2, payload["counts"]["records"])
        self.assertEqual(1, payload["counts"]["verified"])
        self.assertEqual(1, payload["counts"]["invalid"])

    # -- selection ---------------------------------------------------------------

    def test_source_id_selects_one_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            (target / "sources" / "normalized" / "other.md").write_text("junk\n", encoding="utf-8")
            code, payload, _, stderr = self.run_verify(target, "--source-id", SOURCE_ID)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        self.assertEqual(1, payload["counts"]["records"])
        self.assertEqual(SOURCE_ID, payload["records"][0]["source_id"])

    def test_all_matches_the_default_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            default_code, default_payload, _, _ = self.run_verify(target)
            all_code, all_payload, _, _ = self.run_verify(target, "--all")

        self.assertEqual(default_code, all_code)
        self.assertEqual(
            [record["path"] for record in default_payload["records"]],
            [record["path"] for record in all_payload["records"]],
        )

    def test_manifest_source_without_a_record_is_reported_not_skipped(self):
        # Asking to verify an inventoried source that was never normalized must not
        # quietly succeed; lint owns the workspace-wide "missing record" finding, but a
        # direct request deserves a direct answer.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=None)
            code, payload, _, stderr = self.run_verify(target, "--source-id", SOURCE_ID)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        record = payload["records"][0]
        self.assertFalse(record["exists"])
        self.assertEqual(
            ["NORMALIZED_CONTRACT_FRONTMATTER_MISSING"],
            [violation["code"] for violation in record["violations"]],
        )

    def test_unknown_source_id_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            code, _, raw_stdout, stderr = self.run_verify(target, "--source-id", "nope:not-real")

        self.assertEqual(VERIFY.EXIT_INVALID, code)
        self.assertEqual("", raw_stdout, "fatal errors must leave stdout empty")
        envelope = json.loads(stderr)
        self.assertEqual("SOURCE_UNKNOWN", envelope["error_code"])
        self.assertTrue(envelope["remediation"])

    # -- report shape ------------------------------------------------------------

    def test_report_is_one_json_document_on_stdout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            _, _, raw_stdout, stderr = self.run_verify(target)

        decoder = json.JSONDecoder()
        payload, consumed = decoder.raw_decode(raw_stdout.lstrip())
        self.assertEqual(len(raw_stdout.lstrip().rstrip()), consumed, "stdout carried more than one document")
        self.assertEqual(VERIFY.SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(VERIFY.DOCUMENT_TYPE, payload["document_type"])
        self.assertEqual("", stderr)

    def test_report_declares_the_contract_it_checked_against(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            _, payload, _, _ = self.run_verify(target)

        self.assertEqual("docs/normalized-source-format.md", payload["contract"]["document"])
        self.assertEqual(1, payload["contract"]["written_version"])
        self.assertEqual([1], payload["contract"]["accepted_versions"])
        self.assertFalse(payload["network_io_executed"])

    def test_empty_workspace_verifies_but_says_it_found_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=None)
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        self.assertEqual(0, payload["counts"]["records"])
        self.assertTrue(payload["warnings"], "an empty run must not look like a passing run")

    def test_text_format_names_each_violation_and_its_remediation(self):
        broken = FOREIGN_RECORD.replace("status: content_extracted", "status: done")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=broken)
            code, _, raw_stdout, _ = self.run_verify(target, "--format", "text")

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code)
        self.assertIn("NORMALIZED_CONTRACT_FRONTMATTER_INVALID", raw_stdout)
        self.assertIn("field: status", raw_stdout)
        self.assertIn("remediation:", raw_stdout)
        self.assertIn("Overall: not_verified", raw_stdout)

    def test_output_option_writes_the_report_to_a_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            destination = Path(tmpdir) / "report.json"
            code, _, raw_stdout, _ = self.run_verify(target, "--output", str(destination))

        self.assertEqual(VERIFY.EXIT_OK, code)
        self.assertEqual("", raw_stdout)

    # -- rendered coverage reporting ---------------------------------------------

    def test_record_without_a_coverage_block_reports_null_not_a_guess(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        # Absent is optional, and "no declaration" must not read as "renders nothing":
        # the block is only meaningful for records that render structured content.
        self.assertIsNone(payload["records"][0]["rendered_coverage"])

    def test_report_carries_the_declared_ratio_and_names_the_capped_facets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=CAPPED_RECORD)
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        coverage = payload["records"][0]["rendered_coverage"]
        self.assertEqual(0.25, coverage["ratio"])
        self.assertEqual(40, coverage["total_values"])
        self.assertEqual(10, coverage["rendered_values"])
        # The capped facet is the one a claim can cite but never quote, so naming it is
        # the whole point of reporting the block.
        self.assertEqual(["supplier_quote"], coverage["capped_sections"])

    def test_fully_rendered_sections_are_not_reported_as_capped(self):
        full = CAPPED_RECORD.replace("ratio: 0.25", "ratio: 1.0")
        full = full.replace("total_values: 40", "total_values: 10")
        full = full.replace("      total: 38", "      total: 8")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=full)
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        coverage = payload["records"][0]["rendered_coverage"]
        self.assertEqual(1.0, coverage["ratio"])
        self.assertEqual([], coverage["capped_sections"])

    def test_text_format_states_the_coverage_and_the_capped_facet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=CAPPED_RECORD)
            code, _, raw_stdout, stderr = self.run_verify(target, "--format", "text")

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        self.assertIn("rendered coverage: 0.25", raw_stdout)
        self.assertIn("10/40 values", raw_stdout)
        self.assertIn("capped: supplier_quote", raw_stdout)

    def test_coverage_is_reported_even_when_the_record_is_invalid(self):
        broken = CAPPED_RECORD.replace("status: content_extracted", "status: done")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=broken)
            code, payload, _, _ = self.run_verify(target)

        # Coverage is a property of the rendering, not a reward for conforming: an
        # operator triaging a bad record still needs to know how much of it is quotable.
        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code)
        self.assertEqual(0.25, payload["records"][0]["rendered_coverage"]["ratio"])

    # -- fatal workspace errors --------------------------------------------------

    def test_missing_config_is_a_fatal_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code, _, raw_stdout, stderr = self.run_verify(Path(tmpdir))

        self.assertEqual(VERIFY.EXIT_INVALID, code)
        self.assertEqual("", raw_stdout)
        self.assertEqual("CONFIG_MISSING", json.loads(stderr)["error_code"])

    def test_missing_manifest_is_a_fatal_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            (target / "sources" / "manifest.jsonl").unlink()
            code, _, raw_stdout, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_INVALID, code)
        self.assertEqual("", raw_stdout)
        self.assertEqual("MANIFEST_MISSING", json.loads(stderr)["error_code"])


if __name__ == "__main__":
    unittest.main()
