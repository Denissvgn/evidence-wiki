"""Tests for companion artifact discovery (E35-T03).

`discover_sources.py companions --source-id ID` finds a paper's companion
repositories, datasets, project pages, supplemental material, and publisher
pages. It prefers links already present in the paper body/frontmatter `links` or
its provider metadata (highest trust), then falls back to GitHub repository
discovery (E32-T02) and the configured general search provider (E33). It proposes
candidates for review and never fetches or executes anything.

These tests use a mocked GitHub transport and a fixture search provider (no real
network) and cover: inline host classification across all five source types,
self-link exclusion, provider-metadata publisher pages, GitHub dedupe against an
inline github link (inline wins), ranking (paper_inline > github_search > search),
the --no-github / --no-search flags, search skip-with-warning when unconfigured,
the disabled-discovery gate, idempotent appends, --request-id origin, and that
nothing is ever fetched.
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


DISCOVER = load_script_module("companion_discovery_under_test", "discover_sources.py")

SEED_TITLE = "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"
SEED_ARXIV = "2005.11401"


def repo(full_name: str, *, description: str = "companion repo", license_key: str | None = "MIT") -> dict:
    owner, name = full_name.split("/", 1)
    license_obj = {"spdx_id": license_key, "key": (license_key or "").lower()} if license_key is not None else None
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "html_url": f"https://github.com/{full_name}",
        "url": f"https://api.github.com/repos/{full_name}",
        "description": description,
        "default_branch": "main",
        "license": license_obj,
        "stargazers_count": 10,
        "forks_count": 1,
        "archived": False,
        "fork": False,
        "pushed_at": "2026-01-01T00:00:00Z",
    }


def github_payload(*repos: dict) -> bytes:
    return json.dumps({"total_count": len(repos), "items": list(repos)}).encode("utf-8")


class CompanionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._saved_token = os.environ.pop("GITHUB_TOKEN", None)
        self.addCleanup(self._restore_token)
        self.addCleanup(self._reset_transport)

    def _restore_token(self):
        if self._saved_token is not None:
            os.environ["GITHUB_TOKEN"] = self._saved_token
        else:
            os.environ.pop("GITHUB_TOKEN", None)

    def _reset_transport(self):
        DISCOVER.GITHUB_TRANSPORT = None
        DISCOVER.GITHUB_LAST_REQUEST_AT = None

    def install_github(self, payload: bytes) -> None:
        DISCOVER.GITHUB_TRANSPORT = lambda url, timeout, headers: payload
        DISCOVER.GITHUB_CLOCK = lambda: 0.0
        DISCOVER.GITHUB_SLEEP = lambda _seconds: None
        DISCOVER.GITHUB_LAST_REQUEST_AT = None

    def write_workspace(
        self,
        root: Path,
        *,
        manifest: list[dict],
        normalized: str | None = None,
        enabled: bool = True,
        search_provider: bool = False,
        search_results: list[dict] | None = None,
    ) -> Path:
        workspace = root / "ws"
        (workspace / "sources" / "normalized").mkdir(parents=True, exist_ok=True)
        (workspace / "sources" / "discovery" / "fixtures").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: companions-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "  normalized_dir: sources/normalized",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
            "    providers:",
            "      - github",
            "      - search",
        ]
        if search_provider:
            lines += [
                "    search:",
                "      provider: fixture",
                "      fixture_path: sources/discovery/fixtures/search.jsonl",
            ]
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in manifest), encoding="utf-8"
        )
        if normalized is not None:
            safe_id = manifest[0]["id"].replace(":", "--")
            (workspace / "sources" / "normalized" / f"{safe_id}.md").write_text(normalized, encoding="utf-8")
        if search_provider:
            body = "".join(json.dumps(r) + "\n" for r in (search_results or []))
            (workspace / "sources" / "discovery" / "fixtures" / "search.jsonl").write_text(body, encoding="utf-8")
        return workspace

    def paper_record(self, source_id: str, *, metadata: dict | None = None) -> dict:
        record = {"id": source_id, "kind": "paper", "status": "normalized"}
        if metadata is not None:
            record["metadata"] = metadata
        return record

    def normalized_doc(
        self,
        source_id: str,
        *,
        title: str = SEED_TITLE,
        arxiv_id: str = SEED_ARXIV,
        links: list[str] | None = None,
        body: str = "",
    ) -> str:
        parts = [
            "---",
            "type: normalized_source",
            f"source_id: {source_id}",
            # Quote the title (json is valid YAML) so a colon in the title — like a
            # "SystemName: subtitle" paper title — does not break frontmatter parsing,
            # matching how the real normalizer serializes it via yaml.safe_dump.
            f"title: {json.dumps(title)}",
            f"arxiv_id: {arxiv_id}",
            "authors:",
            "  - Ada Lovelace",
        ]
        if links is not None:
            parts.append("links:")
            for url in links:
                parts.append(f"  - {url}")
        parts.append("---")
        parts.append("")
        parts.append(body)
        return "\n".join(parts) + "\n"

    def run_companions(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "companions", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    # --- Phase 1: inline classification + self-link exclusion ----------------

    def test_inline_links_classified_into_all_source_types_and_self_link_excluded(self):
        links = [
            "https://github.com/acme/rag-toolkit",       # code_repository
            "https://zenodo.org/records/12345",          # dataset
            "https://doi.org/10.1000/rag-data",          # publisher_page (official_primary)
            "https://example.com/rag-project",           # project_page
        ]
        body = (
            "A related preprint https://arxiv.org/abs/2401.99999 extends this work. "
            "This paper itself lives at https://arxiv.org/abs/2005.11401."  # self-link -> excluded
        )
        normalized = self.normalized_doc("paper:rag", links=links, body=body)
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            # Inline-only: no network expected.
            self.install_github(github_payload())  # not used because --no-github
            code, stdout, stderr = self.run_companions(
                workspace, "--source-id", "paper:rag", "--no-github", "--no-search"
            )
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertFalse(report["network_io_executed"])
        by_type = {c["source_type"]: c for c in report["candidates"]}
        self.assertEqual({"code_repository", "dataset", "publisher_page", "project_page", "supplemental_material"},
                         set(by_type))
        self.assertEqual("primary_non_official", by_type["code_repository"]["trust_tier"])
        self.assertEqual("primary_non_official", by_type["dataset"]["trust_tier"])
        self.assertEqual("official_primary", by_type["publisher_page"]["trust_tier"])
        self.assertTrue(by_type["publisher_page"]["official_source"])
        self.assertEqual("primary_non_official", by_type["supplemental_material"]["trust_tier"])
        self.assertTrue(all(c["evidence_origin"] == "paper_inline" for c in report["candidates"]))
        # The seed paper's own arXiv listing is never proposed.
        urls = {c["url"] for c in report["candidates"]}
        self.assertFalse(any("2005.11401" in u for u in urls), urls)
        self.assertTrue(any("2401.99999" in u for u in urls), urls)
        self.assertEqual("review", by_type["code_repository"]["recommended_action"])
        repo_gate = by_type["code_repository"]["quality_gates"]["companion_repository"]
        self.assertEqual("review_required", repo_gate["status"])
        self.assertEqual("paper_inline", repo_gate["repository_link_origin"])
        self.assertEqual("paper_linked", repo_gate["origin_confidence"])
        self.assertEqual("paper_inline", by_type["code_repository"]["companions"]["repository_link_origin"])

    # --- Provider metadata publisher page ------------------------------------

    def test_provider_metadata_landing_page_becomes_publisher_page(self):
        metadata = {"primary_location": {"landing_page_url": "https://aclanthology.org/2024.acl-long.1"}}
        normalized = self.normalized_doc("paper:oa", links=[], body="body")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir), manifest=[self.paper_record("paper:oa", metadata=metadata)], normalized=normalized
            )
            code, stdout, _ = self.run_companions(
                workspace, "--source-id", "paper:oa", "--no-github", "--no-search"
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        provider_cands = [c for c in report["candidates"] if c["evidence_origin"] == "provider_metadata"]
        self.assertEqual(1, len(provider_cands))
        self.assertEqual("aclanthology.org", provider_cands[0]["companions"]["host"])

    def test_provider_metadata_repository_records_metadata_link_origin(self):
        metadata = {"primary_location": {"landing_page_url": "https://github.com/acme/rag-toolkit"}}
        normalized = self.normalized_doc("paper:oa", links=[], body="body")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir), manifest=[self.paper_record("paper:oa", metadata=metadata)], normalized=normalized
            )
            code, stdout, _ = self.run_companions(
                workspace, "--source-id", "paper:oa", "--no-github", "--no-search"
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        candidate = report["candidates"][0]
        self.assertEqual("code_repository", candidate["source_type"])
        self.assertEqual("provider_metadata", candidate["companions"]["repository_link_origin"])
        gate = candidate["quality_gates"]["companion_repository"]
        self.assertEqual("review_required", gate["status"])
        self.assertEqual("provider_metadata", gate["repository_link_origin"])
        self.assertEqual("provider_metadata", gate["origin_confidence"])
        self.assertTrue(gate["review_required"])

    # --- Phase 2: GitHub dedupe against inline (inline wins) -----------------

    def test_github_search_dedupes_against_inline_link_and_inline_outranks(self):
        # Inline already cites acme/rag-toolkit; GitHub search returns it again
        # plus a second repo. The inline one wins; the search duplicate collapses.
        links = ["https://github.com/acme/rag-toolkit", "https://zenodo.org/records/1"]
        normalized = self.normalized_doc("paper:rag", links=links, body="")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            self.install_github(github_payload(repo("acme/rag-toolkit"), repo("other/rag-impl")))
            code, stdout, stderr = self.run_companions(
                workspace, "--source-id", "paper:rag", "--no-search"
            )
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertTrue(report["network_io_executed"])  # github phase ran
        github_urls = [c["url"] for c in report["candidates"]]
        # acme/rag-toolkit appears exactly once.
        self.assertEqual(1, sum("acme/rag-toolkit" in u for u in github_urls), github_urls)
        acme = next(c for c in report["candidates"] if "acme/rag-toolkit" in c["url"])
        self.assertEqual("paper_inline", acme["evidence_origin"])  # inline won the dedupe
        other = next(c for c in report["candidates"] if "other/rag-impl" in c["url"])
        self.assertEqual("github_search", other["evidence_origin"])
        self.assertIn("github", other)  # rich github metadata block retained
        self.assertEqual("github_search", other["companions"]["repository_link_origin"])
        gate = other["quality_gates"]["companion_repository"]
        self.assertEqual("review_required", gate["status"])
        self.assertEqual("github_search", gate["repository_link_origin"])
        self.assertEqual("search_only", gate["origin_confidence"])
        # Inline origin ranks before github_search.
        self.assertLess(report["candidates"].index(acme), report["candidates"].index(other))

    def test_no_github_flag_skips_github_phase(self):
        links = ["https://github.com/acme/rag-toolkit"]
        normalized = self.normalized_doc("paper:rag", links=links, body="")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            # If github ran, the throwing transport would fail the test.
            DISCOVER.GITHUB_TRANSPORT = lambda *a: (_ for _ in ()).throw(AssertionError("github should not run"))
            code, stdout, _ = self.run_companions(
                workspace, "--source-id", "paper:rag", "--no-github", "--no-search"
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        github_phase = next(p for p in report["phases"] if p["phase"] == "github")
        self.assertTrue(github_phase["skipped"])
        self.assertFalse(report["network_io_executed"])

    # --- Phase 3: search -----------------------------------------------------

    def test_search_phase_skipped_with_warning_when_no_provider(self):
        normalized = self.normalized_doc("paper:rag", links=["https://github.com/acme/rag-toolkit"], body="")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            self.install_github(github_payload())
            code, stdout, _ = self.run_companions(workspace, "--source-id", "paper:rag")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("no_search_provider", codes)
        search_phase = next(p for p in report["phases"] if p["phase"] == "search")
        self.assertFalse(search_phase["network_io_executed"])

    def test_search_phase_emits_search_origin_candidates_via_fixture_provider(self):
        normalized = self.normalized_doc("paper:rag", links=[], body="")
        search_results = [
            {"url": "https://zenodo.org/records/99921", "title": "RAG evaluation dataset"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.paper_record("paper:rag")],
                normalized=normalized,
                search_provider=True,
                search_results=search_results,
            )
            self.install_github(github_payload())
            code, stdout, _ = self.run_companions(workspace, "--source-id", "paper:rag", "--no-github")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        search_cands = [c for c in report["candidates"] if c["evidence_origin"] == "search"]
        self.assertEqual(1, len(search_cands))
        self.assertEqual("dataset", search_cands[0]["source_type"])
        self.assertEqual("primary_non_official", search_cands[0]["trust_tier"])

    def test_search_generic_host_is_secondary_unknown(self):
        normalized = self.normalized_doc("paper:rag", links=[], body="")
        search_results = [{"url": "https://blog.example.com/rag-notes", "title": "RAG notes"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.paper_record("paper:rag")],
                normalized=normalized,
                search_provider=True,
                search_results=search_results,
            )
            code, stdout, _ = self.run_companions(
                workspace, "--source-id", "paper:rag", "--no-github"
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        c = report["candidates"][0]
        self.assertEqual("project_page", c["source_type"])
        self.assertEqual("secondary_unknown", c["trust_tier"])

    def test_search_only_repository_records_search_origin_quality_gate(self):
        normalized = self.normalized_doc("paper:rag", links=[], body="")
        search_results = [{"url": "https://github.com/search-only/rag-toolkit", "title": "RAG toolkit code"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.paper_record("paper:rag")],
                normalized=normalized,
                search_provider=True,
                search_results=search_results,
            )
            code, stdout, _ = self.run_companions(
                workspace, "--source-id", "paper:rag", "--no-github"
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        c = report["candidates"][0]
        self.assertEqual("code_repository", c["source_type"])
        self.assertEqual("search", c["companions"]["repository_link_origin"])
        gate = c["quality_gates"]["companion_repository"]
        self.assertEqual("review_required", gate["status"])
        self.assertEqual("search", gate["repository_link_origin"])
        self.assertEqual("search_only", gate["origin_confidence"])
        self.assertTrue(gate["review_required"])

    # --- Composite ranking inline > github_search > search -------------------

    def test_ranking_paper_inline_before_github_before_search(self):
        links = ["https://github.com/acme/rag-toolkit"]  # paper_inline
        normalized = self.normalized_doc("paper:rag", links=links, body="")
        search_results = [{"url": "https://blog.example.com/rag-notes", "title": "RAG notes"}]  # search
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.paper_record("paper:rag")],
                normalized=normalized,
                search_provider=True,
                search_results=search_results,
            )
            self.install_github(github_payload(repo("other/rag-impl")))  # github_search
            code, stdout, _ = self.run_companions(workspace, "--source-id", "paper:rag")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        order = [c["evidence_origin"] for c in report["candidates"]]
        self.assertIn("paper_inline", order)
        self.assertIn("github_search", order)
        self.assertIn("search", order)
        self.assertLess(order.index("paper_inline"), order.index("github_search"))
        self.assertLess(order.index("github_search"), order.index("search"))

    # --- Origin, gate, idempotency, errors -----------------------------------

    def test_request_id_is_carried_onto_candidates(self):
        links = ["https://zenodo.org/records/1"]
        normalized = self.normalized_doc("paper:rag", links=links, body="")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            code, stdout, _ = self.run_companions(
                workspace, "--source-id", "paper:rag", "--request-id", "req-abc",
                "--no-github", "--no-search",
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual("req-abc", report["request_id"])
        self.assertTrue(all(c["request_id"] == "req-abc" for c in report["candidates"]))

    def test_disabled_discovery_refuses(self):
        normalized = self.normalized_doc("paper:rag", links=[], body="")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized, enabled=False
            )
            code, _, stderr = self.run_companions(workspace, "--source-id", "paper:rag")
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])

    def test_unknown_source_id_is_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")])
            code, _, stderr = self.run_companions(workspace, "--source-id", "paper:missing", "--no-github", "--no-search")
        self.assertEqual(2, code)
        self.assertEqual("SOURCE_UNKNOWN", json.loads(stderr)["error_code"])

    def test_re_run_does_not_duplicate_candidates(self):
        links = ["https://zenodo.org/records/1", "https://github.com/acme/rag-toolkit"]
        normalized = self.normalized_doc("paper:rag", links=links, body="")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            self.install_github(github_payload())
            code1, out1, _ = self.run_companions(workspace, "--source-id", "paper:rag")
            code2, out2, _ = self.run_companions(workspace, "--source-id", "paper:rag")
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            lines = [ln for ln in store.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(0, code1)
        self.assertEqual(0, code2)
        first = json.loads(out1)
        second = json.loads(out2)
        self.assertTrue(first["written"] >= 1)
        self.assertEqual(0, second["written"])
        self.assertEqual(len(lines), first["count"])

    def test_candidates_are_review_required_and_seed_linked(self):
        links = ["https://zenodo.org/records/1"]
        normalized = self.normalized_doc("paper:rag", links=links, body="")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            code, stdout, _ = self.run_companions(
                workspace, "--source-id", "paper:rag", "--no-github", "--no-search"
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertTrue(report["candidates"])
        for c in report["candidates"]:
            self.assertEqual("review", c["recommended_action"])
            self.assertEqual("paper:rag", c["seed_source_id"])
            self.assertEqual("companions", c["provider"])

    # --- F1: bounded multi-input query plan ----------------------------------

    def test_query_plan_uses_title_author_identifier_and_project_name(self):
        # A colon title yields a project/system name; the lead author surname and
        # the arXiv id scope additional bounded queries beyond the title alone.
        normalized = self.normalized_doc(
            "paper:rag", title="RAGKit: A Retrieval Toolkit", arxiv_id="2005.11401", links=[], body=""
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.paper_record("paper:rag")],
                normalized=normalized,
                search_provider=True,
                search_results=[],
            )
            self.install_github(github_payload())
            code, stdout, stderr = self.run_companions(workspace, "--source-id", "paper:rag")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        gh_queries = [entry["query"] for entry in report["query_plan"]["github"]]
        se_queries = [entry["query"] for entry in report["query_plan"]["search"]]
        self.assertIn("RAGKit: A Retrieval Toolkit", gh_queries)               # title
        self.assertIn("RAGKit", gh_queries)                                     # project/system name
        self.assertTrue(any("Lovelace" in q for q in gh_queries), gh_queries)  # lead author surname
        self.assertTrue(any("Lovelace" in q for q in se_queries), se_queries)  # author-scoped search
        self.assertTrue(any("2005.11401" in q for q in se_queries), se_queries)  # identifier-scoped
        self.assertTrue(any(q.lower().endswith("dataset") for q in se_queries), se_queries)
        # Bounded: never more than the per-phase cap of queries.
        self.assertLessEqual(len(gh_queries), 3)
        self.assertLessEqual(len(se_queries), 3)
        # The github phase echoes exactly the planned queries it executed.
        github_phase = next(p for p in report["phases"] if p["phase"] == "github")
        self.assertEqual(gh_queries, github_phase["queries"])

    def test_github_phase_issues_multiple_planned_queries(self):
        # The planned queries (title, project name, author-scoped) actually reach
        # the GitHub adapter — proving recall is widened beyond the title alone.
        normalized = self.normalized_doc(
            "paper:rag", title="RAGKit: A Retrieval Toolkit", arxiv_id="2005.11401", links=[], body=""
        )
        captured: list[str] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[self.paper_record("paper:rag")], normalized=normalized)
            DISCOVER.GITHUB_TRANSPORT = lambda url, timeout, headers: (captured.append(url) or github_payload())
            DISCOVER.GITHUB_CLOCK = lambda: 0.0
            DISCOVER.GITHUB_SLEEP = lambda _seconds: None
            DISCOVER.GITHUB_LAST_REQUEST_AT = None
            code, _, stderr = self.run_companions(workspace, "--source-id", "paper:rag", "--no-search")
        self.assertEqual(0, code, stderr)
        self.assertGreaterEqual(len(captured), 2)                       # more than one query issued
        self.assertEqual(len(captured), len(set(captured)))            # and they are distinct
        self.assertTrue(any("Lovelace" in url for url in captured), captured)  # author surname reached the API

    def test_fixture_search_phase_reports_no_network(self):
        # A local fixture/command backend does no network; the per-phase flag must
        # reflect the adapter, not merely that a provider was configured.
        normalized = self.normalized_doc("paper:rag", links=[], body="")
        search_results = [{"url": "https://zenodo.org/records/1", "title": "data"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[self.paper_record("paper:rag")],
                normalized=normalized,
                search_provider=True,
                search_results=search_results,
            )
            code, stdout, _ = self.run_companions(workspace, "--source-id", "paper:rag", "--no-github")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        search_phase = next(p for p in report["phases"] if p["phase"] == "search")
        self.assertTrue(search_phase["provider_configured"])
        self.assertFalse(search_phase["network_io_executed"])   # fixture backend = local read
        self.assertFalse(report["network_io_executed"])         # no github + fixture search = no network


if __name__ == "__main__":
    unittest.main()
