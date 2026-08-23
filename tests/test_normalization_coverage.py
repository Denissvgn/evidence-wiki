import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


INVENTORY = load_script_module("coverage_inventory", "source_inventory.py")
NORMALIZE = load_script_module("coverage_normalize", "normalize_sources.py")
QUERY = load_script_module("coverage_query_index", "query_index.py")
LINT = load_script_module("coverage_lint", "lint.py")
STATUS = load_script_module("coverage_workspace_status", "workspace_status.py")
CONTRACT = load_script_module("coverage_normalized_contract", "_normalized_contract.py")
STRUCTURED_VIEW = load_script_module("coverage_structured_view", "_structured_view.py")


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


class TableStructuredViewTests(NormalizationCoverageBase):
    """CR-7 T13: the structured-view sidecar the native tabular path emits.

    `normalize_table_record` already streams every row through `csv.reader` and then
    keeps 20 of them, cells ellipsized at 80 characters, for the quotable body. For a
    CSV price history that leaves nearly every row citable but permanently unquotable.
    These tests are about the uncapped complement: full cells, every row, addressed by
    pointer — and about the tables that are refused rather than half-rendered, since a
    sidecar that looks complete and is not would let an anchor cite evidence the file
    never held.

    Driven through `main` rather than `normalize_record`, because the sidecar is written
    by `write_normalized_source`; the base helper stops short of it.
    """

    PRICE_ROWS = 60
    ANCHOR_ROW = 41  # Past TABLE_SAMPLE_ROWS: unquotable in the body, anchorable here.

    def price_history_csv(self, rows: int = PRICE_ROWS) -> str:
        lines = ["date,price,seller"]
        for index in range(rows):
            lines.append(f"2026-08-{index % 28 + 1:02d},{20 + index / 100:.2f},Seller {index}")
        return "\n".join(lines) + "\n"

    def deliver(self, workspace: Path, name: str, text: str) -> None:
        (workspace / "raw" / "data" / name).write_text(text, encoding="utf-8")

    def normalize(self, workspace: Path, *extra: str) -> tuple[int, dict, str]:
        self.run_inventory(workspace)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = NORMALIZE.main(
                ["--project-root", str(workspace), "--all", "--format", "json", *extra]
            )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def table_record(self, workspace: Path) -> dict:
        records = [
            json.loads(line)
            for line in (workspace / "sources" / "manifest.jsonl").read_text().splitlines()
            if line.strip()
        ]
        return self.record_by_kind(records, "table")

    def record_path(self, workspace: Path) -> Path:
        return NORMALIZE.normalized_output_path_for_record(
            self.table_record(workspace), workspace / "sources" / "normalized"
        )

    def sidecar_path(self, workspace: Path) -> Path:
        return CONTRACT.expected_structured_path(
            workspace / "sources" / "normalized", self.table_record(workspace)["id"]
        )

    def frontmatter(self, workspace: Path) -> dict:
        return NORMALIZE.read_output_frontmatter(self.record_path(workspace))

    def contract_violations(self, workspace: Path) -> list:
        manifest = {record["id"]: record for record in [self.table_record(workspace)]}
        return CONTRACT.validate_record(
            self.record_path(workspace),
            manifest_by_id=manifest,
            normalized_root=workspace / "sources" / "normalized",
        )

    def skip_warnings(self, frontmatter: dict) -> list[str]:
        return [
            warning
            for warning in frontmatter.get("parse_warnings") or []
            if "no structured view emitted" in warning
        ]

    def assert_no_sidecar(self, workspace: Path, reason: str) -> None:
        """A refused table: no bytes, no binding, a warning naming why, body intact."""
        code, _, stderr = self.normalize(workspace)
        sidecar_exists = self.sidecar_path(workspace).exists()
        frontmatter = self.frontmatter(workspace)
        body = self.record_path(workspace).read_text()
        violations = self.contract_violations(workspace)
        warnings = self.skip_warnings(frontmatter)

        self.assertEqual(0, code, stderr)
        self.assertFalse(sidecar_exists, f"a sidecar was written for a table refused as: {reason}")
        self.assertIn("structured_view", frontmatter)
        self.assertIsNone(frontmatter["structured_view"])
        self.assertEqual(1, len(warnings), frontmatter.get("parse_warnings"))
        self.assertIn(reason, warnings[0])
        # Degrades to exactly today's behaviour: the quotable body is untouched.
        self.assertIn("Sample rows (first", body)
        self.assertEqual([], violations, [violation.message for violation in violations])

    def test_a_clean_csv_emits_a_sidecar_bound_to_its_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "price-history.csv", self.price_history_csv())

            code, _, stderr = self.normalize(workspace)
            sidecar = self.sidecar_path(workspace)
            data = sidecar.read_bytes() if sidecar.is_file() else None
            frontmatter = self.frontmatter(workspace)
            declared = CONTRACT.expected_structured_path(
                workspace / "sources" / "normalized", self.table_record(workspace)["id"]
            )
            violations = self.contract_violations(workspace)

        self.assertEqual(0, code, stderr)
        self.assertIsNotNone(data, f"no sidecar was written for a clean CSV: {stderr}")
        document = json.loads(data)
        self.assertEqual(["date", "price", "seller"], document["columns"])
        self.assertEqual(self.PRICE_ROWS, len(document["rows"]))
        self.assertEqual({"date": "2026-08-01", "price": "20.00", "seller": "Seller 0"}, document["rows"][0])
        # The record binds these exact bytes, so the writer's serialization is the one a
        # reader must be able to reproduce.
        self.assertEqual(NORMALIZE.render_structured_view(document), data)
        self.assertEqual(declared.name, Path(frontmatter["structured_view"]["path"]).name)
        self.assertFalse(Path(frontmatter["structured_view"]["path"]).is_absolute())
        self.assertEqual(
            STRUCTURED_VIEW.content_hash(data),
            frontmatter["structured_view"]["content_hash"],
        )
        self.assertEqual([], violations, [violation.message for violation in violations])

    def test_an_anchor_resolves_to_a_row_past_the_sample_cap(self):
        """The payoff: a row the rendered body never shows is still citable.

        Row 41 is past `TABLE_SAMPLE_ROWS`, so no quote could ever reach it — the body
        contains 20 sample rows and a count. The anchor addresses it directly.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "price-history.csv", self.price_history_csv())
            self.normalize(workspace)

            frontmatter = self.frontmatter(workspace)
            sidecar = self.sidecar_path(workspace)
            body = self.record_path(workspace).read_text()
            expected_price = f"{20 + self.ANCHOR_ROW / 100:.2f}"
            hit = STRUCTURED_VIEW.resolve_anchor(
                frontmatter, sidecar, f"rows/{self.ANCHOR_ROW}/price", expected_price
            )
            miss = STRUCTURED_VIEW.resolve_anchor(
                frontmatter, sidecar, f"rows/{self.ANCHOR_ROW}/price", "999.99"
            )

        self.assertNotIn(f"Seller {self.ANCHOR_ROW} ", body)
        self.assertNotIn(f"| Seller {self.ANCHOR_ROW} |", body)
        self.assertTrue(hit.ok, hit.detail)
        self.assertFalse(miss.ok)
        self.assertEqual(STRUCTURED_VIEW.RESULT_ANCHOR_VALUE_MISMATCH, miss.result)

    def test_cell_values_are_never_type_coerced(self):
        """The property canonical equality depends on.

        CSV is untyped, so the sidecar renders what the file literally holds. Inferring
        would turn `007` into the number 7 and `true` into a boolean, and canonical
        equality would then compare an anchor's `expected` against a value the file does
        not contain — `"007"` would stop matching, and `expected: "1"` would start
        matching a cell that reads `true`.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(
                workspace,
                "typed-looking.csv",
                "sku,flag,count,empty,scientific\n"
                "007,true,3,,1e3\n"
                "080,false,0,,NaN\n",
            )
            self.normalize(workspace)
            document = json.loads(self.sidecar_path(workspace).read_bytes())
            frontmatter = self.frontmatter(workspace)
            sidecar = self.sidecar_path(workspace)
            sku = STRUCTURED_VIEW.resolve_anchor(frontmatter, sidecar, "rows/0/sku", "007")

        self.assertEqual(
            {"sku": "007", "flag": "true", "count": "3", "empty": "", "scientific": "1e3"},
            document["rows"][0],
        )
        self.assertEqual(
            {"sku": "080", "flag": "false", "count": "0", "empty": "", "scientific": "NaN"},
            document["rows"][1],
        )
        for row in document["rows"]:
            for value in row.values():
                self.assertIsInstance(value, str)
        self.assertTrue(sku.ok, sku.detail)

    def test_sidecar_cells_are_not_capped_the_way_rendered_cells_are(self):
        """The body is capped, the sidecar is not — that is the whole point.

        `TABLE_MAX_CELL_CHARS` lives in `escape_table_cell`, a render-time concession to
        a readable Markdown table. Applying it here would let an anchor's `expected`
        match an ellipsis of the evidence rather than the evidence.
        """
        long_cell = "L" + "o" * (NORMALIZE.TABLE_MAX_CELL_CHARS * 2) + "ng"
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "wide-cells.csv", f"note,price\n{long_cell},1.00\n")
            self.normalize(workspace)
            document = json.loads(self.sidecar_path(workspace).read_bytes())
            body = self.record_path(workspace).read_text()

        self.assertEqual(long_cell, document["rows"][0]["note"])
        self.assertGreater(len(long_cell), NORMALIZE.TABLE_MAX_CELL_CHARS)
        self.assertNotIn(long_cell, body)
        self.assertIn("…", body)

    def test_the_tsv_delimiter_path_emits_the_same_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(
                workspace,
                "quarters.tsv",
                "quarter\trevenue\tregion\n"
                "2026Q1\t18400\tEMEA\n"
                "2026Q2\t19750\tAPAC\n",
            )
            self.normalize(workspace)
            document = json.loads(self.sidecar_path(workspace).read_bytes())
            body = self.record_path(workspace).read_text()
            frontmatter = self.frontmatter(workspace)
            anchor = STRUCTURED_VIEW.resolve_anchor(
                frontmatter, self.sidecar_path(workspace), "rows/1/revenue", "19750"
            )

        self.assertIn("Delimiter: tab", body)
        self.assertEqual(["quarter", "revenue", "region"], document["columns"])
        self.assertEqual({"quarter": "2026Q2", "revenue": "19750", "region": "APAC"}, document["rows"][1])
        self.assertTrue(anchor.ok, anchor.detail)

    def test_a_ragged_table_emits_no_sidecar_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(
                workspace,
                "ragged.csv",
                "col_a,col_b,col_c\n1,2,3\n4,5\n6,7,8,9\n",
            )
            self.assert_no_sidecar(workspace, "2 row(s) do not match the 3-column header")

    def test_a_duplicate_header_emits_no_sidecar_and_says_why(self):
        # `rows/0/price` would be ambiguous: the second column would silently win, and
        # the anchor would cite a value no reader can locate in the file.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "twice-priced.csv", "date,price,price\nx,1.00,2.00\n")
            self.assert_no_sidecar(workspace, "the header repeats column name(s): price")

    def test_an_empty_header_cell_emits_no_sidecar_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "unnamed-column.csv", "date, ,price\nx,y,1.00\n")
            self.assert_no_sidecar(workspace, "the header has an empty column name")

    def test_a_truncated_read_emits_no_sidecar_and_says_why(self):
        """The honest-caps argument: the sidecar inherits the 5 MB read ceiling.

        Every row past the ceiling is invisible to the parse, so a sidecar built from
        what was read would look complete and number its rows as if nothing were
        missing. Refusing is how the cap stays honest instead of silently partial.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            row = "value-a,value-b,value-c\n"
            rows_needed = NORMALIZE.TABLE_MAX_BYTES // len(row) + 10
            with (workspace / "raw" / "data" / "huge.csv").open("w") as handle:
                handle.write("col_one,col_two,col_three\n")
                for _ in range(rows_needed):
                    handle.write(row)

            self.assert_no_sidecar(workspace, "read ceiling")

    def test_the_emission_decision_refuses_by_default(self):
        """No header seen is a refusal, not an empty table.

        The parse cannot normally end without a header, but the guarantee has to hold on
        the paths nobody reached: a decision that defaults to "emit" would answer a
        pointer with `columns: []` for a file whose columns were simply never read.
        Checked directly because reaching this state through a CSV is the hard part.
        """
        no_header = NORMALIZE.table_structured_skip_reason([], NORMALIZE.table_header_problem([]), False, 0)
        good_header = NORMALIZE.table_structured_skip_reason(
            ["date", "price"], NORMALIZE.table_header_problem(["date", "price"]), False, 0
        )

        self.assertIsNotNone(no_header)
        self.assertIn("no header row", no_header)
        self.assertIsNone(good_header)
        # Truncation outranks a good header: the ceiling is inherited, not escapable.
        self.assertIn(
            "read ceiling",
            NORMALIZE.table_structured_skip_reason(["date"], None, True, 0),
        )

    def test_a_leading_blank_line_does_not_disqualify_the_real_header(self):
        """The header branch runs twice; a verdict from the first must not carry over.

        A stale refusal here would not skip the sidecar — `table_structured_skip_reason`
        would still see the real header and emit — it would emit `rows: []` for a table
        that has rows. That is the half-truthful sidecar the whole unit refuses.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "leading-blank.csv", "\ndate,price\n2026-08-01,23.99\n")
            self.normalize(workspace)
            document = json.loads(self.sidecar_path(workspace).read_bytes())

        self.assertEqual(["date", "price"], document["columns"])
        self.assertEqual([{"date": "2026-08-01", "price": "23.99"}], document["rows"])

    def test_a_changed_raw_fingerprint_rewrites_the_sidecar_and_its_digest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "price-history.csv", "date,price\n2026-08-01,23.99\n")
            self.normalize(workspace)
            before = self.frontmatter(workspace)["structured_view"]["content_hash"]

            self.deliver(workspace, "price-history.csv", "date,price\n2026-08-01,24.99\n")
            code, _, stderr = self.normalize(workspace)

            after = self.frontmatter(workspace)["structured_view"]["content_hash"]
            data = self.sidecar_path(workspace).read_bytes()
            document = json.loads(data)
            violations = self.contract_violations(workspace)

        self.assertEqual(0, code, stderr)
        self.assertNotEqual(before, after)
        self.assertEqual("24.99", document["rows"][0]["price"])
        self.assertEqual(STRUCTURED_VIEW.content_hash(data), after)
        self.assertEqual([], violations, [violation.message for violation in violations])

    def test_an_unchanged_table_reproduces_the_same_sidecar_bytes(self):
        # The binding is by digest, so a serialization that varied between runs would
        # invalidate a record nothing had a reason to rewrite.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "price-history.csv", self.price_history_csv(rows=5))
            self.normalize(workspace)
            first = self.sidecar_path(workspace).read_bytes()
            self.normalize(workspace, "--force")
            second = self.sidecar_path(workspace).read_bytes()
            digest = self.frontmatter(workspace)["structured_view"]["content_hash"]

        self.assertEqual(first, second)
        self.assertEqual(STRUCTURED_VIEW.content_hash(second), digest)

    def test_a_table_that_stops_qualifying_loses_its_sidecar(self):
        """The removal direction, which would otherwise leave an orphan nothing collects.

        A sidecar no record declares is a contract violation and a file every consumer
        here is blind to — they all glob `*.md`. When the source changes into something
        unaddressable, the record losing its binding has to take the file with it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "price-history.csv", "date,price\n2026-08-01,23.99\n")
            self.normalize(workspace)
            self.assertTrue(self.sidecar_path(workspace).is_file())

            self.deliver(workspace, "price-history.csv", "date,price\n2026-08-01,23.99,extra\n")
            code, _, stderr = self.normalize(workspace)

            sidecar_exists = self.sidecar_path(workspace).exists()
            frontmatter = self.frontmatter(workspace)
            warnings = self.skip_warnings(frontmatter)
            violations = self.contract_violations(workspace)

        self.assertEqual(0, code, stderr)
        self.assertFalse(sidecar_exists, "a stale sidecar outlived the binding that declared it")
        self.assertIsNone(frontmatter["structured_view"])
        self.assertEqual(1, len(warnings), frontmatter.get("parse_warnings"))
        self.assertEqual([], violations, [violation.message for violation in violations])

    def test_a_record_written_before_sidecars_regenerates_to_gain_one(self):
        """`missing_structured_view_key` already covers `table_text`.

        A tabular record from before the field existed would otherwise stay
        `skipped_existing` forever and never gain the view an anchor resolves against.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.deliver(workspace, "price-history.csv", "date,price\n2026-08-01,23.99\n")
            self.normalize(workspace)

            # Rewind the record to its pre-CR-7 shape: no key, no sidecar.
            record_path = self.record_path(workspace)
            record_path.write_text(
                "\n".join(
                    line
                    for line in record_path.read_text().split("\n")
                    if not line.startswith("structured_view:")
                )
            )
            self.sidecar_path(workspace).unlink()
            self.assertNotIn("structured_view", self.frontmatter(workspace))

            code, _, stderr = self.normalize(workspace)
            sidecar = self.sidecar_path(workspace)
            data = sidecar.read_bytes() if sidecar.is_file() else None
            frontmatter = self.frontmatter(workspace)

        self.assertEqual(0, code, stderr)
        self.assertIsNotNone(data, f"regeneration did not gain the structured view: {stderr}")
        self.assertEqual(STRUCTURED_VIEW.content_hash(data), frontmatter["structured_view"]["content_hash"])


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
