import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "arxiv-source-project"
CODEBASE_FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "codebase-analysis-project"
INVENTORY_PATH = REPO_ROOT / "workspace-template" / "scripts" / "source_inventory.py"
NORMALIZE_PATH = REPO_ROOT / "workspace-template" / "scripts" / "normalize_sources.py"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = load_script_module("research_source_inventory", INVENTORY_PATH)
NORMALIZE = load_script_module("research_normalize_sources", NORMALIZE_PATH)


def records_by_id(records: list[dict]) -> dict[str, dict]:
    return {record["id"]: record for record in records}


def records_by_kind(records: list[dict], kind: str) -> list[dict]:
    return [record for record in records if record.get("kind") == kind]


class InventoryNormalizationTests(unittest.TestCase):
    def build_inventory_records(self) -> tuple[list[dict], list[str], dict[str, int]]:
        config = INVENTORY.load_config(FIXTURE_ROOT)
        return INVENTORY.build_records(FIXTURE_ROOT, config, previous_detected_at={})

    def build_codebase_records(self, project_root: Path = CODEBASE_FIXTURE_ROOT) -> tuple[list[dict], dict]:
        config = INVENTORY.load_config(project_root)
        records, _, _ = INVENTORY.build_records(project_root, config, previous_detected_at={})
        return records, config

    def test_inventory_detects_arxiv_bundle(self):
        records, _, _ = self.build_inventory_records()
        paper = records_by_id(records)["paper:2601.00001v1"]

        self.assertEqual("paper", paper["kind"])
        self.assertEqual("raw/other/arXiv-2601.00001v1", paper["latex_root"])
        self.assertEqual("arxiv", paper["metadata"]["bundle_type"])
        self.assertEqual("2601.00001v1", paper["metadata"]["arxiv_id"])

    def test_inventory_uses_readme_entrypoint_metadata(self):
        records, _, _ = self.build_inventory_records()
        paper = records_by_id(records)["paper:2601.00001v1"]

        self.assertEqual("main.tex", paper["entrypoint"])
        self.assertEqual("pdflatex", paper["compiler"])
        self.assertEqual("readme", paper["metadata"]["entrypoint_source"])
        self.assertEqual(["main.tex"], paper["metadata"]["entrypoint_candidates"])

    def test_inventory_pairs_pdf_with_arxiv_bundle(self):
        records, _, summary = self.build_inventory_records()
        paper = records_by_id(records)["paper:2601.00001v1"]

        self.assertEqual("paired", paper["pairing_status"])
        self.assertEqual("raw/pdf/2601.00001v1.pdf", paper["raw_pdf"])
        self.assertIn("raw/pdf/2601.00001v1.pdf", paper["raw_paths"])
        self.assertEqual([], records_by_kind(records, "pdf"))
        self.assertEqual(
            {"paired": 1, "pdf_only": 0, "latex_only": 0, "ambiguous": 0},
            summary,
        )

    def test_inventory_parses_link_records(self):
        records, _, _ = self.build_inventory_records()
        repo_links = records_by_kind(records, "repo_link")
        web_links = records_by_kind(records, "web_link")

        self.assertEqual(1, len(repo_links))
        self.assertEqual(1, len(web_links))
        self.assertEqual("https://github.com/example/fixture-repo", repo_links[0]["url"])
        self.assertEqual("example/fixture-repo", repo_links[0]["metadata"]["repo_full_name"])
        self.assertEqual("https://example.org/fixture-project", web_links[0]["url"])
        self.assertEqual("example.org", web_links[0]["metadata"]["host"])

    def test_normalization_warns_per_source_for_unsupported_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "ws"
            shutil.copytree(FIXTURE_ROOT, target)
            (target / "sources").mkdir(exist_ok=True)
            record = {
                "id": "raw:loose-notes",
                "kind": "markdown",
                "raw_paths": ["raw/papers/loose-notes.md"],
                "status": "pending",
                "detected_at": "2026-06-09T00:00:00Z",
            }
            (target / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")

            args = NORMALIZE.parse_args(["--project-root", str(target), "--all", "--format", "json"])
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = NORMALIZE.run_normalization(args)
            report = json.loads(buffer.getvalue())

            self.assertEqual(0, code)
            self.assertEqual(1, report["summary"]["skipped_unsupported"])
            self.assertTrue(
                any("raw:loose-notes" in warning and "not a supported" in warning for warning in report["warnings"]),
                report["warnings"],
            )

    def test_normalization_eligibility_uses_latex_and_link_methods(self):
        records, _, _ = self.build_inventory_records()
        eligible = NORMALIZE.eligible_records(FIXTURE_ROOT, records)
        methods_by_id = {
            NORMALIZE.record_id(item.record): item.method
            for item in eligible
        }

        self.assertEqual("latex", methods_by_id["paper:2601.00001v1"])
        self.assertEqual("link", methods_by_id["link:github-example-fixture-repo-5f629f49ca"])
        self.assertEqual("link", methods_by_id["link:example-org-fixture-project-e51812dad4"])
        self.assertNotIn("pdf", methods_by_id.values())

    def test_latex_normalization_expands_input_file(self):
        records, _, _ = self.build_inventory_records()
        paper = records_by_id(records)["paper:2601.00001v1"]

        normalized = NORMALIZE.normalize_latex_record(FIXTURE_ROOT, paper)

        self.assertEqual("latex", normalized.extraction_method)
        self.assertEqual("Synthetic Fixture Paper", normalized.title)
        self.assertIn(
            "raw/other/arXiv-2601.00001v1/main.tex",
            normalized.included_paths,
        )
        self.assertIn(
            "raw/other/arXiv-2601.00001v1/sections/intro.tex",
            normalized.included_paths,
        )
        self.assertIn(
            "This included section should appear",
            normalized.extracted_text,
        )

    def test_link_normalization_creates_offline_stubs(self):
        records, _, _ = self.build_inventory_records()
        repo_link = records_by_kind(records, "repo_link")[0]
        web_link = records_by_kind(records, "web_link")[0]

        repo_normalized = NORMALIZE.normalize_link_record(repo_link)
        web_normalized = NORMALIZE.normalize_link_record(web_link)

        self.assertEqual("link_stub", repo_normalized.extraction_method)
        self.assertEqual(["https://github.com/example/fixture-repo"], repo_normalized.links)
        self.assertIn("Network content has not been fetched", repo_normalized.abstract)
        self.assertEqual("web_stub", web_normalized.extraction_method)
        self.assertEqual(["https://example.org/fixture-project"], web_normalized.links)
        self.assertIn("Network content has not been fetched", web_normalized.abstract)

    def test_inventory_accepts_web_transport_provenance_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "raw" / "web").mkdir(parents=True)
            (workspace / "sources").mkdir()
            (workspace / "research.yml").write_text(
                "raw:\n"
                "  source_roots:\n"
                "    - raw/web\n"
                "sources:\n"
                "  manifest_path: sources/manifest.jsonl\n",
                encoding="utf-8",
            )
            html = workspace / "raw" / "web" / "official-example.html"
            html.write_text("<html><body>Official source.</body></html>", encoding="utf-8")
            html.with_name("official-example.html.provenance.yml").write_text(
                "origin_url: https://official.example/page\n"
                "url: https://official.example/page\n"
                "final_url: https://official.example/page\n"
                "retrieved_at: 2026-07-04T12:00:00Z\n"
                "retrieved_by: fetch_sources.py/web\n"
                "license: null\n"
                "source_type: official_web\n"
                "publisher: Official Example\n"
                "supported_evidence_areas:\n"
                "  - current_legal_figure\n"
                "byte_count: 42\n"
                "content_type: text/html\n"
                "http_status: 200\n"
                "redirect_chain: []\n"
                "tls_verified: true\n"
                "checksum: sha256:0000000000000000000000000000000000000000000000000000000000000000\n",
                encoding="utf-8",
            )

            config = INVENTORY.load_config(workspace)
            records, warnings, _ = INVENTORY.build_records(workspace, config, previous_detected_at={})

        self.assertFalse(any("unknown provenance field" in warning for warning in warnings), warnings)
        html_record = records_by_kind(records, "html")[0]
        provenance = html_record["provenance"]
        self.assertEqual("fetch_sources.py/web", provenance["retrieved_by"])
        self.assertEqual(42, provenance["byte_count"])
        self.assertEqual(200, provenance["http_status"])
        self.assertTrue(provenance["tls_verified"])
        self.assertEqual(["current_legal_figure"], provenance["supported_evidence_areas"])

    def test_provenance_timestamp_is_canonical_utc_across_offset_and_dst_forms(self):
        self.assertEqual(
            "2026-11-01T05:45:00Z",
            INVENTORY.provenance_timestamp_text("2026-11-01T01:45:00-04:00"),
        )
        self.assertEqual(
            "2026-11-01T06:15:00Z",
            INVENTORY.provenance_timestamp_text("2026-11-01T01:15:00-05:00"),
        )
        self.assertEqual(
            "2025-12-31T22:30:00Z",
            INVENTORY.provenance_timestamp_text("2026-01-01T00:30:00+02:00"),
        )

    def test_inventory_preserves_standards_provenance_and_marks_malformed_for_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "raw" / "web").mkdir(parents=True)
            (workspace / "sources").mkdir()
            (workspace / "research.yml").write_text(
                "raw:\n"
                "  source_roots:\n"
                "    - raw/web\n"
                "sources:\n"
                "  manifest_path: sources/manifest.jsonl\n",
                encoding="utf-8",
            )
            good = workspace / "raw" / "web" / "iso-19131.html"
            good.write_text("<html><body>ISO registry.</body></html>", encoding="utf-8")
            good.with_name("iso-19131.html.provenance.yml").write_text(
                "origin_url: https://www.iso.org/standard/77442.html\n"
                "retrieved_at: 2026-07-07T12:00:00Z\n"
                "retrieved_by: fetch_sources.py/web\n"
                "license: null\n"
                "source_type: standards_registry_entry\n"
                "standards:\n"
                "  registry_provider: iso-open-data\n"
                "  standards_body: ISO\n"
                "  designation: ISO 19131:2022\n"
                "  title: Geographic information - Data product specifications\n"
                "  edition: 2\n"
                "  publication_date: '2022-11-01'\n"
                "  status: published\n"
                "  registry_url: https://www.iso.org/standard/77442.html\n",
                encoding="utf-8",
            )
            malformed = workspace / "raw" / "web" / "bad-standard.html"
            malformed.write_text("<html><body>Bad registry.</body></html>", encoding="utf-8")
            malformed.with_name("bad-standard.html.provenance.yml").write_text(
                "origin_url: https://standards.example/bad\n"
                "retrieved_at: 2026-07-07T12:00:00Z\n"
                "retrieved_by: fetch_sources.py/web\n"
                "license: null\n"
                "source_type: standards_registry_entry\n"
                "standards: not-a-mapping\n",
                encoding="utf-8",
            )

            config = INVENTORY.load_config(workspace)
            records, warnings, _ = INVENTORY.build_records(workspace, config, previous_detected_at={})

        by_path = {record["raw_paths"][0]: record for record in records}
        provenance = by_path["raw/web/iso-19131.html"]["provenance"]
        self.assertEqual("ISO 19131:2022", provenance["standards"]["designation"])
        self.assertEqual(2, provenance["standards"]["edition"])
        bad = by_path["raw/web/bad-standard.html"]
        self.assertTrue(bad["metadata"]["review_required"])
        self.assertTrue(any("provenance standards must be a mapping" in warning for warning in warnings), warnings)

    def test_inventory_preserves_academic_identity_provenance_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            raw = workspace / "raw" / "papers" / "2601.00001v1.pdf"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"%PDF-1.4\nSynthetic PDF bytes\n")
            (workspace / "sources").mkdir()
            (workspace / "research.yml").write_text(
                "raw:\n"
                "  source_roots:\n"
                "    - raw/papers\n"
                "sources:\n"
                "  manifest_path: sources/manifest.jsonl\n",
                encoding="utf-8",
            )
            raw.with_name("2601.00001v1.pdf.provenance.yml").write_text(
                "origin_url: https://arxiv.org/abs/2601.00001v1\n"
                "retrieved_at: 2026-07-05T12:00:00Z\n"
                "retrieved_by: fetch_sources.py/arxiv\n"
                "license: unresolved\n"
                "terms_url: https://arxiv.org/abs/2601.00001v1\n"
                "academic_provider: arxiv\n"
                "academic_source_type: preprint\n"
                "title: Synthetic Retrieval Paper\n"
                "authors:\n"
                "  - Ada Lovelace\n"
                "  - Grace Hopper\n"
                "published: 2026-01-01T01:02:03Z\n"
                "arxiv_id: 2601.00001v1\n"
                "doi: 10.5555/example\n"
                "doi_source: arxiv-atom\n"
                "openalex_work_id: W260100001\n"
                "openalex_publication_year: 2026\n"
                "openalex_enrichment_status: resolved\n"
                "provider_license_slug: public-domain\n"
                "license_source: openalex\n"
                "openalex_title_lag: true\n"
                "openalex_reported_title: Synthetic Retrieval Paper v1\n"
                "openalex_reported_authors:\n"
                "  - Ada Lovelace\n"
                "openalex_reported_publication_year: 2025\n"
                "openalex_identity_evidence:\n"
                "  title: mismatch\n"
                "  authors: matched\n"
                "doi_resolution:\n"
                "  status: datacite_arxiv_doi\n"
                "  resolved_url: https://arxiv.org/abs/2601.00001\n"
                "  matches_arxiv_id: true\n",
                encoding="utf-8",
            )

            config = INVENTORY.load_config(workspace)
            records, warnings, _ = INVENTORY.build_records(workspace, config, previous_detected_at={})

        self.assertFalse(any("unknown provenance field" in warning for warning in warnings), warnings)
        paper = next(record for record in records if record.get("provenance", {}).get("arxiv_id") == "2601.00001v1")
        provenance = paper["provenance"]
        self.assertEqual("Synthetic Retrieval Paper", provenance["title"])
        self.assertEqual(["Ada Lovelace", "Grace Hopper"], provenance["authors"])
        self.assertEqual("2026-01-01T01:02:03Z", provenance["published"])
        self.assertEqual("10.5555/example", provenance["doi"])
        self.assertEqual("arxiv-atom", provenance["doi_source"])
        self.assertEqual("W260100001", provenance["openalex_work_id"])
        self.assertEqual(2026, provenance["openalex_publication_year"])
        self.assertEqual("resolved", provenance["openalex_enrichment_status"])
        self.assertEqual("public-domain", provenance["provider_license_slug"])
        self.assertEqual("openalex", provenance["license_source"])
        self.assertTrue(provenance["openalex_title_lag"])
        self.assertEqual("Synthetic Retrieval Paper v1", provenance["openalex_reported_title"])
        self.assertEqual(["Ada Lovelace"], provenance["openalex_reported_authors"])
        self.assertEqual(2025, provenance["openalex_reported_publication_year"])
        self.assertEqual("mismatch", provenance["openalex_identity_evidence"]["title"])
        self.assertTrue(provenance["doi_resolution"]["matches_arxiv_id"])

    def test_pdf_normalization_prefers_provider_identity_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            pdf = workspace / "raw" / "papers" / "2601.00001v1.pdf"
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4\n")
            record = {
                "id": "paper:2601.00001v1",
                "kind": "pdf",
                "raw_paths": ["raw/papers/2601.00001v1.pdf"],
                "raw_pdf": "raw/papers/2601.00001v1.pdf",
                "status": "pending",
                "provenance": {
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "title": "Synthetic Retrieval Paper",
                    "authors": ["Ada Lovelace", "Grace Hopper"],
                    "doi": "10.5555/example",
                    "openalex_work_id": "W260100001",
                    "arxiv_id": "2601.00001v1",
                },
            }
            original_extract = NORMALIZE.extract_pdf_text
            NORMALIZE.extract_pdf_text = lambda *_args: (
                "Synthetic Retrieval Paper\nYu Sun 1 Xiaolong Wang 1\nAbstract\nThis text should not become identity.",
                [],
                True,
            )
            try:
                normalized = NORMALIZE.normalize_pdf_record(workspace, record, "pdftotext")
            finally:
                NORMALIZE.extract_pdf_text = original_extract
            output_path = workspace / "sources" / "normalized" / "paper--2601.00001v1.md"
            frontmatter = NORMALIZE.frontmatter_for(normalized, "sources/manifest.jsonl", output_path, "2026-07-05")

        self.assertEqual("Synthetic Retrieval Paper", normalized.title)
        self.assertEqual(["Ada Lovelace", "Grace Hopper"], normalized.authors)
        self.assertEqual("provider", frontmatter["title_source"])
        self.assertEqual(
            "Synthetic Retrieval Paper",
            frontmatter["extracted_title"],
        )
        self.assertEqual("10.5555/example", frontmatter["doi"])
        self.assertEqual("W260100001", frontmatter["openalex_id"])

    def test_pdf_normalization_records_pdf_title_source_for_manual_delivery(self):
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
            NORMALIZE.extract_pdf_text = lambda *_args: ("Manual PDF Title\nAbstract\nBody text.", [], True)
            try:
                normalized = NORMALIZE.normalize_pdf_record(workspace, record, "pdftotext")
            finally:
                NORMALIZE.extract_pdf_text = original_extract
            output_path = workspace / "sources" / "normalized" / "raw--manual-pdf.md"
            frontmatter = NORMALIZE.frontmatter_for(normalized, "sources/manifest.jsonl", output_path, "2026-07-05")

        self.assertEqual("Manual PDF Title", normalized.title)
        self.assertEqual("pdf_inference", frontmatter["title_source"])
        self.assertEqual("Manual PDF Title", frontmatter["extracted_title"])

    def test_html_not_found_page_is_marked_unusable_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            raw = workspace / "raw" / "web" / "official-not-found.html"
            raw.parent.mkdir(parents=True)
            raw.write_text(
                "<html><head><title>404 Not Found</title></head>"
                "<body><h1>Page not found</h1><p>The requested official product page was not found.</p></body></html>",
                encoding="utf-8",
            )
            record = {
                "id": "web:official-not-found",
                "kind": "html",
                "raw_paths": ["raw/web/official-not-found.html"],
                "status": "discovered",
                "detected_at": "2026-07-02T12:00:00Z",
            }

            source = NORMALIZE.normalize_html_record(workspace, record)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "web--official-not-found.md",
                "2026-07-02",
            )

        self.assertFalse(frontmatter["evidence_usable"])
        self.assertIn("html_error_page:not_found", frontmatter["unusable_evidence_reasons"])
        self.assertTrue(any("html_error_page:not_found" in warning for warning in source.warnings))

    def test_sparse_javascript_shell_is_marked_unusable_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            raw = workspace / "raw" / "web" / "product-shell.html"
            raw.parent.mkdir(parents=True)
            raw.write_text(
                "<html><head><title>NVIDIA DGX Spark product specification</title></head>"
                "<body><noscript>You need to enable JavaScript to run this app.</noscript>"
                "<div id='root'></div><script src='/static/app.js'></script><script>window.__DATA__={}</script>"
                "</body></html>",
                encoding="utf-8",
            )
            record = {
                "id": "web:product-shell",
                "kind": "html",
                "raw_paths": ["raw/web/product-shell.html"],
                "status": "discovered",
                "detected_at": "2026-07-02T12:00:00Z",
            }

            source = NORMALIZE.normalize_html_record(workspace, record)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "web--product-shell.md",
                "2026-07-02",
            )

        self.assertFalse(frontmatter["evidence_usable"])
        self.assertIn("html_javascript_shell", frontmatter["unusable_evidence_reasons"])
        self.assertTrue(any("html_javascript_shell" in warning for warning in source.warnings))

    def test_rich_html_with_javascript_disabled_boilerplate_remains_usable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            raw = workspace / "raw" / "web" / "official-guidance.html"
            raw.parent.mkdir(parents=True)
            guidance = " ".join(
                [
                    "Official collection guidance says programs should keep battery terminals protected, "
                    "train collection crews, inspect containers daily, and document fire-response procedures."
                    for _ in range(45)
                ]
            )
            raw.write_text(
                "<html><head><title>Official Battery Collection Guidance</title></head>"
                "<body>"
                "<div class='usa-banner'>JavaScript appears to be disabled on this computer. "
                "Please click here to see any active alerts.</div>"
                f"<main><h1>Battery Collection Best Practices</h1><p>{guidance}</p></main>"
                "<script src='/assets/site.js'></script>"
                "</body></html>",
                encoding="utf-8",
            )
            record = {
                "id": "web:official-guidance",
                "kind": "html",
                "raw_paths": ["raw/web/official-guidance.html"],
                "status": "discovered",
                "detected_at": "2026-07-04T12:00:00Z",
            }

            source = NORMALIZE.normalize_html_record(workspace, record)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "web--official-guidance.md",
                "2026-07-04",
            )

        self.assertTrue(frontmatter["evidence_usable"])
        self.assertIsNone(frontmatter["unusable_evidence_reasons"])
        self.assertNotIn("html_javascript_shell", source.warnings)

    def test_complete_usability_override_can_clear_javascript_shell_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            raw = workspace / "raw" / "web" / "official-shell.html"
            raw.parent.mkdir(parents=True)
            raw.write_text(
                "<html><head><title>Official shell</title></head>"
                "<body><noscript>You need to enable JavaScript to run this app.</noscript>"
                "<div id='root'></div><script src='/static/app.js'></script><script>window.__DATA__={}</script>"
                "</body></html>",
                encoding="utf-8",
            )
            record = {
                "id": "web:official-shell",
                "kind": "html",
                "raw_paths": ["raw/web/official-shell.html"],
                "status": "discovered",
                "detected_at": "2026-07-04T12:00:00Z",
                "provenance": {
                    "evidence_usability_override": {
                        "usable": True,
                        "reviewed_by": "verifier-agent",
                        "reviewed_at": "2026-07-04T12:30:00Z",
                        "reason": "Reviewer verified retrieved bytes outside the generic shell heuristic.",
                    }
                },
            }

            source = NORMALIZE.normalize_html_record(workspace, record)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "web--official-shell.md",
                "2026-07-04",
            )

        self.assertTrue(frontmatter["evidence_usable"])
        self.assertIsNone(frontmatter["unusable_evidence_reasons"])
        self.assertTrue(frontmatter["provenance"]["evidence_usability_override_applied"])

    def test_normalized_frontmatter_carries_standards_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            raw = workspace / "raw" / "web" / "iso-19131.html"
            raw.parent.mkdir(parents=True)
            raw.write_text("<html><head><title>ISO 19131:2022</title></head><body>Registry page.</body></html>", encoding="utf-8")
            record = {
                "id": "web:iso-19131",
                "kind": "html",
                "raw_paths": ["raw/web/iso-19131.html"],
                "status": "discovered",
                "detected_at": "2026-07-07T12:00:00Z",
                "provenance": {
                    "origin_url": "https://www.iso.org/standard/77442.html",
                    "source_type": "standards_registry_entry",
                    "standards": {
                        "registry_provider": "iso-open-data",
                        "standards_body": "ISO",
                        "designation": "ISO 19131:2022",
                        "status": "published",
                    },
                },
            }

            source = NORMALIZE.normalize_html_record(workspace, record)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "web--iso-19131.md",
                "2026-07-07",
            )

        self.assertEqual("ISO 19131:2022", frontmatter["standards"]["designation"])
        self.assertEqual("published", frontmatter["standards"]["status"])
        self.assertEqual(frontmatter["standards"], frontmatter["provenance"]["standards"])

    def test_usability_override_cannot_clear_delivery_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            raw = workspace / "raw" / "web" / "upstream-error.html"
            raw.parent.mkdir(parents=True)
            raw.write_text(
                "<html><head><title>Upstream Error</title></head>"
                "<body><p>Temporary upstream error.</p></body></html>",
                encoding="utf-8",
            )
            record = {
                "id": "web:upstream-error",
                "kind": "html",
                "raw_paths": ["raw/web/upstream-error.html"],
                "status": "discovered",
                "detected_at": "2026-07-04T12:00:00Z",
                "provenance": {
                    "source_status": "unavailable",
                    "delivery_failure_code": "http_error",
                    "evidence_usability_override": {
                        "usable": True,
                        "reviewed_by": "verifier-agent",
                        "reviewed_at": "2026-07-04T12:30:00Z",
                        "reason": "Reviewer cannot override the recorded upstream HTTP failure.",
                    },
                },
            }

            source = NORMALIZE.normalize_html_record(workspace, record)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "web--upstream-error.md",
                "2026-07-04",
            )

        self.assertFalse(frontmatter["evidence_usable"])
        self.assertIn("source_status:unavailable", frontmatter["unusable_evidence_reasons"])
        self.assertIn("delivery_failure_code:http_error", frontmatter["unusable_evidence_reasons"])
        self.assertNotIn("evidence_usability_override_applied", frontmatter["provenance"])

    def test_write_normalized_source_records_timestamp_metadata_on_create_and_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            normalized_root = Path(tmpdir) / "sources" / "normalized"
            record = {
                "id": "link:example-org-fixture-project-e51812dad4",
                "kind": "web_link",
                "url": "https://example.org/fixture-project",
                "raw_paths": ["raw/links/links.txt"],
                "metadata": {"host": "example.org"},
            }
            source = NORMALIZE.normalize_link_record(record)

            output_path, action = NORMALIZE.write_normalized_source(
                source,
                normalized_root,
                "sources/manifest.jsonl",
                "2026-06-14",
                normalized_at="2026-06-14T12:34:56Z",
            )
            first_frontmatter = NORMALIZE.read_output_frontmatter(output_path)

            _, second_action = NORMALIZE.write_normalized_source(
                source,
                normalized_root,
                "sources/manifest.jsonl",
                "2026-06-15",
                normalized_at="2026-06-15T01:02:03Z",
                force=True,
            )
            second_frontmatter = NORMALIZE.read_output_frontmatter(output_path)

        self.assertEqual("created", action)
        self.assertEqual("updated", second_action)
        self.assertEqual("2026-06-14", first_frontmatter["created"])
        self.assertEqual("2026-06-14", first_frontmatter["updated"])
        self.assertEqual("2026-06-14T12:34:56Z", first_frontmatter["normalized_at"])
        self.assertEqual("2026-06-14", second_frontmatter["created"])
        self.assertEqual("2026-06-15", second_frontmatter["updated"])
        self.assertEqual("2026-06-15T01:02:03Z", second_frontmatter["normalized_at"])

    def test_codebase_inventory_enabled_emits_architecture_records(self):
        records, _ = self.build_codebase_records()
        codebase_records = records_by_kind(records, "codebase_architecture")

        self.assertEqual(2, len(codebase_records))
        repo_records = [record for record in codebase_records if record.get("url")]
        local_records = [record for record in codebase_records if not record.get("url")]
        self.assertEqual("https://github.com/example/codebase-fixture", repo_records[0]["url"])
        self.assertEqual("repo_link", repo_records[0]["metadata"]["codebase_source_type"])
        self.assertEqual("example/codebase-fixture", repo_records[0]["metadata"]["repo_full_name"])
        self.assertEqual("local_repo", local_records[0]["metadata"]["codebase_source_type"])
        self.assertEqual(["raw/code/local-fixture"], local_records[0]["raw_paths"])
        self.assertTrue(local_records[0]["metadata"]["codebase_output_dir"].startswith("sources/code_wikis/"))
        raw_paths = [path for record in records for path in record.get("raw_paths", [])]
        self.assertNotIn("raw/code/local-fixture/pyproject.toml", raw_paths)

    def test_codebase_normalization_reads_local_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir) / "codebase-project"
            shutil.copytree(CODEBASE_FIXTURE_ROOT, project_root)
            records, config = self.build_codebase_records(project_root)
            repo_record = next(record for record in records if record.get("url"))
            artifact_dir = project_root / repo_record["metadata"]["codebase_output_dir"]
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "context.json").write_text(
                json.dumps(
                    {
                        "summary": "Fixture repository exposes a tiny architecture surface.",
                        "components": ["package metadata", "source module"],
                        "warnings": ["fixture warning"],
                    }
                )
            )

            normalized = NORMALIZE.normalize_codebase_record(project_root, config, repo_record)
            output_path = project_root / "sources" / "normalized" / "codebase.md"
            frontmatter = NORMALIZE.frontmatter_for(
                normalized,
                "sources/manifest.jsonl",
                output_path,
                "2026-05-10",
            )

            self.assertEqual("codebase_context", normalized.extraction_method)
            self.assertIn("Fixture repository exposes", normalized.abstract)
            self.assertIn("package metadata", normalized.extracted_text)
            self.assertIn("fixture warning", normalized.warnings)
            self.assertEqual("codebase_architecture", frontmatter["source_kind"])
            self.assertEqual("example/codebase-fixture", frontmatter["codebase_repo"])
            self.assertEqual("agent-wiki-cli", frontmatter["codebase_tool"])
            self.assertTrue(any(path.endswith("context.json") for path in frontmatter["codebase_artifact_paths"]))
            self.assertEqual("artifact_recorded", frontmatter["fetch_status"])

    def test_codebase_normalization_missing_artifact_is_stub(self):
        records, config = self.build_codebase_records()
        repo_record = next(record for record in records if record.get("url"))

        normalized = NORMALIZE.normalize_codebase_record(CODEBASE_FIXTURE_ROOT, config, repo_record)

        self.assertEqual("codebase_stub", normalized.extraction_method)
        self.assertIn("Adapter output has not been recorded", normalized.abstract)
        self.assertTrue(any("no codebase adapter artifact" in warning for warning in normalized.warnings))

    def write_latex_paper(self, workspace: Path, arxiv_id: str, bibliography: str) -> dict:
        bundle = workspace / "raw" / "other" / f"arXiv-{arxiv_id}"
        bundle.mkdir(parents=True)
        (bundle / "main.tex").write_text(
            "\\documentclass{article}\n"
            f"\\title{{Paper {arxiv_id}}}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "\\begin{abstract}Synthetic citation graph fixture.\\end{abstract}\n"
            "\\section{Overview}\n"
            "Citation graph evidence lives in the local bibliography.\n"
            "\\bibliography{refs}\n"
            "\\end{document}\n"
        )
        (bundle / "refs.bib").write_text(bibliography)
        return {
            "id": f"paper:{arxiv_id}",
            "kind": "paper",
            "latex_root": f"raw/other/arXiv-{arxiv_id}",
            "entrypoint": "main.tex",
            "metadata": {"arxiv_id": arxiv_id},
        }

    def test_latex_normalization_records_bidirectional_arxiv_bibliography_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            first = self.write_latex_paper(
                workspace,
                "2601.00001v1",
                "@article{second,\n"
                "  archivePrefix = {arXiv},\n"
                "  eprint = {2601.00002v1}\n"
                "}\n",
            )
            second = self.write_latex_paper(
                workspace,
                "2601.00002v1",
                "@article{first,\n"
                "  eprinttype = {arxiv},\n"
                "  eprint = {2601.00001v1}\n"
                "}\n",
            )
            records = [first, second]

            first_source = NORMALIZE.normalize_latex_record(workspace, first)
            second_source = NORMALIZE.normalize_latex_record(workspace, second)
            first_frontmatter = NORMALIZE.frontmatter_for(
                first_source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "paper--2601.00001v1.md",
                "2026-06-13",
                manifest_records=records,
                project_root=workspace,
            )
            second_frontmatter = NORMALIZE.frontmatter_for(
                second_source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "paper--2601.00002v1.md",
                "2026-06-13",
                manifest_records=records,
                project_root=workspace,
            )

            self.assertEqual(["paper:2601.00002v1"], first_frontmatter["references_source_ids"])
            self.assertEqual(["paper:2601.00001v1"], second_frontmatter["references_source_ids"])

    def test_latex_normalization_matches_doi_references_from_bibtex(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            citing = self.write_latex_paper(
                workspace,
                "2601.00003v1",
                "@article{doiTarget,\n"
                "  doi = {https://doi.org/10.5555/Fixture.DOI}\n"
                "}\n",
            )
            target = {
                "id": "paper:doi-target",
                "kind": "paper",
                "raw_paths": ["raw/papers/doi-target.txt"],
                "metadata": {"doi": "10.5555/fixture.doi"},
            }
            records = [citing, target]

            source = NORMALIZE.normalize_latex_record(workspace, citing)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "paper--2601.00003v1.md",
                "2026-06-13",
                manifest_records=records,
                project_root=workspace,
            )

            self.assertEqual(["paper:doi-target"], frontmatter["references_source_ids"])

    def test_latex_reference_matching_ignores_unmatched_duplicates_and_self_references(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            citing = self.write_latex_paper(
                workspace,
                "2601.00004v1",
                "@article{targetA, eprint = {2601.00005v1}, archivePrefix = {arXiv}}\n"
                "@article{targetB, eprint = {2601.00005v1}, archivePrefix = {arXiv}}\n"
                "@article{self, eprint = {2601.00004v1}, archivePrefix = {arXiv}}\n"
                "@article{missing, eprint = {2601.99999v1}, archivePrefix = {arXiv}}\n"
                "@article{missingDoi, doi = {10.5555/missing}}\n",
            )
            target = self.write_latex_paper(workspace, "2601.00005v1", "")
            records = [citing, target]

            source = NORMALIZE.normalize_latex_record(workspace, citing)
            frontmatter = NORMALIZE.frontmatter_for(
                source,
                "sources/manifest.jsonl",
                workspace / "sources" / "normalized" / "paper--2601.00004v1.md",
                "2026-06-13",
                manifest_records=records,
                project_root=workspace,
            )

            self.assertEqual(["paper:2601.00005v1"], frontmatter["references_source_ids"])


class PdfExtractionBugTests(unittest.TestCase):
    """Tests for bug fixes in extract_pdf_text and infer_pdf_title."""

    def test_extract_pdf_text_nonzero_exit_returns_empty_string(self):
        """Bug 2A: non-zero returncode must not return partial stdout."""
        import subprocess
        from unittest.mock import MagicMock, patch

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "partial junk"
        mock_result.stderr = ""

        with patch.object(subprocess, "run", return_value=mock_result):
            text, warnings, extractor_ran = NORMALIZE.extract_pdf_text("pdftotext", Path("fake.pdf"), "fake")

        self.assertEqual("", text)
        self.assertFalse(extractor_ran)
        self.assertTrue(any("exited with code 1" in w for w in warnings))

    def test_extract_pdf_text_empty_stdout_keeps_raw_output_for_page_count(self):
        """Zero returncode with whitespace-only stdout warns but preserves the raw
        output (form feeds carry the page count for the needs_ocr heuristic)."""
        import subprocess
        from unittest.mock import MagicMock, patch

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "   \n  \f"
        mock_result.stderr = ""

        with patch.object(subprocess, "run", return_value=mock_result):
            text, warnings, extractor_ran = NORMALIZE.extract_pdf_text("pdftotext", Path("fake.pdf"), "fake")

        self.assertEqual("   \n  \f", text)
        self.assertEqual("", NORMALIZE.normalize_pdf_text(text))
        self.assertTrue(extractor_ran)
        self.assertTrue(any("no output" in w for w in warnings))

    def test_infer_pdf_title_multiline_is_high_confidence(self):
        """Bug 3A: two title lines (joined > 20 chars) → confidence 'high'."""
        # Put two non-metadata lines before 'abstract'
        text = "First Title Line That Is Long Enough\nSecond Title Continuation\nAbstract\nThis is the abstract text."
        title, confidence = NORMALIZE.infer_pdf_title(text, "src:fallback")

        self.assertNotEqual("src:fallback", title)
        self.assertEqual("high", confidence)

    def test_infer_pdf_title_single_long_line_is_high_confidence(self):
        """Bug 3A: single title line > 20 chars → confidence 'high'."""
        text = "A Reasonably Long Title That Exceeds Twenty Characters\nAbstract\nThe abstract follows."
        title, confidence = NORMALIZE.infer_pdf_title(text, "src:fallback")

        self.assertNotEqual("src:fallback", title)
        self.assertEqual("high", confidence)
        self.assertGreater(len(title), 20)

    def test_infer_pdf_title_no_candidate_is_none_confidence(self):
        """Bug 3A: only metadata/short lines → confidence 'none', returns source_id."""
        # Provide only the abstract sentinel line with no preceding meaningful content.
        text = "Abstract\nThis is the abstract."
        title, confidence = NORMALIZE.infer_pdf_title(text, "src:my-paper")

        self.assertEqual("src:my-paper", title)
        self.assertEqual("none", confidence)


class LatexSafetyTests(unittest.TestCase):
    """E15-T04: verify existing LaTeX include safety guards produce warnings without crashing."""

    def test_circular_include_returns_cyclic_warning(self):
        r"""A→B→A circular \input{} should complete with a 'cyclic include skipped' warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            a = root / "a.tex"
            b = root / "b.tex"
            a.write_text(r"\input{b}")
            b.write_text(r"\input{a}")

            result = NORMALIZE.read_latex_with_includes(root, root, a)

        self.assertTrue(
            any("cyclic include skipped" in w for w in result.warnings),
            f"Expected 'cyclic include skipped' warning; got: {result.warnings}",
        )

    def test_depth_exceeded_returns_depth_warning(self):
        """An include encountered at MAX_INCLUDE_DEPTH+1 should warn about depth exceeded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = root / "entry.tex"
            sub = root / "sub.tex"
            entry.write_text(r"\input{sub}")
            sub.write_text("Deep content.")

            # Call with depth already at the limit so the sub-include exceeds it.
            result = NORMALIZE.read_latex_with_includes(
                root, root, entry, depth=NORMALIZE.MAX_INCLUDE_DEPTH
            )

        self.assertTrue(
            any("include depth exceeded" in w for w in result.warnings),
            f"Expected 'include depth exceeded' warning; got: {result.warnings}",
        )

    def test_path_traversal_with_dotdot_is_blocked(self):
        r"""An \input{} containing '..' should be blocked with an 'unsafe include path' warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = root / "entry.tex"
            entry.write_text(r"\input{../outside}")

            result = NORMALIZE.read_latex_with_includes(root, root, entry)

        self.assertTrue(
            any("unsafe include path" in w for w in result.warnings),
            f"Expected 'unsafe include path' warning; got: {result.warnings}",
        )

    def test_missing_include_file_returns_warning(self):
        r"""An \input{} pointing to a non-existent file should produce a 'not found' warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = root / "entry.tex"
            entry.write_text(r"\input{nonexistent}")

            result = NORMALIZE.read_latex_with_includes(root, root, entry)

        self.assertTrue(
            any("not found" in w for w in result.warnings),
            f"Expected 'not found' warning; got: {result.warnings}",
        )


class ConcurrentInventoryTests(unittest.TestCase):
    """E15-T05: manifest write is atomic and inventory is idempotent under repeat/concurrent runs."""

    def _do_inventory(self, workspace: Path) -> None:
        config = INVENTORY.load_config(workspace)
        sources_config = config.get("sources") or {}
        manifest_path = workspace / str(sources_config.get("manifest_path", "sources/manifest.jsonl"))
        records, _, _ = INVENTORY.build_records(workspace, config, previous_detected_at={})
        INVENTORY.write_manifest(manifest_path, records)

    def test_inventory_idempotent_on_repeat_run(self):
        """Running inventory twice on the same workspace produces identical manifests."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            shutil.copytree(FIXTURE_ROOT, workspace)

            self._do_inventory(workspace)
            manifest_path = workspace / "sources" / "manifest.jsonl"
            first_content = manifest_path.read_text()

            self._do_inventory(workspace)
            second_content = manifest_path.read_text()

        self.assertEqual(first_content, second_content, "Repeat inventory run produced different manifest")

    def test_concurrent_inventory_produces_valid_manifest(self):
        """Two concurrent inventory runs must not corrupt the manifest (atomic write guard)."""
        import threading

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            shutil.copytree(FIXTURE_ROOT, workspace)

            errors: list[Exception] = []

            def run():
                try:
                    self._do_inventory(workspace)
                except Exception as exc:
                    errors.append(exc)

            t1 = threading.Thread(target=run)
            t2 = threading.Thread(target=run)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual([], errors, f"Concurrent inventory raised: {errors}")

            manifest_path = workspace / "sources" / "manifest.jsonl"
            self.assertTrue(manifest_path.is_file(), "manifest.jsonl missing after concurrent runs")

            for line in manifest_path.read_text().splitlines():
                if line.strip():
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as exc:
                        self.fail(f"Corrupt JSONL line after concurrent writes: {exc!r}\nLine: {line!r}")


class RawFingerprintTests(unittest.TestCase):
    def test_inventory_adds_raw_fingerprint_to_papers_not_links(self):
        config = INVENTORY.load_config(FIXTURE_ROOT)
        records, _, _ = INVENTORY.build_records(FIXTURE_ROOT, config, previous_detected_at={})
        paper = records_by_id(records)["paper:2601.00001v1"]
        self.assertIsInstance(paper.get("raw_fingerprint"), str)
        self.assertTrue(paper["raw_fingerprint"].startswith("sha256:"))
        for record in records:
            if record.get("kind") in {"web_link", "repo_link", "image", "link"}:
                self.assertNotIn("raw_fingerprint", record)

    def test_inventory_fingerprint_changes_when_raw_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text(
                "raw:\n  source_roots:\n    - raw/papers\n"
                "sources:\n  manifest_path: sources/manifest.jsonl\n  normalized_dir: sources/normalized\n"
            )
            bundle = root / "raw" / "papers" / "arxiv-2601.00009v1"
            bundle.mkdir(parents=True)
            tex = bundle / "main.tex"
            tex.write_text("\\documentclass{article}\n\\begin{document}\nOriginal.\n\\end{document}\n")
            config = INVENTORY.load_config(root)
            before = records_by_id(INVENTORY.build_records(root, config, {})[0])["paper:2601.00009v1"]["raw_fingerprint"]
            tex.write_text("\\documentclass{article}\n\\begin{document}\nRevised body.\n\\end{document}\n")
            after = records_by_id(INVENTORY.build_records(root, config, {})[0])["paper:2601.00009v1"]["raw_fingerprint"]
            self.assertNotEqual(before, after)

    def test_is_stale_compares_manifest_and_stored_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "rec.md"
            output.write_text("---\nraw_fingerprint: sha256:AAA\n---\n# x\n")
            self.assertFalse(NORMALIZE.is_stale({"raw_fingerprint": "sha256:AAA"}, output))
            self.assertTrue(NORMALIZE.is_stale({"raw_fingerprint": "sha256:BBB"}, output))
            # No manifest fingerprint -> not stale (links/codebase, pre-feature manifests).
            self.assertFalse(NORMALIZE.is_stale({}, output))
            # Stored fingerprint missing but manifest has one -> stale (backfill).
            bare = Path(tmpdir) / "bare.md"
            bare.write_text("---\ntype: normalized_source\n---\n# x\n")
            self.assertTrue(NORMALIZE.is_stale({"raw_fingerprint": "sha256:BBB"}, bare))

    def test_read_output_frontmatter_is_line_anchored(self):
        # A value line beginning with `----` must not truncate the frontmatter block.
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "rec.md"
            output.write_text(
                '---\nraw_fingerprint: sha256:Z\nnote: "---- divider"\ncreated: 2026-01-01\n---\n\nBody\n'
            )
            frontmatter = NORMALIZE.read_output_frontmatter(output)
            self.assertEqual("sha256:Z", frontmatter.get("raw_fingerprint"))
            self.assertEqual("2026-01-01", NORMALIZE.existing_created_date(output))


class LogAppendTests(unittest.TestCase):
    def test_concurrent_log_appends_preserve_all_entries(self):
        import threading

        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.md"
            log_path.write_text("# Research Wiki Activity Log\n\n")
            count = 40

            def worker(index: int) -> None:
                NORMALIZE.append_log_entry(log_path, f"## [2026-01-01] normalize | entry-{index}\n\n- body {index}\n")

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            text = log_path.read_text()
            for index in range(count):
                self.assertIn(f"entry-{index}", text)


if __name__ == "__main__":
    unittest.main()
