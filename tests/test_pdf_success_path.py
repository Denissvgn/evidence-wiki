import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
PDF_EXTRACTION_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pdf-extraction"
REQUIRED_PDF_EXTRACTION_FIXTURE_IDS = ("1909.13231v3", "2010.04003v2", "2212.07677v2", "2402.02750v2")


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = load_script_module("pdf_success_inventory", "source_inventory.py")
NORMALIZE = load_script_module("pdf_success_normalize", "normalize_sources.py")
LINT = load_script_module("pdf_success_lint", "lint.py")


def tiny_pdf_bytes() -> bytes:
    stream = (
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
    ).encode("ascii")
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


class PdfSuccessPathTests(unittest.TestCase):
    def test_pypdf_is_the_deterministic_default_even_when_poppler_is_available(self):
        with mock.patch.object(NORMALIZE.shutil, "which", return_value="/opt/homebrew/bin/pdftotext"):
            selected = NORMALIZE.pdf_extractor_name({"sources": {}})

        self.assertEqual("pypdf", selected)

    def test_poppler_requires_explicit_config_or_cli_selection(self):
        self.assertEqual(
            "poppler",
            NORMALIZE.pdf_extractor_name({"sources": {"pdf_extractor": "poppler"}}),
        )
        self.assertEqual(
            "pypdf",
            NORMALIZE.pdf_extractor_name(
                {"sources": {"pdf_extractor": "poppler"}},
                "pypdf",
            ),
        )
        args = NORMALIZE.parse_args(["--pdf-extractor", "poppler"])
        self.assertEqual("poppler", args.pdf_extractor)

    def test_pypdf_extractor_uses_isolated_bounded_child_process(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "paper.pdf"
            pdf.write_bytes(tiny_pdf_bytes())
            payload = {
                "reading_text": "Reading order.\f",
                "layout_text": "Layout order.\f",
                "page_count": 1,
                "backend": "pypdf",
                "backend_version": "6.14.2",
            }
            completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            with mock.patch.object(NORMALIZE.subprocess, "run", return_value=completed) as run:
                result = NORMALIZE.run_pypdf_extractor(
                    pdf,
                    "raw/pdf/paper.pdf",
                    NORMALIZE.PdfExtractor("pypdf", "6.14.2"),
                )

        argv = run.call_args.args[0]
        self.assertEqual(sys.executable, argv[0])
        self.assertEqual(["-I", "-B", "-c"], argv[1:4])
        self.assertEqual(str(NORMALIZE.PDF_MAX_PAGES), argv[-2])
        self.assertEqual(str(NORMALIZE.PDF_MAX_OUTPUT_CHARS), argv[-1])
        self.assertEqual(NORMALIZE.PDF_EXTRACTION_TIMEOUT_SECONDS, run.call_args.kwargs["timeout"])
        self.assertEqual("Reading order.\f", result.reading_text)
        self.assertEqual("Layout order.\f", result.layout_text)
        self.assertEqual(1, result.page_count)
        self.assertEqual("pypdf", result.backend)
        self.assertEqual("6.14.2", result.backend_version)
        self.assertTrue(result.extractor_ran)

    def test_real_pypdf_extractor_normalizes_tiny_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pdf = workspace / "raw" / "pdf" / "tiny.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(tiny_pdf_bytes())
            extractor = NORMALIZE.resolve_pdf_extractor("pypdf")
            result = NORMALIZE.run_pypdf_extractor(pdf, "raw/pdf/tiny.pdf", extractor)
            record = {
                "id": "raw:tiny-pdf",
                "kind": "pdf",
                "raw_paths": ["raw/pdf/tiny.pdf"],
                "raw_pdf": "raw/pdf/tiny.pdf",
            }
            normalized = NORMALIZE.normalize_pdf_record(workspace, record, extractor)

        self.assertTrue(result.extractor_ran, result.warnings)
        self.assertEqual(1, result.page_count)
        self.assertEqual("pypdf", result.backend)
        self.assertEqual(extractor.version, result.backend_version)
        self.assertIn("A Tiny PDF Fixture For Normalization", result.reading_text)
        self.assertEqual("A Tiny PDF Fixture For Normalization", normalized.title)
        self.assertIn("PDF-only fixture exercises pdftotext", normalized.extracted_text)
        self.assertEqual("pypdf", normalized.pdf_extractor)

    def test_pypdf_extractor_rejects_oversized_input_before_launch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            with (
                mock.patch.object(NORMALIZE, "PDF_MAX_INPUT_BYTES", 3),
                mock.patch.object(NORMALIZE.subprocess, "run") as run,
            ):
                result = NORMALIZE.run_pypdf_extractor(
                    pdf,
                    "raw/pdf/paper.pdf",
                    NORMALIZE.PdfExtractor("pypdf", "6.14.2"),
                )

        run.assert_not_called()
        self.assertFalse(result.extractor_ran)
        self.assertTrue(any("exceeds extraction limit" in warning for warning in result.warnings))

    def test_pypdf_extractor_rejects_oversized_child_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf = Path(tmpdir) / "paper.pdf"
            pdf.write_bytes(b"%PDF")
            payload = {
                "reading_text": "too long",
                "layout_text": "layout",
                "page_count": 1,
                "backend": "pypdf",
                "backend_version": "6.14.2",
            }
            completed = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
            with (
                mock.patch.object(NORMALIZE, "PDF_MAX_OUTPUT_CHARS", 4),
                mock.patch.object(NORMALIZE.subprocess, "run", return_value=completed),
            ):
                result = NORMALIZE.run_pypdf_extractor(
                    pdf,
                    "raw/pdf/paper.pdf",
                    NORMALIZE.PdfExtractor("pypdf", "6.14.2"),
                )

        self.assertFalse(result.extractor_ran)
        self.assertTrue(any("invalid or oversized result" in warning for warning in result.warnings))

    def test_pdf_frontmatter_records_extractor_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pdf = workspace / "raw" / "papers" / "manual.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\n")
            record = {
                "id": "raw:manual-pdf",
                "kind": "pdf",
                "raw_paths": ["raw/papers/manual.pdf"],
                "raw_pdf": "raw/papers/manual.pdf",
            }
            extraction = NORMALIZE.PdfExtractionResult(
                reading_text="Manual PDF Title\nAbstract\nUseful body text.\f",
                layout_text="Manual PDF Title\nAbstract\nUseful body text.\f",
                page_count=1,
                backend="pypdf",
                backend_version="6.14.2",
                warnings=[],
                extractor_ran=True,
            )
            with mock.patch.object(NORMALIZE, "extract_pdf", return_value=extraction):
                normalized = NORMALIZE.normalize_pdf_record(
                    workspace,
                    record,
                    NORMALIZE.PdfExtractor("pypdf", "6.14.2"),
                )
            output = workspace / "sources" / "normalized" / "raw--manual-pdf.md"
            frontmatter = NORMALIZE.frontmatter_for(
                normalized,
                "sources/manifest.jsonl",
                output,
                "2026-07-21",
            )

        self.assertEqual({"name": "pypdf", "version": "6.14.2"}, frontmatter["pdf_extractor"])
        self.assertEqual(2, frontmatter["normalizer"]["version"])

    def test_failed_pypdf_extraction_writes_no_normalized_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pdf = workspace / "raw" / "pdf" / "corrupt.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\ncorrupt")
            (workspace / "sources" / "normalized").mkdir(parents=True)
            (workspace / "research.yml").write_text(
                "raw:\n"
                "  source_roots: [raw/pdf]\n"
                "sources:\n"
                "  manifest_path: sources/manifest.jsonl\n"
                "  normalized_dir: sources/normalized\n",
                encoding="utf-8",
            )
            record = {
                "id": "raw:corrupt-pdf",
                "kind": "pdf",
                "raw_paths": ["raw/pdf/corrupt.pdf"],
                "raw_pdf": "raw/pdf/corrupt.pdf",
                "pairing_status": "pdf_only",
                "status": "discovered",
            }
            (workspace / "sources" / "manifest.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            failure = NORMALIZE.PdfExtractionResult(
                reading_text="",
                layout_text="",
                page_count=0,
                backend="pypdf",
                backend_version="6.14.2",
                warnings=["raw/pdf/corrupt.pdf: pypdf extraction failed: PdfReadError"],
                extractor_ran=False,
            )
            args = NORMALIZE.parse_args(
                [
                    "--project-root",
                    str(workspace),
                    "--source-id",
                    "raw:corrupt-pdf",
                    "--format",
                    "json",
                ]
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    NORMALIZE,
                    "resolve_pdf_extractor",
                    return_value=NORMALIZE.PdfExtractor("pypdf", "6.14.2"),
                ),
                mock.patch.object(NORMALIZE, "run_pypdf_extractor", return_value=failure),
                contextlib.redirect_stdout(stdout),
            ):
                code = NORMALIZE.run_normalization(args)
            report = json.loads(stdout.getvalue())
            output = NORMALIZE.normalized_output_path_for_record(
                record,
                workspace / "sources" / "normalized",
            )
            output_exists = output.exists()

        self.assertEqual(1, code)
        self.assertEqual(1, report["summary"]["failed"])
        self.assertEqual("failed", report["actions"][0]["status"])
        self.assertIn("PDF extraction failed using pypdf", report["actions"][0]["error"])
        self.assertEqual(
            {"name": "pypdf", "version": "6.14.2"},
            report["actions"][0]["pdf_extractor"],
        )
        self.assertFalse(output_exists)

    def test_required_pdf_extraction_fixtures_are_committed(self):
        for arxiv_id in REQUIRED_PDF_EXTRACTION_FIXTURE_IDS:
            with self.subTest(arxiv_id=arxiv_id):
                reading_order = PDF_EXTRACTION_FIXTURES / arxiv_id / "reading-order.txt"
                layout = PDF_EXTRACTION_FIXTURES / arxiv_id / "layout.txt"
                self.assertTrue(reading_order.is_file(), f"Missing reading-order fixture for {arxiv_id}")
                self.assertTrue(layout.is_file(), f"Missing layout fixture for {arxiv_id}")
                self.assertGreater(reading_order.stat().st_size, 500)
                self.assertGreater(layout.stat().st_size, 500)

    def test_pdf_title_inference_stops_before_spaced_superscript_author_line(self):
        title, confidence = NORMALIZE.infer_pdf_title(
            "Test-Time Training with Self-Supervision for Generalization under Distribution Shifts\n"
            "Yu Sun 1 Xiaolong Wang 1 2 Zhuang Liu 1\n"
            "Abstract\n"
            "Our proposed method creates a self-supervised signal.\n",
            "paper:1909.13231v3",
        )

        self.assertEqual("Test-Time Training with Self-Supervision for Generalization under Distribution Shifts", title)
        self.assertNotIn("Yu Sun", title)
        self.assertNotIn("Abstract", title)
        self.assertEqual("high", confidence)

    def test_pdf_title_inference_truncates_inline_abstract_leakage(self):
        title, confidence = NORMALIZE.infer_pdf_title(
            "Retrieval-Augmented Evaluation for Agents Abstract This paper studies evaluation leakage.\n"
            "1 Introduction\n",
            "paper:2407.04620v4",
        )

        self.assertEqual("Retrieval-Augmented Evaluation for Agents", title)
        self.assertNotIn("Abstract", title)
        self.assertEqual("low", confidence)

    def test_pdf_title_inference_stops_when_abstract_heading_shares_its_line_with_body(self):
        title, confidence = NORMALIZE.infer_pdf_title(
            "A Tiny PDF Fixture For Normalization\n"
            "Abstract This PDF-only fixture exercises extraction.\n"
            "1 Introduction The body follows.\n",
            "paper:tiny-fixture",
        )

        self.assertEqual("A Tiny PDF Fixture For Normalization", title)
        self.assertEqual("high", confidence)

    def test_pdf_text_hygiene_strips_arxiv_watermark_and_joins_hyphen_breaks(self):
        text = NORMALIZE.normalize_pdf_text(
            "A method for self-\n"
            "supervised adaptation.\n"
            "arXiv:1909.13231v3 [cs.LG] 29 Sep 2019\n"
            "The next sentence remains.\n"
        )

        self.assertIn("selfsupervised adaptation", text)
        self.assertNotIn("arXiv:1909.13231v3", text)
        self.assertIn("The next sentence remains.", text)

    def test_pdf_abstract_fallback_handles_reordered_icml_abstract_box(self):
        abstract = NORMALIZE.extract_pdf_abstract(
            "KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache\n"
            "Abstract\n"
            "1. Introduction\n"
            "KIVI is a tuning-free asymmetric quantization method for KV cache. "
            "It preserves long-context serving quality while reducing memory pressure.\n"
            "2. Background\n"
        )

        self.assertEqual(
            "KIVI is a tuning-free asymmetric quantization method for KV cache. "
            "It preserves long-context serving quality while reducing memory pressure.",
            abstract,
        )

    def test_pdf_title_inference_collapses_letter_spaced_small_caps(self):
        title, confidence = NORMALIZE.infer_pdf_title(
            "GPTQ: ACCURATE P OST-T RAINING QUANTIZATION FOR GENERATIVE PRE-TRAINED TRANSFORMERS\n"
            "Abstract\n"
            "We study quantization.\n",
            "paper:2210.17323v2",
        )

        self.assertEqual(
            "GPTQ: ACCURATE POST-TRAINING QUANTIZATION FOR GENERATIVE PRE-TRAINED TRANSFORMERS",
            title,
        )
        self.assertEqual("low", confidence)

    def test_pdf_title_inference_does_not_collapse_ordinary_all_caps_title(self):
        title, confidence = NORMALIZE.infer_pdf_title(
            "A NEW FRAMEWORK FOR ROBUST LEARNING\n"
            "Abstract\n"
            "We study robust learning.\n",
            "paper:ordinary-all-caps",
        )

        self.assertEqual("A NEW FRAMEWORK FOR ROBUST LEARNING", title)
        self.assertEqual("high", confidence)

    def test_pdf_title_inference_ignores_incidental_single_letter_words(self):
        title, confidence = NORMALIZE.infer_pdf_title(
            "A SIMPLE TUTORIAL FOR Q LEARNING\n"
            "Abstract\n"
            "We study reinforcement learning.\n",
            "paper:ordinary-q-learning",
        )

        self.assertEqual("A SIMPLE TUTORIAL FOR Q LEARNING", title)
        self.assertEqual("high", confidence)

    def test_pdf_abstract_recovery_fallback_sets_low_confidence_and_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pdf = workspace / "raw" / "papers" / "fallback.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\n")
            record = {
                "id": "paper:fallback",
                "kind": "pdf",
                "raw_paths": ["raw/papers/fallback.pdf"],
                "raw_pdf": "raw/papers/fallback.pdf",
                "status": "pending",
                "provenance": {"retrieved_by": "fetch-agent/manual"},
            }
            original_extract = NORMALIZE.extract_pdf_text

            def fake_extract(_pdftotext_path, _pdf_path, _pdf_label):
                return (
                    "KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache\n"
                    "Abstract\n"
                    "1. Introduction\n"
                    "KIVI is a tuning-free asymmetric quantization method for KV cache. "
                    "It preserves long-context serving quality while reducing memory pressure.\n"
                    "2. Background\n",
                    [],
                    True,
                )

            NORMALIZE.extract_pdf_text = fake_extract
            try:
                normalized = NORMALIZE.normalize_pdf_record(workspace, record, "pdftotext")
            finally:
                NORMALIZE.extract_pdf_text = original_extract

        self.assertEqual("low", normalized.abstract_confidence)
        self.assertTrue(any("abstract recovery fallback" in warning for warning in normalized.warnings))
        self.assertEqual(
            "KIVI is a tuning-free asymmetric quantization method for KV cache. "
            "It preserves long-context serving quality while reducing memory pressure.",
            normalized.abstract,
        )

    def test_pdf_abstract_normal_path_keeps_high_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pdf = workspace / "raw" / "papers" / "normal.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\n")
            record = {
                "id": "paper:normal",
                "kind": "pdf",
                "raw_paths": ["raw/papers/normal.pdf"],
                "raw_pdf": "raw/papers/normal.pdf",
                "status": "pending",
                "provenance": {"retrieved_by": "fetch-agent/manual"},
            }
            original_extract = NORMALIZE.extract_pdf_text

            def fake_extract(_pdftotext_path, _pdf_path, _pdf_label):
                return (
                    "Normal Abstract Paper\n"
                    "Abstract\n"
                    "This paper has a normal abstract before the introduction.\n"
                    "1 Introduction\n"
                    "Body text.\n",
                    [],
                    True,
                )

            NORMALIZE.extract_pdf_text = fake_extract
            try:
                normalized = NORMALIZE.normalize_pdf_record(workspace, record, "pdftotext")
                frontmatter = NORMALIZE.frontmatter_for(
                    normalized,
                    "sources/manifest.jsonl",
                    workspace / "sources" / "normalized" / "paper--normal.md",
                    "2026-07-09",
                )
            finally:
                NORMALIZE.extract_pdf_text = original_extract

        self.assertEqual("high", normalized.abstract_confidence)
        self.assertEqual("high", frontmatter["abstract_confidence"])
        self.assertFalse(any("abstract recovery fallback" in warning for warning in normalized.warnings))

    def test_clean_single_column_pdf_fixtures_normalize_without_regression(self):
        cases = {
            "2407.04620v4": {
                "title": "Learning to (Learn at Test Time): RNNs with Expressive Hidden States",
                "authors": ["Yu Sun", "Xinhao Li", "Karan Dalal"],
                "abstract_phrase": "wholly synthetic fixture models long-context sequence models",
            },
            "2504.19874v1": {
                "title": "TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
                "authors": ["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
                "abstract_phrase": "wholly synthetic fixture models vector-quantization parser behavior",
            },
        }
        for arxiv_id, expected in cases.items():
            with self.subTest(arxiv_id=arxiv_id):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace = Path(tmpdir)
                    raw_pdf = f"raw/papers/{arxiv_id}.pdf"
                    pdf = workspace / raw_pdf
                    pdf.parent.mkdir(parents=True)
                    pdf.write_bytes(b"%PDF-1.4\n")
                    record = {
                        "id": f"paper:{arxiv_id}",
                        "kind": "paper",
                        "raw_paths": [raw_pdf],
                        "raw_pdf": raw_pdf,
                        "status": "pending",
                        "provenance": {
                            "retrieved_by": "fetch_sources.py/arxiv",
                            "title": expected["title"],
                            "authors": expected["authors"],
                            "arxiv_id": arxiv_id,
                            "publication_year": 2025,
                        },
                    }
                    original_extract = NORMALIZE.extract_pdf_text
                    original_cache = getattr(NORMALIZE, "PDF_LAYOUT_TEXT_CACHE", {}).copy()

                    def fake_extract(_pdftotext_path, _pdf_path, pdf_label, _arxiv_id=arxiv_id):
                        reading_order = PDF_EXTRACTION_FIXTURES / _arxiv_id / "reading-order.txt"
                        layout = PDF_EXTRACTION_FIXTURES / _arxiv_id / "layout.txt"
                        NORMALIZE.PDF_LAYOUT_TEXT_CACHE[pdf_label] = layout.read_text(encoding="utf-8")
                        return reading_order.read_text(encoding="utf-8"), [], True

                    NORMALIZE.extract_pdf_text = fake_extract
                    try:
                        normalized = NORMALIZE.normalize_pdf_record(workspace, record, "pdftotext")
                    finally:
                        NORMALIZE.extract_pdf_text = original_extract
                        NORMALIZE.PDF_LAYOUT_TEXT_CACHE.clear()
                        NORMALIZE.PDF_LAYOUT_TEXT_CACHE.update(original_cache)

                self.assertEqual(expected["title"], normalized.title)
                self.assertEqual("provider", normalized.title_source)
                self.assertIn(expected["abstract_phrase"], normalized.abstract)
                self.assertNotIn("abstract not extracted", "\n".join(normalized.warnings))
                self.assertGreater(len(normalized.extracted_text), 1000)

    def test_pdf_normalization_uses_layout_text_only_for_media_extraction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pdf = workspace / "raw" / "papers" / "manual.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\n")
            record = {
                "id": "raw:manual-pdf",
                "kind": "pdf",
                "raw_paths": ["raw/papers/manual.pdf"],
                "raw_pdf": "raw/papers/manual.pdf",
                "status": "pending",
                "provenance": {"retrieved_by": "fetch-agent/manual"},
            }
            original_extract = NORMALIZE.extract_pdf_text
            original_cache = getattr(NORMALIZE, "PDF_LAYOUT_TEXT_CACHE", {}).copy()

            def fake_extract(_pdftotext_path, _pdf_path, pdf_label):
                NORMALIZE.PDF_LAYOUT_TEXT_CACHE[pdf_label] = "Figure 1: Layout-only caption survives table pass."
                return (
                    "Manual PDF Title\n"
                    "Abstract\n"
                    "This reading-order prose should become the extracted text.\n"
                    "1 Introduction\n"
                    "Body text.\n",
                    [],
                    True,
                )

            NORMALIZE.extract_pdf_text = fake_extract
            try:
                normalized = NORMALIZE.normalize_pdf_record(workspace, record, "pdftotext")
            finally:
                NORMALIZE.extract_pdf_text = original_extract
                NORMALIZE.PDF_LAYOUT_TEXT_CACHE.clear()
                NORMALIZE.PDF_LAYOUT_TEXT_CACHE.update(original_cache)

        self.assertIn("reading-order prose", normalized.extracted_text)
        self.assertEqual("Layout-only caption survives table pass.", normalized.media[0].caption)

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext is required for the PDF success path")
    def test_pdf_only_source_inventory_normalization_and_lint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "workspace"
            (root / "raw" / "pdf").mkdir(parents=True)
            (root / "sources" / "normalized").mkdir(parents=True)
            (root / "wiki" / "sources").mkdir(parents=True)
            (root / "research.yml").write_text(
                "project:\n"
                "  name: pdf-success\n"
                "  description: PDF success fixture.\n"
                "raw:\n"
                "  source_roots:\n"
                "    - raw/pdf\n"
                "sources:\n"
                "  manifest_path: sources/manifest.jsonl\n"
                "  normalized_dir: sources/normalized\n"
                "  default_status: discovered\n"
                "  lifecycle_statuses:\n"
                "    - discovered\n"
                "    - normalized\n"
                "    - noted\n"
                "    - integrated\n"
                "    - deferred\n"
                "    - superseded\n"
                "    - rejected\n"
                "wiki:\n"
                "  root: wiki\n"
                "  required_dirs: []\n"
                "  allowed_page_types:\n"
                "    - source\n"
                "  frontmatter_required: []\n"
                "lint:\n"
                "  validate_structure: false\n"
                "  validate_frontmatter: false\n"
                "  validate_links: false\n"
                "  validate_source_coverage: true\n"
                "  validate_claims: false\n"
            )
            (root / "raw" / "pdf" / "tiny-fixture.pdf").write_bytes(tiny_pdf_bytes())

            config = INVENTORY.load_config(root)
            records, warnings, summary = INVENTORY.build_records(root, config, previous_detected_at={})
            pdf_record = next(record for record in records if record.get("kind") == "pdf")
            manifest_path = root / "sources" / "manifest.jsonl"
            INVENTORY.write_manifest(manifest_path, records)

            eligible = NORMALIZE.eligible_records(root, records)
            pdf_item = next(item for item in eligible if item.record == pdf_record)
            normalized = NORMALIZE.normalize_selected_record(root, config, pdf_item, shutil.which("pdftotext"))
            output_path = NORMALIZE.normalized_output_path_for_record(pdf_record, root / "sources" / "normalized")
            frontmatter = NORMALIZE.frontmatter_for(normalized, "sources/manifest.jsonl", output_path, "2026-05-31")
            output_path.write_text(NORMALIZE.render_markdown(normalized, frontmatter))

            lint_results = LINT.run_checks(root, LINT.load_config(root))

        self.assertEqual("pdf_only", pdf_record["pairing_status"])
        self.assertEqual({"paired": 0, "pdf_only": 1, "latex_only": 0, "ambiguous": 0}, summary)
        self.assertTrue(any("no matching LaTeX source bundle" in warning for warning in warnings))
        self.assertEqual("pdf", pdf_item.method)
        self.assertEqual("pdf_text", normalized.extraction_method)
        self.assertEqual("A Tiny PDF Fixture For Normalization", normalized.title)
        self.assertEqual("high", normalized.title_confidence)
        self.assertIn("PDF-only fixture exercises pdftotext", normalized.extracted_text)
        self.assertTrue(frontmatter["content_hash"].startswith("sha256:"))
        self.assertEqual([], [issue for issue in lint_results["issues"] if issue["severity"] == "HIGH"])
        self.assertNotIn("pdf_extraction_failed", {issue["category"] for issue in lint_results["issues"]})
        self.assertNotIn("pdf_title_uncertain", {issue["category"] for issue in lint_results["issues"]})
        self.assertEqual(
            "normalized",
            next(row["effective_status"] for row in lint_results["source_coverage"] if row["source_id"] == pdf_record["id"]),
        )


if __name__ == "__main__":
    unittest.main()
