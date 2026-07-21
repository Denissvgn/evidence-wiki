"""Tests for plan-fetch using explicitly selected discovery candidates (E36-T02).

`source_requests.py plan-fetch` folds in discovery candidates that were selected
for a request (discover_sources.py candidates select, E36-T01). For each selected
candidate it emits an explicit acquisition route by candidate type — a provider
fetch command for arXiv/OpenAlex/GitHub/web candidates, or a manual-delivery target
for manual-only / dataset candidates — and warns when a candidate's trust
tier is below the request's required threshold. Planning never fetches:
`network_io_executed` stays false and the artifacts are never mutated.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

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


REQUESTS = load_script_module("plan_fetch_candidates_under_test", "source_requests.py")


def candidate(candidate_id, request_id, **fields) -> dict:
    base = {
        "candidate_id": candidate_id,
        "provider": "search",
        "url": "https://example.org/x",
        "title": candidate_id,
        "source_type": "web_page",
        "trust_tier": "official_primary",
        "official_source": True,
        "recommended_action": "fetch",
        "status": "selected",
        "selected_request_id": request_id,
    }
    base.update(fields)
    return base


class PlanFetchCandidatesTests(unittest.TestCase):
    def write_workspace(
        self,
        root: Path,
        *,
        candidates: list[dict],
        request_kind: str = "web",
        request_extra: dict | None = None,
        acquisition_enabled: bool = False,
        providers: list[str] | None = None,
    ) -> tuple[Path, str]:
        workspace = root / "ws"
        (workspace / "sources" / "discovery").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: plan-fetch-candidates",
            "sources:",
            "  source_requests_path: sources/source-requests.jsonl",
            "integrations:",
            "  acquisition:",
            f"    enabled: {'true' if acquisition_enabled else 'false'}",
            f"    providers: [{', '.join(providers or [])}]",
        ]
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")

        request_id = "req-test01"
        request = {
            "schema_version": "1.0",
            "request_id": request_id,
            "kind": request_kind,
            "query_or_identifier": "clean air act statute",
            "rationale": "x",
            "priority": "high",
            "question_slugs": [],
            "status": "open",
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
            "source_id": None,
        }
        request.update(request_extra or {})
        (workspace / "sources" / "source-requests.jsonl").write_text(
            json.dumps(request) + "\n", encoding="utf-8"
        )
        store = workspace / "sources" / "discovery" / "candidates.jsonl"
        store.write_text("".join(json.dumps(c) + "\n" for c in candidates), encoding="utf-8")
        return workspace, request_id

    def run_plan(
        self,
        workspace: Path,
        request_id: str,
        *,
        candidate_ids: list[str] | None = None,
    ) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "plan-fetch", "--request-id", request_id, "--format", "json"]
        for candidate_id in candidate_ids or []:
            argv.extend(["--candidate-id", candidate_id])
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = REQUESTS.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def plan(
        self,
        workspace: Path,
        request_id: str,
        *,
        candidate_ids: list[str] | None = None,
    ) -> dict:
        code, stdout, stderr = self.run_plan(
            workspace,
            request_id,
            candidate_ids=candidate_ids,
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def routes_by_id(self, report: dict) -> dict:
        return {route["candidate_id"]: route for route in report["candidate_routes"]}

    def write_coverage_manifest(self, workspace: Path, request_id: str, facets: list[dict]) -> Path:
        document = {
            "schema_version": "1.0",
            "question_slug": "which-benchmarks",
            "created_at": "2026-06-30T00:00:00Z",
            "updated_at": "2026-06-30T00:00:00Z",
            "coverage_profile": "plan-fetch-policy",
            "coverage_verdict": "pending",
            "required_facets": [
                {
                    "facet_id": facet["facet_id"],
                    "description": facet.get("description", f"Facet {facet['facet_id']}"),
                    "required": True,
                    "evidence_path": facet["evidence_path"],
                    "source_policy": facet["source_policy"],
                    "freshness_policy": facet.get("freshness_policy", "manual_review"),
                    "identity_policy": facet.get("identity_policy", "origin_url_matches_candidate"),
                    "min_sources": 1,
                    "accepted_source_ids": [],
                    "blocking_request_ids": [request_id],
                    "facet_verdict": "blocked",
                }
                for facet in facets
            ],
            "optional_facets": [],
        }
        path = workspace / "sources" / "coverage" / "which-benchmarks.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    # --- route construction per candidate type --------------------------------

    def test_github_candidate_suggests_repo_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-gh", "req-test01", provider="github", source_type="code_repository",
                url="https://github.com/acme/rag-toolkit", trust_tier="primary_non_official",
                official_source=False, recommended_action="review",
            )
            workspace, request_id = self.write_workspace(
                Path(tmpdir), candidates=[cand], acquisition_enabled=True, providers=["github"]
            )
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-gh"]
        self.assertEqual([], report["routes"])
        self.assertEqual("selected_candidates", report["routing_basis"])
        self.assertEqual("github", route["provider"])
        self.assertEqual("repo-metadata", route["route"])
        self.assertTrue(route["provider_backed"])
        self.assertTrue(route["allowed_by_config"])
        self.assertEqual(
            ["python3", "scripts/fetch_sources.py", "--format", "json", "github",
             "repo-metadata", "--url", "https://github.com/acme/rag-toolkit", "--request-id", request_id,
             "--candidate-id", "cand-gh"],
            route["command_argv"],
        )
        self.assertIsNone(route["manual_delivery"])

    def test_github_candidate_recognized_by_url_without_code_source_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-gh2", "req-test01", source_type="web_page", url="https://github.com/x/y")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        self.assertEqual("github", self.routes_by_id(report)["cand-gh2"]["provider"])

    def test_versioned_arxiv_paper_candidate_suggests_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-paper", "req-test01", source_type="paper",
                url="https://arxiv.org/abs/2601.00001v2", trust_tier="secondary_reputable", official_source=None,
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-paper"]
        self.assertEqual("arxiv", route["provider"])
        self.assertEqual("download-source", route["route"])
        self.assertIn("2601.00001v2", route["command"])
        self.assertEqual(
            ["python3", "scripts/fetch_sources.py", "--format", "json", "arxiv",
             "download", "--id", "2601.00001v2", "--format", "source", "--request-id", request_id,
             "--candidate-id", "cand-paper"],
            route["command_argv"],
        )
        self.assertEqual(
            ["python3", "scripts/fetch_sources.py", "--format", "json", "arxiv",
             "download", "--id", "2601.00001v2", "--format", "pdf", "--request-id", request_id,
             "--candidate-id", "cand-paper"],
            route["companion_commands"][0]["command_argv"],
        )

    def test_arxiv_paper_metadata_suggests_download_and_preserves_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-paper-meta",
                "req-test01",
                source_type="paper",
                url="https://example.org/not-arxiv",
                trust_tier="secondary_reputable",
                official_source=None,
                license="CC-BY-4.0",
                paper={
                    "provider_ids": {"arxiv": "2601.00001v2", "openalex": None, "doi": None},
                    "title": "Academic Candidate Flow",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "doi": None,
                    "arxiv_id": "2601.00001v2",
                    "open_access": True,
                    "oa_status": "green",
                    "license": "CC-BY-4.0",
                    "landing_page_url": "https://arxiv.org/abs/2601.00001v2",
                    "pdf_url": "https://arxiv.org/pdf/2601.00001v2",
                    "resolution_status": "resolved",
                },
                provider_budget={
                    "provider": "arxiv",
                    "network_io_executed": True,
                    "token_used": False,
                    "max_results": 5,
                    "per_provider_limit": 5,
                },
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-paper-meta"]
        self.assertEqual("arxiv", route["provider"])
        self.assertEqual("download-source", route["route"])
        self.assertEqual(
            ["python3", "scripts/fetch_sources.py", "--format", "json", "arxiv",
             "download", "--id", "2601.00001v2", "--format", "source", "--request-id", request_id,
             "--candidate-id", "cand-paper-meta"],
            route["command_argv"],
        )
        self.assertEqual(
            ["python3", "scripts/fetch_sources.py", "--format", "json", "arxiv",
             "download", "--id", "2601.00001v2", "--format", "pdf", "--request-id", request_id,
             "--candidate-id", "cand-paper-meta"],
            route["companion_commands"][0]["command_argv"],
        )
        self.assertEqual(cand["paper"], route["paper"])
        self.assertTrue(route["candidate_network_io_executed"])
        self.assertEqual(cand["provider_budget"], route["provider_budget"])

    def test_unversioned_arxiv_candidate_suggests_id_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-arxiv0", "req-test01", source_type="paper", url="arXiv:2601.00001")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-arxiv0"]
        self.assertEqual("arxiv", route["provider"])
        self.assertEqual("search-by-id", route["route"])

    def test_doi_candidate_suggests_openalex_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-doi", "req-test01", source_type="paper", url="https://doi.org/10.1234/abcd.efgh")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-doi"]
        self.assertEqual("openalex", route["provider"])
        self.assertEqual("get-by-doi", route["route"])
        self.assertEqual(
            ["python3", "scripts/fetch_sources.py", "--format", "json", "openalex",
             "get", "--id-or-doi", "10.1234/abcd.efgh",
             "--output", f"raw/papers/openalex-{request_id}-cand-doi-metadata.json", "--request-id", request_id,
             "--candidate-id", "cand-doi"],
            route["command_argv"],
        )

    def test_openalex_oa_paper_metadata_suggests_pdf_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-openalex-oa",
                "req-test01",
                provider="openalex",
                source_type="paper",
                url="https://openalex.org/W260100001",
                trust_tier="secondary_reputable",
                official_source=None,
                license="cc-by",
                paper={
                    "provider_ids": {"arxiv": None, "openalex": "W260100001", "doi": "10.5555/example"},
                    "title": "OpenAlex OA Paper",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "doi": "10.5555/example",
                    "arxiv_id": None,
                    "open_access": True,
                    "oa_status": "gold",
                    "license": "cc-by",
                    "landing_page_url": "https://example.org/paper",
                    "pdf_url": "https://example.org/paper.pdf",
                    "resolution_status": "resolved",
                },
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-openalex-oa"]
        self.assertEqual("openalex", route["provider"])
        self.assertEqual("download-pdf", route["route"])
        self.assertEqual(
            ["python3", "scripts/fetch_sources.py", "--format", "json", "openalex",
             "download-pdf", "--work-id", "W260100001", "--request-id", request_id,
             "--candidate-id", "cand-openalex-oa"],
            route["command_argv"],
        )
        self.assertEqual(cand["paper"], route["paper"])

    def test_merged_paper_uses_the_authorized_retained_provider_identity(self):
        merged = candidate(
            "cand-merged-paper",
            "req-test01",
            provider="arxiv",
            source_type="paper",
            url="https://arxiv.org/abs/2601.00001v2",
            paper={
                "provider_ids": {
                    "arxiv": "2601.00001v2",
                    "openalex": "W260100001",
                    "doi": "10.5555/example",
                },
                "title": "Merged Provider Paper",
                "authors": ["Ada Lovelace"],
                "publication_year": 2026,
                "doi": "10.5555/example",
                "arxiv_id": "2601.00001v2",
                "open_access": True,
                "oa_status": "green",
                "license": "cc-by",
                "landing_page_url": "https://arxiv.org/abs/2601.00001v2",
                "pdf_url": "https://arxiv.org/pdf/2601.00001v2",
                "resolution_status": "resolved",
            },
        )
        for enabled_provider, expected_route in (
            ("arxiv", "download-source"),
            ("openalex", "download-pdf"),
        ):
            with self.subTest(provider=enabled_provider), tempfile.TemporaryDirectory() as tmpdir:
                workspace, request_id = self.write_workspace(
                    Path(tmpdir),
                    candidates=[merged],
                    acquisition_enabled=True,
                    providers=[enabled_provider],
                )
                route = self.routes_by_id(self.plan(workspace, request_id))["cand-merged-paper"]

            self.assertEqual(enabled_provider, route["provider"])
            self.assertEqual(expected_route, route["route"])
            self.assertTrue(route["allowed_by_config"])

    def test_openalex_metadata_only_paper_suggests_get_and_warns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-openalex-metadata",
                "req-test01",
                provider="openalex",
                source_type="paper",
                url="https://openalex.org/W260100002",
                trust_tier="secondary_reputable",
                official_source=None,
                license="cc-by",
                paper={
                    "provider_ids": {"arxiv": None, "openalex": "W260100002", "doi": None},
                    "title": "OpenAlex Metadata Only Paper",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2025,
                    "doi": None,
                    "arxiv_id": None,
                    "open_access": False,
                    "oa_status": "closed",
                    "license": "cc-by",
                    "landing_page_url": "https://example.org/paper",
                    "pdf_url": None,
                    "resolution_status": "metadata_only",
                },
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-openalex-metadata"]
        self.assertEqual("openalex", route["provider"])
        self.assertEqual("get", route["route"])
        self.assertEqual(
            [
                "python3",
                "scripts/fetch_sources.py",
                "--format",
                "json",
                "openalex",
                "get",
                "--id-or-doi",
                "W260100002",
                "--output",
                "raw/papers/openalex-W260100002-metadata.json",
                "--request-id",
                request_id,
                "--candidate-id",
                "cand-openalex-metadata",
            ],
            route["command_argv"],
        )
        self.assertTrue(any("cand-openalex-metadata" in warning and "not open access" in warning for warning in report["warnings"]))

    def test_uncertain_openalex_paper_suggests_resolve_and_warns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-openalex-uncertain",
                "req-test01",
                provider="openalex",
                source_type="paper",
                url="https://example.org/no-provider-id",
                trust_tier="secondary_reputable",
                official_source=None,
                license=None,
                paper={
                    "provider_ids": {"arxiv": None, "openalex": None, "doi": None},
                    "title": "Possible TurboQuant Paper",
                    "authors": [],
                    "publication_year": None,
                    "doi": None,
                    "arxiv_id": None,
                    "open_access": None,
                    "oa_status": None,
                    "license": None,
                    "landing_page_url": None,
                    "pdf_url": None,
                    "resolution_status": "uncertain",
                },
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-openalex-uncertain"]
        self.assertEqual("openalex", route["provider"])
        self.assertEqual("resolve", route["route"])
        self.assertIn("Possible TurboQuant Paper", route["command_argv"])
        self.assertTrue(any("cand-openalex-uncertain" in warning and "uncertain" in warning for warning in report["warnings"]))
        self.assertTrue(any("cand-openalex-uncertain" in warning and "license" in warning for warning in report["warnings"]))

    def test_official_legal_candidate_suggests_web_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-legal", "req-test01", provider="legal", source_type="official_legal",
                url="https://www.govinfo.gov/clean-air-act",
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-legal"]
        self.assertEqual("web", route["provider"])
        self.assertEqual("get", route["route"])
        self.assertTrue(route["provider_backed"])
        self.assertFalse(route["allowed_by_config"])
        self.assertIn("web get", route["command"])
        self.assertIsNone(route["manual_delivery"])

    def test_standards_registry_candidate_suggests_selected_web_get_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-iso-19131",
                "req-test01",
                provider="iso-open-data",
                source_type="standards_registry_entry",
                url="https://www.iso.org/standard/77442.html",
                title="ISO 19131:2022",
            )
            for field in ("evidence_path", "source_policy", "freshness_policy", "identity_policy"):
                cand.pop(field, None)
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-iso-19131"]
        self.assertEqual("web", route["provider"])
        self.assertEqual("get", route["route"])
        self.assertTrue(route["provider_backed"])
        self.assertFalse(route["allowed_by_config"])
        self.assertIsNone(route["manual_delivery"])
        self.assertEqual("standards_registry_reference", route["evidence_path"])
        self.assertEqual("official_standards_registry", route["source_policy"])
        self.assertEqual("current_standard_reference", route["freshness_policy"])
        self.assertEqual("standard_designation_matches_registry", route["identity_policy"])
        self.assertIn("--source-type standards_registry_entry", route["command"])

    def test_dataset_candidate_manual_delivery_to_data_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-data", "req-test01", source_type="dataset", url="https://data.example/set")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-data"]
        self.assertEqual("manual", route["provider"])
        self.assertEqual("raw/data/", route["manual_delivery"]["target_root"])

    # --- selection scoping, status, and read-only guarantee -------------------

    def test_only_selected_candidates_for_this_request_are_included(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mine = candidate("cand-mine", "req-test01", source_type="official_legal", url="https://govinfo.gov/a")
            other = candidate("cand-other", "req-OTHER", source_type="official_legal", url="https://govinfo.gov/b")
            unselected = candidate("cand-new", "req-test01", url="https://govinfo.gov/c")
            unselected["status"] = "new"
            unselected.pop("selected_request_id")
            workspace, request_id = self.write_workspace(
                Path(tmpdir), candidates=[mine, other, unselected]
            )
            report = self.plan(workspace, request_id)
        ids = set(self.routes_by_id(report))
        self.assertEqual({"cand-mine"}, ids)
        self.assertEqual(1, report["selected_candidate_count"])

    def test_explicit_candidate_scope_emits_only_the_authorized_selected_route(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            authorized = candidate(
                "cand-authorized",
                "req-test01",
                source_type="paper",
                url="https://arxiv.org/abs/2601.00001v2",
            )
            out_of_scope = candidate(
                "cand-out-of-scope",
                "req-test01",
                source_type="paper",
                url="https://arxiv.org/abs/2601.00002v1",
            )
            workspace, request_id = self.write_workspace(
                Path(tmpdir),
                candidates=[authorized, out_of_scope],
                acquisition_enabled=True,
                providers=["arxiv"],
            )

            report = self.plan(
                workspace,
                request_id,
                candidate_ids=["cand-authorized"],
            )

        self.assertEqual(["cand-authorized"], [route["candidate_id"] for route in report["candidate_routes"]])
        self.assertEqual(1, report["selected_candidate_count"])
        self.assertNotIn("cand-out-of-scope", json.dumps(report, sort_keys=True))

    def test_explicit_candidate_scope_rejects_unknown_unselected_and_request_mismatched_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            selected = candidate("cand-selected", "req-test01")
            unselected = candidate("cand-unselected", "req-test01")
            unselected["status"] = "new"
            mismatched = candidate("cand-other-request", "req-other")
            workspace, request_id = self.write_workspace(
                Path(tmpdir),
                candidates=[selected, unselected, mismatched],
            )

            cases = (
                ("cand-missing", "Unknown candidate id"),
                ("cand-unselected", "requires selected candidates"),
                ("cand-other-request", "linked to request req-other"),
            )
            for candidate_id, expected in cases:
                with self.subTest(candidate_id=candidate_id):
                    code, stdout, stderr = self.run_plan(
                        workspace,
                        request_id,
                        candidate_ids=[candidate_id],
                    )
                    self.assertEqual(REQUESTS.EXIT_INVALID, code)
                    payload = json.loads(stdout or stderr)
                    self.assertIn(expected, payload["message"])

    def test_selected_for_request_id_is_canonical_selection_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-newlink", "legacy-ignored", source_type="official_legal", url="https://govinfo.gov/a")
            cand["selected_for_request_id"] = "req-test01"
            cand.pop("selected_request_id")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        ids = set(self.routes_by_id(report))
        self.assertEqual({"cand-newlink"}, ids)
        self.assertEqual(1, report["selected_candidate_count"])

    def test_candidates_upgrade_unsupported_web_request_to_ready(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-legal", "req-test01", source_type="official_legal", url="https://govinfo.gov/a")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand], request_kind="web")
            report = self.plan(workspace, request_id)
        self.assertEqual("ready", report["plan_status"])
        # The stale "use manual delivery" note must not contradict "ready".
        self.assertNotIn(REQUESTS.UNSUPPORTED_KIND_WARNING, report["warnings"])

    def test_no_candidates_keeps_legacy_behavior(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[], request_kind="web")
            report = self.plan(workspace, request_id)
        self.assertEqual("unsupported", report["plan_status"])
        self.assertEqual([], report["candidate_routes"])
        self.assertEqual(0, report["selected_candidate_count"])

    def test_plan_fetch_never_mutates_artifacts_or_runs_network(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-legal", "req-test01", source_type="official_legal", url="https://govinfo.gov/a")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            reqs = workspace / "sources" / "source-requests.jsonl"
            store_before, reqs_before = store.read_text(), reqs.read_text()
            report = self.plan(workspace, request_id)
            self.assertFalse(report["network_io_executed"])
            self.assertEqual(store_before, store.read_text())
            self.assertEqual(reqs_before, reqs.read_text())

    # --- trust-tier threshold and provider allow-list warnings ----------------

    def test_low_trust_candidate_warns_against_default_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            low = candidate("cand-blog", "req-test01", trust_tier="secondary_unknown", official_source=None, recommended_action="review")
            ok = candidate("cand-ok", "req-test01", trust_tier="official_primary", url="https://govinfo.gov/a")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[low, ok])
            report = self.plan(workspace, request_id)
        self.assertEqual("secondary_reputable", report["min_trust_tier"])
        warned = [w for w in report["warnings"] if "cand-blog" in w and "below the required" in w]
        self.assertEqual(1, len(warned))
        # The official_primary candidate is above threshold -> no threshold warning.
        self.assertFalse(any("cand-ok" in w and "below the required" in w for w in report["warnings"]))

    def test_request_min_trust_tier_override_tightens_threshold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-db", "req-test01", trust_tier="secondary_reputable", official_source=False, recommended_action="review")
            workspace, request_id = self.write_workspace(
                Path(tmpdir), candidates=[cand], request_extra={"min_trust_tier": "official_primary"}
            )
            report = self.plan(workspace, request_id)
        self.assertEqual("official_primary", report["min_trust_tier"])
        self.assertTrue(any("cand-db" in w and "below the required 'official_primary'" in w for w in report["warnings"]))

    def test_rejected_recommendation_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-rej", "req-test01", trust_tier="official_primary", recommended_action="reject", url="https://govinfo.gov/a")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            report = self.plan(workspace, request_id)
        self.assertTrue(any("cand-rej" in w and "reject" in w for w in report["warnings"]))

    def test_acquisition_disabled_warns_even_with_candidate_routes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-gh", "req-test01", source_type="code_repository", url="https://github.com/a/b")
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand], acquisition_enabled=False)
            report = self.plan(workspace, request_id)
        self.assertIn(REQUESTS.ACQUISITION_DISABLED_WARNING, report["warnings"])

    def test_candidate_provider_not_allowlisted_warns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-gh", "req-test01", source_type="code_repository", url="https://github.com/a/b")
            workspace, request_id = self.write_workspace(
                Path(tmpdir), candidates=[cand], acquisition_enabled=True, providers=["arxiv"]
            )
            report = self.plan(workspace, request_id)
        route = self.routes_by_id(report)["cand-gh"]
        self.assertFalse(route["allowed_by_config"])
        self.assertTrue(any("not allow-listed" in w and "github" in w for w in report["warnings"]))

    # --- linked coverage-facet policy context (E42-T02) ------------------------

    def test_policy_facets_attach_to_routes_for_each_evidence_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            candidates = [
                candidate(
                    "cand-arxiv",
                    "req-test01",
                    source_type="paper",
                    url="https://arxiv.org/abs/2601.00001v2",
                    trust_tier="secondary_reputable",
                    evidence_path="academic_method_existence",
                    source_policy="openalex_or_arxiv",
                ),
                candidate(
                    "cand-doi",
                    "req-test01",
                    source_type="paper",
                    url="https://doi.org/10.1234/example",
                    trust_tier="secondary_reputable",
                    evidence_path="academic_method_existence",
                    source_policy="openalex_or_arxiv",
                ),
                candidate(
                    "cand-gh-policy",
                    "req-test01",
                    source_type="code_repository",
                    url="https://github.com/acme/tool",
                    trust_tier="official_primary",
                    evidence_path="github_implementation",
                    source_policy="canonical_repository",
                ),
                candidate(
                    "cand-legal-policy",
                    "req-test01",
                    provider="legal",
                    source_type="official_legal",
                    url="https://www.govinfo.gov/app/details/CFR-2026-title40",
                    trust_tier="official_primary",
                    evidence_path="legal_current_figure",
                    source_policy="official_primary",
                ),
                candidate(
                    "cand-vendor",
                    "req-test01",
                    source_type="web_page",
                    url="https://vendor.example/product/specs",
                    trust_tier="official_primary",
                    evidence_path="vendor_product_spec",
                    source_policy="official_vendor",
                ),
            ]
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=candidates)
            self.write_coverage_manifest(
                workspace,
                request_id,
                [
                    {
                        "facet_id": "academic",
                        "evidence_path": "academic_method_existence",
                        "source_policy": "openalex_or_arxiv",
                        "freshness_policy": "publication_identity",
                        "identity_policy": "citation_id_resolves",
                    },
                    {
                        "facet_id": "github",
                        "evidence_path": "github_implementation",
                        "source_policy": "canonical_repository",
                        "freshness_policy": "release_snapshot",
                        "identity_policy": "repo_ref_resolves",
                    },
                    {
                        "facet_id": "legal",
                        "evidence_path": "legal_current_figure",
                        "source_policy": "official_primary",
                        "freshness_policy": "current_legal_figure",
                        "identity_policy": "official_domain_match",
                    },
                    {
                        "facet_id": "vendor",
                        "evidence_path": "vendor_product_spec",
                        "source_policy": "official_vendor",
                        "freshness_policy": "current_product_spec",
                        "identity_policy": "official_domain_match",
                    },
                ],
            )

            report = self.plan(workspace, request_id)

        routes = self.routes_by_id(report)
        self.assertEqual("coverage_manifest", report["policy_source"])
        self.assertEqual(
            {"academic", "github", "legal", "vendor"},
            {facet["facet_id"] for facet in report["policy_facets"]},
        )
        self.assertEqual("arxiv", routes["cand-arxiv"]["provider"])
        self.assertEqual("openalex", routes["cand-doi"]["provider"])
        self.assertEqual("github", routes["cand-gh-policy"]["provider"])
        self.assertEqual("web", routes["cand-legal-policy"]["provider"])
        self.assertEqual("web", routes["cand-vendor"]["provider"])
        self.assertIsNone(routes["cand-legal-policy"]["manual_delivery"])
        self.assertIsNone(routes["cand-vendor"]["manual_delivery"])
        self.assertEqual(["academic"], [facet["facet_id"] for facet in routes["cand-arxiv"]["policy_facets"]])
        self.assertEqual(["academic"], [facet["facet_id"] for facet in routes["cand-doi"]["policy_facets"]])
        self.assertEqual("secondary_reputable", routes["cand-arxiv"]["policy_min_trust_tier"])
        self.assertEqual("official_primary", routes["cand-gh-policy"]["policy_min_trust_tier"])
        self.assertEqual("matched", routes["cand-vendor"]["policy_alignment"])
        self.assertFalse(any("below linked facet policy" in warning for warning in report["warnings"]))

    def test_linked_facet_policy_warns_when_candidate_trust_tier_is_too_low(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-low-legal",
                "req-test01",
                provider="legal",
                source_type="official_legal",
                url="https://mirror.example/legal",
                trust_tier="secondary_reputable",
                evidence_path="legal_current_figure",
                source_policy="official_primary",
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            self.write_coverage_manifest(
                workspace,
                request_id,
                [{"facet_id": "legal", "evidence_path": "legal_current_figure", "source_policy": "official_primary"}],
            )

            report = self.plan(workspace, request_id)

        route = self.routes_by_id(report)["cand-low-legal"]
        self.assertEqual("below_min_trust", route["policy_alignment"])
        self.assertEqual("official_primary", route["policy_min_trust_tier"])
        self.assertTrue(any("cand-low-legal" in warning and "below linked facet policy" in warning for warning in report["warnings"]))

    def test_linked_facet_policy_warns_when_candidate_evidence_path_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-vendor-mismatch",
                "req-test01",
                source_type="web_page",
                url="https://vendor.example/product/specs",
                trust_tier="official_primary",
                evidence_path="vendor_product_spec",
                source_policy="official_vendor",
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            self.write_coverage_manifest(
                workspace,
                request_id,
                [{"facet_id": "legal", "evidence_path": "legal_current_figure", "source_policy": "official_primary"}],
            )

            report = self.plan(workspace, request_id)

        route = self.routes_by_id(report)["cand-vendor-mismatch"]
        self.assertEqual("no_matching_evidence_path", route["policy_alignment"])
        self.assertIsNone(route["policy_min_trust_tier"])
        self.assertEqual([], route["policy_facets"])
        self.assertTrue(
            any("cand-vendor-mismatch" in warning and "does not match any linked coverage facet" in warning for warning in report["warnings"])
        )

    def test_no_linked_coverage_manifest_preserves_request_min_trust_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate("cand-old", "req-test01", source_type="official_legal", url="https://govinfo.gov/a")
            for field in ("evidence_path", "source_policy", "freshness_policy", "identity_policy"):
                cand.pop(field, None)
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])

            report = self.plan(workspace, request_id)

        route = self.routes_by_id(report)["cand-old"]
        self.assertEqual("request_min_trust_tier", report["policy_source"])
        self.assertEqual([], report["policy_facets"])
        self.assertEqual("request_min_trust_tier", route["policy_alignment"])
        self.assertEqual("secondary_reputable", route["policy_min_trust_tier"])
        self.assertEqual([], route["policy_facets"])
        self.assertEqual("legal_current_figure", route["evidence_path"])

    def test_plan_fetch_policy_context_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cand = candidate(
                "cand-legal-readonly",
                "req-test01",
                source_type="official_legal",
                url="https://govinfo.gov/a",
                evidence_path="legal_current_figure",
                source_policy="official_primary",
            )
            workspace, request_id = self.write_workspace(Path(tmpdir), candidates=[cand])
            log = workspace / "log.md"
            log.write_text("# Log\n", encoding="utf-8")
            coverage = self.write_coverage_manifest(
                workspace,
                request_id,
                [{"facet_id": "legal", "evidence_path": "legal_current_figure", "source_policy": "official_primary"}],
            )
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            reqs = workspace / "sources" / "source-requests.jsonl"
            before = {
                store: store.read_text(encoding="utf-8"),
                reqs: reqs.read_text(encoding="utf-8"),
                log: log.read_text(encoding="utf-8"),
                coverage: coverage.read_text(encoding="utf-8"),
            }

            report = self.plan(workspace, request_id)

            self.assertFalse(report["network_io_executed"])
            for path, text in before.items():
                self.assertEqual(text, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
