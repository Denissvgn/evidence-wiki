"""`normalize_verify.py` — the entry point that makes the record format checkable.

The acceptance these tests encode is CR-2's: a record written by an external normalizer
is accepted on exactly the same terms as one this package wrote, and a malformed one is
refused with a stable code naming what is wrong.
"""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


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

STRUCTURED_VIEW_INVALID = "NORMALIZED_CONTRACT_STRUCTURED_VIEW_INVALID"
SIDECAR_NAME = "raw--raw-data-keepa-40efe41f3b.structured.json"
SIDECAR_REL = f"sources/normalized/{SIDECAR_NAME}"
STRUCTURED_PAYLOAD = {
    "asin": "B0ABC",
    "supplier_quote": {"currency": "EUR", "price": "23.99 EUR"},
}


def sidecar_bytes(payload=None) -> bytes:
    document = STRUCTURED_PAYLOAD if payload is None else payload
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def content_hash(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def with_structured_view(
    *,
    record: str = FOREIGN_RECORD,
    path: str = SIDECAR_REL,
    digest: str | None = None,
    data: bytes | None = None,
) -> str:
    """The record, plus a `structured_view` block binding a sidecar to it."""
    payload = sidecar_bytes() if data is None else data
    block = "\n".join(
        [
            "structured_view:",
            f"  path: {path}",
            f"  content_hash: {digest or content_hash(payload)}",
            f"raw_fingerprint: {FINGERPRINT}",
        ]
    )
    return record.replace(f"raw_fingerprint: {FINGERPRINT}", block)


NATIVE_RECORD = FOREIGN_RECORD.replace(
    "  name: autoseller-normalize\n  version: 1.4.0\n",
    "  name: normalize_sources.py\n  version: 3\n",
)


class NormalizeVerifyTests(unittest.TestCase):
    def make_workspace(
        self,
        root: Path,
        *,
        record: str | None = FOREIGN_RECORD,
        sidecar: bytes | None = None,
        sidecar_name: str = SIDECAR_NAME,
    ) -> Path:
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
        if sidecar is not None:
            (target / "sources" / "normalized" / sidecar_name).write_bytes(sidecar)
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

    # -- structured-view sidecar -------------------------------------------------

    def test_native_record_with_a_conforming_sidecar_verifies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                record=with_structured_view(record=NATIVE_RECORD),
                sidecar=sidecar_bytes(),
            )
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        record = payload["records"][0]
        self.assertEqual([], record["violations"])
        self.assertEqual(
            {
                "declared": True,
                "path": SIDECAR_REL,
                "verified": True,
                "bytes": len(sidecar_bytes()),
            },
            record["structured_view"],
        )
        self.assertEqual(1, payload["counts"]["with_structured_view"])

    def test_foreign_record_with_a_hand_written_sidecar_verifies_on_the_same_terms(self):
        # The CR-2 promise applied to the sidecar: an external normalizer that writes a
        # conforming sidecar and binds it correctly is first-class evidence, with no
        # adapter involved and no native provenance claimed.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                record=with_structured_view(),
                sidecar=sidecar_bytes(),
            )
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        record = payload["records"][0]
        self.assertEqual("external", record["origin"])
        self.assertEqual([], record["violations"])
        self.assertTrue(record["structured_view"]["verified"])

    def test_record_without_a_structured_view_is_left_alone(self):
        # Every paper, PDF, web link and codebase record in every existing workspace.
        # The check must be silent for them, and must not invent a summary either.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        record = payload["records"][0]
        self.assertEqual([], record["violations"])
        self.assertIsNone(record["structured_view"])
        self.assertEqual(0, payload["counts"]["with_structured_view"])

    def test_explicit_null_structured_view_is_treated_as_undeclared(self):
        record = FOREIGN_RECORD.replace(
            f"raw_fingerprint: {FINGERPRINT}",
            f"structured_view: null\nraw_fingerprint: {FINGERPRINT}",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=record)
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        self.assertIsNone(payload["records"][0]["structured_view"])

    def test_declared_but_missing_sidecar_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=with_structured_view())
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        record = payload["records"][0]
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v["code"] for v in record["violations"]])
        self.assertEqual("structured_view.path", record["violations"][0]["field"])
        self.assertFalse(record["structured_view"]["verified"])
        self.assertIsNone(record["structured_view"]["bytes"])

    def test_tampered_sidecar_bytes_are_refused_by_the_hash_binding(self):
        clean = sidecar_bytes()
        tampered = sidecar_bytes({**STRUCTURED_PAYLOAD, "asin": "B0XYZ"})
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                record=with_structured_view(data=clean),
                sidecar=tampered,
            )
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        violations = payload["records"][0]["violations"]
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v["code"] for v in violations])
        self.assertEqual("structured_view.content_hash", violations[0]["field"])
        self.assertEqual(content_hash(clean), violations[0]["expected"])
        self.assertEqual(content_hash(tampered), violations[0]["actual"])

    def test_undeclared_sidecar_on_disk_is_refused(self):
        # `*.md` is the only thing any consumer globs, so a sidecar nothing declares is
        # a file no tool will ever open. Reporting it is the only way it is ever seen.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), sidecar=sidecar_bytes())
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        record = payload["records"][0]
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v["code"] for v in record["violations"]])
        self.assertEqual("structured_view", record["violations"][0]["field"])
        self.assertIn("does not declare it", record["violations"][0]["message"])
        self.assertIsNone(record["structured_view"])

    def test_binding_another_records_sidecar_is_refused(self):
        # The binding exists so an anchor cites the record it names. A record that
        # points at a neighbour's sidecar borrows evidence it never produced.
        other = "raw--raw-data-other-0000000000.structured.json"
        record = with_structured_view(path=f"sources/normalized/{other}")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                record=record,
                sidecar=sidecar_bytes(),
                sidecar_name=other,
            )
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        violations = payload["records"][0]["violations"]
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v["code"] for v in violations])
        self.assertEqual("structured_view.path", violations[0]["field"])
        self.assertIn("different record's sidecar", violations[0]["message"])

    def test_sidecar_path_outside_the_normalized_directory_is_refused(self):
        record = with_structured_view(path=f"sources/raw/{SIDECAR_NAME}")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=record, sidecar=sidecar_bytes())
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        violations = payload["records"][0]["violations"]
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v["code"] for v in violations])
        self.assertIn("normalized directory", violations[0]["message"])

    def test_malformed_binding_block_is_refused_by_shape(self):
        record = with_structured_view(digest="deadbeef").replace(
            "  content_hash:", "  format: json\n  content_hash:"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=record, sidecar=sidecar_bytes())
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        violations = payload["records"][0]["violations"]
        self.assertEqual([STRUCTURED_VIEW_INVALID] * 2, [v["code"] for v in violations])
        self.assertEqual(
            ["structured_view", "structured_view.content_hash"],
            [v["field"] for v in violations],
        )

    def test_sidecar_that_is_not_one_json_object_is_refused(self):
        payload_bytes = sidecar_bytes([{"price": "23.99 EUR"}])
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                record=with_structured_view(data=payload_bytes),
                sidecar=payload_bytes,
            )
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        violations = payload["records"][0]["violations"]
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v["code"] for v in violations])
        self.assertIn("single JSON object", violations[0]["message"])

    def test_sidecar_carrying_nan_is_refused(self):
        # `json.loads` accepts the `NaN` literal even though JSON does not, so a payload
        # that round-trips through this package would carry a value no other reader can.
        payload_bytes = b'{"price": NaN}\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                record=with_structured_view(data=payload_bytes),
                sidecar=payload_bytes,
            )
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        violations = payload["records"][0]["violations"]
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v["code"] for v in violations])
        self.assertIn("not JSON-serializable", violations[0]["message"])

    def test_binding_on_a_record_too_broken_to_locate_its_sidecar_is_not_reported_verified(self):
        # No `source_id` means no canonical sidecar location, so nothing was checked.
        # "Not checked" must not read as "checked and fine".
        record = with_structured_view().replace(f"source_id: {SOURCE_ID}\n", "")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), record=record, sidecar=sidecar_bytes())
            code, payload, _, stderr = self.run_verify(target)

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        self.assertFalse(payload["records"][0]["structured_view"]["verified"])

    # -- the shared validators units 4 and 5 call ---------------------------------

    def test_payload_validator_accepts_an_object_and_refuses_the_rest(self):
        validate = VERIFY.contract.validate_structured_payload
        self.assertEqual([], validate({"supplier_quote": {"price": "23.99 EUR"}}))
        self.assertEqual([], validate({}))
        for bad in ([{"price": 1}], "23.99", 23.99, None, True):
            self.assertEqual(
                [STRUCTURED_VIEW_INVALID],
                [v.code for v in validate(bad)],
                f"{bad!r} is not a JSON object",
            )

    def test_payload_validator_bounds_nesting_before_a_reader_recurses_into_it(self):
        depth = VERIFY.contract.STRUCTURED_VIEW_MAX_DEPTH
        deep = inner = {}
        for _ in range(depth + 4):
            inner["next"] = {}
            inner = inner["next"]
        violations = VERIFY.contract.validate_structured_payload(deep)
        self.assertEqual([STRUCTURED_VIEW_INVALID], [v.code for v in violations])
        self.assertIn(f"deeper than {depth} levels", violations[0].message)

        shallow = inner = {}
        for _ in range(depth - 2):
            inner["next"] = {}
            inner = inner["next"]
        self.assertEqual([], VERIFY.contract.validate_structured_payload(shallow))

    def test_payload_validator_terminates_on_a_cycle_instead_of_recursing(self):
        cyclic: dict = {}
        cyclic["self"] = cyclic
        self.assertEqual(
            [STRUCTURED_VIEW_INVALID],
            [v.code for v in VERIFY.contract.validate_structured_payload(cyclic)],
        )

    def test_block_validator_checks_shape_without_touching_disk(self):
        validate = VERIFY.contract.validate_structured_view_block
        good = {"path": SIDECAR_REL, "content_hash": content_hash(sidecar_bytes())}
        self.assertEqual([], validate(good))
        self.assertEqual([], validate({**good, "x-note": "experiments are exempt"}))
        self.assertEqual(
            ["structured_view.content_hash"],
            [v.field for v in validate({**good, "content_hash": "abc123"})],
        )
        self.assertEqual(
            ["structured_view.path"],
            [v.field for v in validate({**good, "path": "  "})],
        )
        self.assertEqual(["structured_view"], [v.field for v in validate([good])])
        self.assertEqual(
            ["adapter_structured"],
            [v.field for v in validate("nope", field="adapter_structured")],
        )

    def test_sidecar_naming_is_single_sourced_beside_the_record(self):
        root = Path("/workspace/sources/normalized")
        self.assertEqual(
            root / "raw--raw-data-keepa-40efe41f3b.structured.json",
            VERIFY.contract.expected_structured_path(root, SOURCE_ID),
        )
        # Same stem as the record, so the pair is obvious in a directory listing.
        self.assertEqual(
            VERIFY.contract.expected_record_path(root, SOURCE_ID).name.removesuffix(".md"),
            VERIFY.contract.expected_structured_path(root, SOURCE_ID).name.removesuffix(
                VERIFY.contract.STRUCTURED_VIEW_SUFFIX
            ),
        )

    def test_text_format_states_the_sidecar_and_whether_it_resolved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                record=with_structured_view(),
                sidecar=sidecar_bytes(),
            )
            code, _, raw_stdout, stderr = self.run_verify(target, "--format", "text")

        self.assertEqual(VERIFY.EXIT_OK, code, stderr)
        self.assertIn(f"structured view: verified ({SIDECAR_REL}", raw_stdout)

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
