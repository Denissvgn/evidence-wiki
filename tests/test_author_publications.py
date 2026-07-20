"""Tests for author publication discovery (E35-T02).

`discover_sources.py authors --source-id ID --discover-publications` resolves each
extracted author seed (E35-T01) to an OpenAlex identity and proposes that author's
works as `source_candidate` records of source_type `paper`. It performs network I/O
(`network_io_executed: true`) but never downloads anything; candidates land in the
durable store for explicit review/selection.

These tests use a mocked OpenAlex transport (no real network) and cover: ORCID
exact-match discovery, name-resolved (inferred) discovery, ambiguous-name
review-required path, unrelated-publication rejection, candidate ranking, seed-paper
exclusion, idempotent appends, the disabled-discovery gate, HTTP error envelopes
(rate limit / auth), and OPENALEX_API_KEY handling that never leaks the key.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

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


DISCOVER = load_script_module("author_publications_under_test", "discover_sources.py")

SEED_TITLE = "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"
SEED_DOI = "10.1000/seed-paper"
ADA_ORCID = "0000-0002-1825-0097"
ADA_ORCID_URL = f"https://orcid.org/{ADA_ORCID}"


def make_work(
    work_id: str,
    title: str,
    *,
    year: int = 2024,
    doi: str | None = None,
    cited_by: int = 0,
    is_oa: bool = True,
    oa_status: str | None = "gold",
    landing: str | None = None,
    pdf: str | None = None,
    license_key: str | None = None,
) -> dict:
    location = {}
    if landing or pdf or license_key:
        location = {
            "landing_page_url": landing,
            "pdf_url": pdf,
            "license": license_key,
        }
    return {
        "id": f"https://openalex.org/{work_id}",
        "display_name": title,
        "doi": (f"https://doi.org/{doi}" if doi else None),
        "publication_year": year,
        "type": "article",
        "cited_by_count": cited_by,
        "open_access": {"is_oa": is_oa, "oa_status": oa_status},
        "best_oa_location": location or None,
    }


def works_payload(*works: dict) -> bytes:
    return json.dumps({"meta": {"count": len(works)}, "results": list(works)}).encode("utf-8")


def authors_payload(*authors: dict) -> bytes:
    return json.dumps({"meta": {"count": len(authors)}, "results": list(authors)}).encode("utf-8")


def http_error(code: int) -> HTTPError:
    return HTTPError("https://api.openalex.org/works", code, "error", {}, io.BytesIO(b""))


class AuthorPublicationsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Always start without a key unless a test sets one, and never leak a real
        # environment key into the assertions.
        self._saved_key = os.environ.pop("OPENALEX_API_KEY", None)
        self.addCleanup(self._restore_key)
        self.addCleanup(self._reset_transport)

    def _restore_key(self):
        if self._saved_key is not None:
            os.environ["OPENALEX_API_KEY"] = self._saved_key
        else:
            os.environ.pop("OPENALEX_API_KEY", None)

    def _reset_transport(self):
        DISCOVER.OPENALEX_TRANSPORT = None
        DISCOVER.OPENALEX_LAST_REQUEST_AT = None
        DISCOVER.OPENALEX_CLOCK = lambda: 0.0
        DISCOVER.OPENALEX_SLEEP = lambda _seconds: None

    def install_transport(self, transport) -> None:
        DISCOVER.OPENALEX_TRANSPORT = transport
        DISCOVER.OPENALEX_CLOCK = lambda: 0.0
        DISCOVER.OPENALEX_SLEEP = lambda _seconds: None
        DISCOVER.OPENALEX_LAST_REQUEST_AT = None

    def write_workspace(
        self,
        root: Path,
        *,
        manifest: list[dict],
        normalized: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> Path:
        workspace = root / "ws"
        (workspace / "sources" / "normalized").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: author-pubs-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "  normalized_dir: sources/normalized",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
            "    providers:",
            "      - openalex",
        ]
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in manifest), encoding="utf-8"
        )
        for filename, body in (normalized or {}).items():
            (workspace / "sources" / "normalized" / filename).write_text(body, encoding="utf-8")
        return workspace

    def openalex_paper_record(self, source_id: str, *, authors: list[dict]) -> dict:
        return {
            "id": source_id,
            "kind": "paper",
            "status": "normalized",
            "metadata": {"authorships": authors, "doi": SEED_DOI},
        }

    def frontmatter_doc(self, source_id: str, *, title: str, authors: list[str], doi: str = SEED_DOI) -> str:
        author_lines = "\n".join(f"  - {a}" for a in authors)
        return (
            "---\n"
            "type: normalized_source\n"
            f"source_id: {source_id}\n"
            f"title: {title}\n"
            f"doi: {doi}\n"
            "authors:\n"
            f"{author_lines}\n"
            "confidence: high\n"
            "---\n\n# body\n"
        )

    def frontmatter_doc_without_title(self, source_id: str, *, authors: list[str], doi: str = SEED_DOI) -> str:
        author_lines = "\n".join(f"  - {a}" for a in authors)
        return (
            "---\n"
            "type: normalized_source\n"
            f"source_id: {source_id}\n"
            f"doi: {doi}\n"
            "authors:\n"
            f"{author_lines}\n"
            "confidence: high\n"
            "---\n\n# body\n"
        )

    def run_authors(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "authors", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    # --- ORCID exact match ---------------------------------------------------

    def test_orcid_author_emits_high_confidence_related_candidates(self):
        related = make_work(
            "W111", "Retrieval Augmented Generation Benchmarks",
            year=2023, doi="10.1000/related", cited_by=120, is_oa=True,
            landing="https://example.com/related", license_key="cc-by",
        )
        related["authorships"] = [
            {"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}},
            {"author": {"display_name": "Grace Hopper"}},
        ]
        unrelated = make_work(
            "W222", "Marine Biology of Pacific Reefs",
            year=2010, doi="10.1000/unrelated", cited_by=2, is_oa=False,
        )
        seed_work = make_work("W999", SEED_TITLE, year=2020, doi=SEED_DOI, is_oa=True)

        def transport(url, timeout, headers):
            self.assertIn("/works", url)
            # The ORCID rides in the (urlencoded) author.orcid filter value.
            self.assertIn("author.orcid", url)
            self.assertIn(ADA_ORCID, url)
            return works_payload(related, unrelated, seed_work)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL},
                              "institutions": [{"display_name": "Analytical Engine Lab"}]}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc(
                    "paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"],
                )},
            )
            self.install_transport(transport)
            code, stdout, stderr = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("authors", report["command"])
        self.assertTrue(report["network_io_executed"])
        self.assertEqual("openalex", report["publications_provider"])
        candidates = report["candidates"]
        # Seed paper skipped; two candidates remain.
        self.assertEqual(2, len(candidates))
        titles = [c["title"] for c in candidates]
        self.assertNotIn(SEED_TITLE, titles)
        self.assertIn("Retrieval Augmented Generation Benchmarks", titles)

        related_cand = next(c for c in candidates if c["openalex"]["work_id"] == "W111")
        self.assertEqual("orcid_exact", related_cand["openalex"]["identity_match"])
        self.assertEqual("paper", related_cand["source_type"])
        self.assertEqual("primary_non_official", related_cand["trust_tier"])
        self.assertIsNone(related_cand["official_source"])
        self.assertEqual("review", related_cand["recommended_action"])
        self.assertNotIn("identity_inferred", related_cand["reasoning"]["risk_flags"])
        self.assertIn("unknown_officialness", related_cand["reasoning"]["risk_flags"])
        gate = related_cand["quality_gates"]["author_identity"]
        self.assertEqual("passed", gate["status"])
        self.assertEqual("orcid_exact", gate["identity_match"])
        self.assertEqual(["orcid"], gate["signals"])
        self.assertFalse(gate["review_required"])
        self.assertEqual(
            {
                "provider_ids": {"arxiv": None, "openalex": "W111", "doi": "10.1000/related"},
                "title": "Retrieval Augmented Generation Benchmarks",
                "authors": ["Ada Lovelace", "Grace Hopper"],
                "publication_year": 2023,
                "doi": "10.1000/related",
                "arxiv_id": None,
                "open_access": True,
                "oa_status": "gold",
                "license": "cc-by",
                "landing_page_url": "https://example.com/related",
                "pdf_url": None,
                "resolution_status": "resolved",
            },
            related_cand["paper"],
        )
        self.assertEqual(
            {
                "provider": "openalex",
                "network_io_executed": True,
                "token_used": False,
                "max_results": 10,
                "per_provider_limit": DISCOVER.OPENALEX_DISCOVERY_PER_AUTHOR,
                "max_authors": DISCOVER.OPENALEX_DISCOVERY_MAX_AUTHORS,
                "max_results_cap": DISCOVER.OPENALEX_DISCOVERY_MAX_RESULTS_CAP,
            },
            related_cand["provider_budget"],
        )
        # Reasoning contract: all five fields present and matched terms non-empty.
        for field in ("matched_query_terms", "authority_reason", "freshness_reason", "scope_reason", "risk_flags"):
            self.assertIn(field, related_cand["reasoning"])
        self.assertTrue(related_cand["reasoning"]["matched_query_terms"])

        identity = report["author_identity"][0]
        self.assertEqual("orcid_exact", identity["identity"])
        identity_gate = identity["quality_gates"]["author_identity"]
        self.assertEqual("passed", identity_gate["status"])
        self.assertFalse(identity_gate["review_required"])
        self.assertTrue(report["candidates_path"].endswith("candidates.jsonl"))

    # --- Unrelated publication rejection + ranking ---------------------------

    def test_unrelated_publication_is_rejected_and_ranks_below_related(self):
        related = make_work("W111", "Retrieval Augmented Generation Benchmarks", year=2023, is_oa=True)
        unrelated = make_work("W222", "Marine Biology of Pacific Reefs", year=2010, is_oa=False)
        self.install_transport(lambda url, t, h: works_payload(related, unrelated))

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc("paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"])},
            )
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        ordered = report["candidates"]
        self.assertEqual("Retrieval Augmented Generation Benchmarks", ordered[0]["title"])
        rejected = next(c for c in ordered if c["title"] == "Marine Biology of Pacific Reefs")
        self.assertEqual("reject", rejected["recommended_action"])
        self.assertIn("out_of_scope", rejected["reasoning"]["risk_flags"])
        self.assertIn("out of scope", rejected["rationale"].lower())

    # --- Name-resolved (inferred identity) -----------------------------------

    def test_name_only_author_resolves_via_author_search_and_is_flagged_inferred(self):
        resolved_author = {
            "id": "https://openalex.org/A555",
            "display_name": "Jane Roe",
            "works_count": 42,
            "orcid": None,
        }
        related = make_work("W333", "Retrieval Augmented Generation Survey", year=2022, is_oa=True)

        def transport(url, timeout, headers):
            if "/authors" in url:
                self.assertIn("search=jane", url.lower())
                return authors_payload(resolved_author)
            return works_payload(related)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:jane",
                    authors=[{"author": {"display_name": "Jane Roe"}}],
                )],
                normalized={"paper--jane.md": self.frontmatter_doc("paper:jane", title=SEED_TITLE, authors=["Jane Roe"])},
            )
            self.install_transport(transport)
            code, stdout, stderr = self.run_authors(workspace, "--source-id", "paper:jane", "--discover-publications")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        identity = report["author_identity"][0]
        self.assertEqual("name_resolved", identity["identity"])
        self.assertEqual("A555", identity["openalex_author_id"])
        self.assertEqual("author.id:A555", identity["works_filter"])
        candidate = report["candidates"][0]
        self.assertEqual("name_resolved", candidate["openalex"]["identity_match"])
        self.assertIn("identity_inferred", candidate["reasoning"]["risk_flags"])
        gate = candidate["quality_gates"]["author_identity"]
        self.assertEqual("review_required", gate["status"])
        self.assertEqual("name_resolved", gate["identity_match"])
        self.assertIn("seed_title_context", gate["signals"])
        self.assertTrue(gate["review_required"])

    # --- Ambiguous name review-required path ---------------------------------

    def test_ambiguous_name_emits_warning_and_no_candidates_for_that_author(self):
        # Two distinct OpenAlex author records share the same common name; neither
        # can be disambiguated, so identity is ambiguous and no works are listed.
        a1 = {"id": "https://openalex.org/A1", "display_name": "John Smith", "works_count": 10}
        a2 = {"id": "https://openalex.org/A2", "display_name": "John Smith", "works_count": 8}

        def transport(url, timeout, headers):
            self.assertIn("/authors", url)
            return authors_payload(a1, a2)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:smith",
                    authors=[{"author": {"display_name": "John Smith"}}],
                )],
                normalized={"paper--smith.md": self.frontmatter_doc(
                    "paper:smith", title=SEED_TITLE, authors=["John Smith"],
                )},
            )
            self.install_transport(transport)
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:smith", "--discover-publications")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual("ambiguous", report["author_identity"][0]["identity"])
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("author_identity_ambiguous", codes)
        self.assertEqual(0, report["candidate_count"])
        self.assertEqual([], report["candidates"])

    def test_name_only_author_without_title_or_affiliation_is_blocked_before_candidates(self):
        resolved_author = {
            "id": "https://openalex.org/A888",
            "display_name": "Jordan Lee",
            "works_count": 12,
            "orcid": None,
        }
        related = make_work("W444", "Retrieval Augmented Generation Followup", year=2024, is_oa=True)

        def transport(url, timeout, headers):
            if "/authors" in url:
                return authors_payload(resolved_author)
            return works_payload(related)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:context-missing",
                    authors=[{"author": {"display_name": "Jordan Lee"}}],
                )],
                normalized={"paper--context-missing.md": self.frontmatter_doc_without_title(
                    "paper:context-missing", authors=["Jordan Lee"],
                )},
            )
            self.install_transport(transport)
            code, stdout, stderr = self.run_authors(
                workspace, "--source-id", "paper:context-missing", "--discover-publications"
            )
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        identity = report["author_identity"][0]
        self.assertEqual("context_missing", identity["identity"])
        gate = identity["quality_gates"]["author_identity"]
        self.assertEqual("blocked", gate["status"])
        self.assertEqual("context_missing", gate["identity_match"])
        self.assertTrue(gate["review_required"])
        self.assertIn("author_identity_context_missing", {w["code"] for w in report["warnings"]})
        self.assertEqual(0, report["candidate_count"])
        self.assertEqual([], report["candidates"])

    def test_no_author_match_emits_no_match_warning(self):
        def transport(url, timeout, headers):
            return authors_payload()  # empty results
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:nobody",
                    authors=[{"author": {"display_name": "Rare Namexyz"}}],
                )],
                normalized={"paper--nobody.md": self.frontmatter_doc(
                    "paper:nobody", title=SEED_TITLE, authors=["Rare Namexyz"],
                )},
            )
            self.install_transport(transport)
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:nobody", "--discover-publications")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual("no_match", report["author_identity"][0]["identity"])
        self.assertIn("author_identity_no_match", {w["code"] for w in report["warnings"]})

    # --- Default (no flag) stays read-only -----------------------------------

    def test_without_flag_no_network_and_authors_seed_list_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc("paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"])},
            )
            # Any network call would fail the test (transport raises).
            self.install_transport(lambda *a: (_ for _ in ()).throw(AssertionError("no network expected")))
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:ada")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertFalse(report["network_io_executed"])
        self.assertNotIn("candidates", report)
        self.assertEqual(["Ada Lovelace"], [a["name"] for a in report["authors"]])

    # --- Disabled gate -------------------------------------------------------

    def test_disabled_discovery_refuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc("paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"])},
                enabled=False,
            )
            code, _, stderr = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])

    # --- Idempotent append ---------------------------------------------------

    def test_re_run_does_not_duplicate_candidates(self):
        related = make_work("W111", "Retrieval Augmented Generation Benchmarks", is_oa=True)
        self.install_transport(lambda url, t, h: works_payload(related))
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc("paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"])},
            )
            code1, out1, _ = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
            code2, out2, _ = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            lines = [ln for ln in store.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(0, code1)
        self.assertEqual(0, code2)
        first = json.loads(out1)
        second = json.loads(out2)
        self.assertTrue(first["written"] >= 1)
        self.assertEqual(0, second["written"])
        self.assertEqual(len(lines), first["candidate_count"])

    # --- Token handling: reported but never leaked ---------------------------

    def test_api_key_is_reported_but_never_leaked(self):
        os.environ["OPENALEX_API_KEY"] = "super-secret-key-value"
        related = make_work("W111", "Retrieval Augmented Generation Benchmarks", is_oa=True)
        captured_urls: list[str] = []

        def transport(url, timeout, headers):
            captured_urls.append(url)
            return works_payload(related)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc("paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"])},
            )
            self.install_transport(transport)
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
            store_text = (workspace / "sources" / "discovery" / "candidates.jsonl").read_text(encoding="utf-8")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertTrue(report["token_used"])
        # The key is sent to the provider (in the query string, per OpenAlex's
        # polite-pool convention) but must never appear in stdout or the store.
        self.assertTrue(any("super-secret-key-value" in u for u in captured_urls))
        self.assertNotIn("super-secret-key-value", stdout)
        self.assertNotIn("super-secret-key-value", store_text)

    # --- HTTP error envelopes ------------------------------------------------

    def test_rate_limit_surfaces_openalex_rate_limited(self):
        self.install_transport(lambda url, t, h: (_ for _ in ()).throw(http_error(429)))
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc("paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"])},
            )
            code, _, stderr = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
        self.assertEqual(2, code)
        self.assertEqual("OPENALEX_RATE_LIMITED", json.loads(stderr)["error_code"])

    def test_auth_failure_surfaces_openalex_auth_required(self):
        self.install_transport(lambda url, t, h: (_ for _ in ()).throw(http_error(401)))
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.openalex_paper_record(
                    "paper:ada",
                    authors=[{"author": {"display_name": "Ada Lovelace", "orcid": ADA_ORCID_URL}}],
                )],
                normalized={"paper--ada.md": self.frontmatter_doc("paper:ada", title=SEED_TITLE, authors=["Ada Lovelace"])},
            )
            code, _, stderr = self.run_authors(workspace, "--source-id", "paper:ada", "--discover-publications")
        self.assertEqual(2, code)
        self.assertEqual("OPENALEX_AUTH_REQUIRED", json.loads(stderr)["error_code"])


if __name__ == "__main__":
    unittest.main()
