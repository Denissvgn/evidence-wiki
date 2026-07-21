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


INVENTORY = load_script_module("coverage_inventory", "source_inventory.py")
NORMALIZE = load_script_module("coverage_normalize", "normalize_sources.py")
QUERY = load_script_module("coverage_query_index", "query_index.py")
LINT = load_script_module("coverage_lint", "lint.py")
STATUS = load_script_module("coverage_workspace_status", "workspace_status.py")


WORKSPACE_CONFIG = """\
project:
  name: coverage-fixture
  description: Normalization coverage fixture.
raw:
  source_roots:
    - raw/web
    - raw/data
    - raw/pdf
sources:
  manifest_path: sources/manifest.jsonl
  normalized_dir: sources/normalized
  default_status: discovered
  lifecycle_statuses:
    - discovered
    - normalized
    - noted
    - integrated
    - deferred
    - superseded
    - rejected
wiki:
  root: wiki
  required_dirs: []
  allowed_page_types:
    - source
  frontmatter_required: []
lint:
  validate_structure: false
  validate_frontmatter: false
  validate_links: false
  validate_source_coverage: true
  validate_claims: false
  validate_questions: false
"""

WELL_FORMED_HTML = """\
<!DOCTYPE html>
<html>
<head>
<title>Spectral Reasoning in Language Models</title>
<meta name="description" content="An HTML-format paper about spectral reasoning benchmarks.">
<style>body { color: red; }</style>
<script>console.log("never extracted");</script>
</head>
<body>
<nav><a href="https://example.org/nav-link">Navigation chrome</a></nav>
<h1>Spectral Reasoning in Language Models</h1>
<p>This paper studies frequency-domain probes for reasoning evaluation.</p>
<h2>Benchmark Design</h2>
<p>We construct the SPECTRA benchmark with layered haystack documents.
See <a href="https://example.org/spectra-benchmark">the benchmark site</a>
and <a href="/relative/ignored">a relative link</a>.</p>
<h3>Scoring Protocol</h3>
<p>Scores aggregate harmonic precision across probe families.</p>
</body>
</html>
"""


def tiny_pdf_bytes(stream_text: str) -> bytes:
    stream = stream_text.encode("ascii")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\n"
            b"endobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        b"5 0 obj\n<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream\nendobj\n",
    ]
    pdf = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf += obj
    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    pdf += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        pdf += f"{offset:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return pdf


def text_pdf_bytes() -> bytes:
    return tiny_pdf_bytes(
        "BT\n"
        "/F1 18 Tf\n"
        "72 720 Td\n"
        "(A Tiny PDF Fixture For Normalization) Tj\n"
        "0 -36 Td\n"
        "/F1 12 Tf\n"
        "(Abstract) Tj\n"
        "0 -18 Td\n"
        "(This PDF-only fixture exercises pdftotext extraction without network access.) Tj\n"
        "0 -30 Td\n"
        "(1 Introduction) Tj\n"
        "0 -18 Td\n"
        "(The body includes enough characters for a useful normalized source.) Tj\n"
        "ET\n"
    )


def image_only_pdf_bytes() -> bytes:
    # One page of pure vector graphics: no text operators at all.
    return tiny_pdf_bytes("q\n1 0 0 RG\n72 72 468 648 re\nS\nQ\n")


class NormalizationCoverageBase(unittest.TestCase):
    def build_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        for sub in ("raw/web", "raw/data", "raw/pdf", "sources/normalized", "wiki/sources"):
            (workspace / sub).mkdir(parents=True)
        (workspace / "research.yml").write_text(WORKSPACE_CONFIG)
        return workspace

    def run_inventory(self, workspace: Path) -> list[dict]:
        config = INVENTORY.load_config(workspace)
        records, _, _ = INVENTORY.build_records(workspace, config, previous_detected_at={})
        INVENTORY.write_manifest(workspace / "sources" / "manifest.jsonl", records)
        return records

    def record_by_kind(self, records: list[dict], kind: str) -> dict:
        matches = [record for record in records if record.get("kind") == kind]
        self.assertEqual(1, len(matches), f"expected exactly one {kind} record")
        return matches[0]

    def normalize_record(self, workspace: Path, record: dict, pdf_extractor=None):
        config = NORMALIZE.load_config(workspace)
        eligible = NORMALIZE.eligible_records(workspace, [record])
        self.assertEqual(1, len(eligible), f"record {record.get('id')} must be eligible")
        source = NORMALIZE.normalize_selected_record(workspace, config, eligible[0], pdf_extractor)
        output_path = NORMALIZE.normalized_output_path_for_record(record, workspace / "sources" / "normalized")
        frontmatter = NORMALIZE.frontmatter_for(source, "sources/manifest.jsonl", output_path, "2026-06-10")
        output_path.write_text(NORMALIZE.render_markdown(source, frontmatter))
        return source, frontmatter, output_path


class HtmlNormalizationTests(NormalizationCoverageBase):
    """E19-T01: HTML inventory mapping and stdlib extraction."""

    def test_inventory_classifies_html_with_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "web" / "spectral-reasoning.html").write_text(WELL_FORMED_HTML)

            records = self.run_inventory(workspace)
            html_record = self.record_by_kind(records, "html")

            self.assertEqual(["raw/web/spectral-reasoning.html"], html_record["raw_paths"])
            self.assertTrue(html_record["raw_fingerprint"].startswith("sha256:"))

    def test_well_formed_page_extracts_title_outline_links_and_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "web" / "spectral-reasoning.html").write_text(WELL_FORMED_HTML)
            records = self.run_inventory(workspace)

            source, frontmatter, _ = self.normalize_record(workspace, self.record_by_kind(records, "html"))

            self.assertEqual("html_text", source.extraction_method)
            self.assertEqual("Spectral Reasoning in Language Models", source.title)
            self.assertEqual("An HTML-format paper about spectral reasoning benchmarks.", source.abstract)
            self.assertEqual(
                [(2, "Spectral Reasoning in Language Models"), (3, "Benchmark Design"), (4, "Scoring Protocol")],
                source.outline,
            )
            self.assertIn("https://example.org/spectra-benchmark", source.links)
            self.assertNotIn("/relative/ignored", source.links)
            self.assertIn("frequency-domain probes", source.extracted_text)
            self.assertIn("harmonic precision", source.extracted_text)
            self.assertNotIn("never extracted", source.extracted_text)
            self.assertNotIn("color: red", source.extracted_text)
            self.assertNotIn("Navigation chrome", source.extracted_text)
            self.assertEqual("content_extracted", frontmatter["status"])
            self.assertIsNone(frontmatter["needs_ocr"])

    def test_malformed_html_degrades_to_text_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            malformed = (
                "<html><body>"
                "</script></style></nav>"
                "<h2>Broken Heading</h2"
                "<p>Visible evidence text survives malformed markup."
                "</body>"
            )
            (workspace / "raw" / "web" / "broken.html").write_text(malformed)
            records = self.run_inventory(workspace)

            source, frontmatter, _ = self.normalize_record(workspace, self.record_by_kind(records, "html"))

            self.assertIn("Visible evidence text survives malformed markup.", source.extracted_text)
            self.assertTrue(any("malformed HTML markup" in warning for warning in source.warnings))
            self.assertNotEqual("failed", frontmatter["status"])

    def test_huge_page_is_truncated_with_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            filler = "<p>" + ("padding text block " * 50) + "</p>\n"
            page = "<html><head><title>Huge Page</title></head><body>\n"
            while len(page) <= NORMALIZE.HTML_MAX_BYTES:
                page += filler
            page += "</body></html>"
            (workspace / "raw" / "web" / "huge.html").write_text(page)
            records = self.run_inventory(workspace)

            source, _, _ = self.normalize_record(workspace, self.record_by_kind(records, "html"))

            self.assertTrue(any("extraction truncated" in warning for warning in source.warnings))
            self.assertEqual("Huge Page", source.title)
            self.assertIn("padding text block", source.extracted_text)

    def test_page_without_title_falls_back_to_heading(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "web" / "untitled.html").write_text(
                "<html><body><h1>Heading Title</h1><p>Body text here.</p></body></html>"
            )
            records = self.run_inventory(workspace)

            source, _, _ = self.normalize_record(workspace, self.record_by_kind(records, "html"))

            self.assertEqual("Heading Title", source.title)
            self.assertEqual("low", source.title_confidence)
            self.assertTrue(any("no <title> element" in warning for warning in source.warnings))

    def test_html_record_is_searchable_via_query_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "web" / "spectral-reasoning.html").write_text(WELL_FORMED_HTML)
            records = self.run_inventory(workspace)
            html_record = self.record_by_kind(records, "html")

            config = QUERY.load_config(workspace)
            self.assertIn(html_record["id"], QUERY.unnormalized_source_ids(workspace, config))

            self.normalize_record(workspace, html_record)
            self.assertEqual([], QUERY.unnormalized_source_ids(workspace, config))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = QUERY.main(
                    ["spectra", "benchmark", "--project-root", str(workspace), "--format", "json"]
                )
            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())
            self.assertGreaterEqual(payload["result_count"], 1)
            top = payload["results"][0]
            self.assertIn("sources/normalized/", top["path"])
            self.assertEqual("Spectral Reasoning in Language Models", top["title"])


class TableNormalizationTests(NormalizationCoverageBase):
    """E19-T03: CSV/TSV table normalization."""

    def test_clean_csv_yields_columns_rows_and_sample_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "data" / "benchmark-scores.csv").write_text(
                "model,spectra_score,latency_ms\n"
                "alpha,71.5,120\n"
                "beta,64.2,95\n"
                "gamma,80.1,210\n"
            )
            records = self.run_inventory(workspace)
            table_record = self.record_by_kind(records, "table")
            self.assertTrue(table_record["raw_fingerprint"].startswith("sha256:"))

            source, frontmatter, _ = self.normalize_record(workspace, table_record)

            self.assertEqual("table_text", source.extraction_method)
            self.assertEqual("benchmark-scores.csv", source.title)
            self.assertIn("Columns (3): model, spectra_score, latency_ms", source.extracted_text)
            self.assertIn("Data rows: 3", source.extracted_text)
            self.assertIn("| model | spectra_score | latency_ms |", source.extracted_text)
            self.assertIn("| alpha | 71.5 | 120 |", source.extracted_text)
            self.assertEqual([], source.warnings)
            self.assertEqual("content_extracted", frontmatter["status"])
            self.assertEqual("high", frontmatter["confidence"])

    def test_ragged_tsv_reports_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "data" / "ragged.tsv").write_text(
                "col_a\tcol_b\tcol_c\n"
                "1\t2\t3\n"
                "4\t5\n"
                "6\t7\t8\t9\n"
            )
            records = self.run_inventory(workspace)

            source, _, _ = self.normalize_record(workspace, self.record_by_kind(records, "table"))

            self.assertIn("Delimiter: tab", source.extracted_text)
            self.assertTrue(
                any("2 row(s) do not match the 3-column header" in warning for warning in source.warnings)
            )

    def test_huge_csv_is_truncated_with_lower_bound_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            row = "value-a,value-b,value-c\n"
            rows_needed = NORMALIZE.TABLE_MAX_BYTES // len(row) + 10
            with (workspace / "raw" / "data" / "huge.csv").open("w") as handle:
                handle.write("col_one,col_two,col_three\n")
                for _ in range(rows_needed):
                    handle.write(row)
            records = self.run_inventory(workspace)

            source, _, _ = self.normalize_record(workspace, self.record_by_kind(records, "table"))

            self.assertTrue(any("row scan truncated" in warning for warning in source.warnings))
            self.assertIn("Data rows: at least", source.extracted_text)

    def test_xlsx_table_stays_classified_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "data" / "workbook.xlsx").write_bytes(b"PK\x03\x04 not a real workbook")
            records = self.run_inventory(workspace)
            table_record = self.record_by_kind(records, "table")

            self.assertIsNone(NORMALIZE.normalization_method(workspace, table_record))
            self.assertNotIn("raw_fingerprint", table_record)
            config = QUERY.load_config(workspace)
            self.assertEqual([], QUERY.unnormalized_source_ids(workspace, config))

    def test_csv_columns_are_findable_via_query_index(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "data" / "benchmark-scores.csv").write_text(
                "model,spectra_score,latency_ms\nalpha,71.5,120\n"
            )
            records = self.run_inventory(workspace)
            self.normalize_record(workspace, self.record_by_kind(records, "table"))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = QUERY.main(
                    ["spectra_score", "--project-root", str(workspace), "--format", "json"]
                )
            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())
            self.assertGreaterEqual(payload["result_count"], 1)
            self.assertIn("benchmark-scores", payload["results"][0]["path"])


class NeedsOcrDetectionTests(NormalizationCoverageBase):
    """E19-T02: scanned-PDF detection and surfacing."""

    def normalize_pdf(self, workspace: Path) -> tuple:
        records = self.run_inventory(workspace)
        pdf_record = self.record_by_kind(records, "pdf")
        return self.normalize_record(workspace, pdf_record, NORMALIZE.resolve_pdf_extractor("pypdf"))

    def test_text_pdf_behavior_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "pdf" / "text-paper.pdf").write_bytes(text_pdf_bytes())

            source, frontmatter, _ = self.normalize_pdf(workspace)

            self.assertFalse(source.needs_ocr)
            self.assertIsNone(frontmatter["needs_ocr"])
            self.assertEqual("content_extracted", frontmatter["status"])
            self.assertFalse(any("needs OCR" in warning for warning in source.warnings))

    def test_image_only_pdf_is_flagged_needs_ocr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "pdf" / "scanned-paper.pdf").write_bytes(image_only_pdf_bytes())

            source, frontmatter, output_path = self.normalize_pdf(workspace)

            self.assertTrue(source.needs_ocr)
            self.assertTrue(frontmatter["needs_ocr"])
            self.assertEqual("partial", frontmatter["status"])
            self.assertEqual("low", frontmatter["confidence"])
            self.assertTrue(any("needs OCR" in warning for warning in source.warnings))
            self.assertIn("needs_ocr: true", output_path.read_text())

    def test_needs_ocr_surfaces_in_lint_and_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "pdf" / "scanned-paper.pdf").write_bytes(image_only_pdf_bytes())
            self.normalize_pdf(workspace)

            lint_results = LINT.run_checks(workspace, LINT.load_config(workspace))
            ocr_issues = [issue for issue in lint_results["issues"] if issue["category"] == "pdf_needs_ocr"]
            self.assertEqual(1, len(ocr_issues))
            self.assertEqual("LOW", ocr_issues[0]["severity"])
            self.assertIn("scanned-paper", ocr_issues[0]["files"][0])
            # The degraded record must not raise the HIGH extraction-failed issue.
            self.assertFalse(
                [issue for issue in lint_results["issues"] if issue["category"] == "pdf_extraction_failed"]
            )

            config = STATUS.load_yaml_mapping(workspace / "research.yml", "research.yml")
            sources = STATUS.sources_section(workspace, config)
            self.assertEqual(1, sources["needs_ocr"])

    def test_text_pdf_keeps_status_and_lint_clean_of_ocr_signals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "raw" / "pdf" / "text-paper.pdf").write_bytes(text_pdf_bytes())
            self.normalize_pdf(workspace)

            lint_results = LINT.run_checks(workspace, LINT.load_config(workspace))
            self.assertFalse(
                [issue for issue in lint_results["issues"] if issue["category"] == "pdf_needs_ocr"]
            )
            config = STATUS.load_yaml_mapping(workspace / "research.yml", "research.yml")
            self.assertEqual(0, STATUS.sources_section(workspace, config)["needs_ocr"])


if __name__ == "__main__":
    unittest.main()
