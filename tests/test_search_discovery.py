"""Tests for the general search discovery provider (E33-T01) and the reasoned
query planner (E33-T02).

`discover_sources.py search --query TEXT` plans a small, bounded set of explained
queries from a research need (read-only, no network). With `--execute` it runs the
planned queries through a configured, provider-neutral backend (fixture, command,
or HTTP) and normalizes results into `source_candidate` records — never raw
provider dumps. No commercial API is hard-coded; an HTTP backend needs an explicit
endpoint, and execution with no provider configured refuses.

These tests cover the planner (distinct plans per intent, planned-query fields,
intent inference, plan-without-provider) and execution across all three backends
(normalization, domain filtering, dedup, bounded limits, idempotency, refusals,
and no-network behavior for the local providers).
"""

import contextlib
import importlib.util
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"

# The required source_candidate fields a normalized search candidate must carry,
# so a search backend plugs in without changing the candidate schema.
REQUIRED_CANDIDATE_FIELDS = (
    "schema_version", "candidate_id", "request_id", "seed_source_id", "discovery_run_id",
    "discovered_at", "discovered_by", "provider", "url", "title", "source_type",
    "trust_tier", "relevance_score", "trust_score", "official_source", "jurisdiction",
    "license", "terms_url", "rationale", "recommended_action", "network_io_executed", "reasoning",
    "evidence_path", "source_policy", "freshness_policy", "identity_policy",
    "selected_for_request_id", "selected_at",
)

# Fields the planner must record for every planned query (E33-T02).
PLANNED_QUERY_FIELDS = ("query", "expected_source_type", "domain_allowlist", "domain_blocklist", "rationale")


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discover_sources_search_under_test", "discover_sources.py")


def result(title: str, url: str, *, snippet: str = "", published: str | None = None) -> dict:
    record: dict = {"title": title, "url": url}
    if snippet:
        record["snippet"] = snippet
    if published:
        record["published"] = published
    return record


def http_body(*results: dict) -> bytes:
    return json.dumps({"results": list(results)}).encode("utf-8")


class SearchTestBase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._reset_transport)

    def _reset_transport(self):
        DISCOVER.SEARCH_HTTP_TRANSPORT = None

    def write_workspace(self, root: Path, search_block: list[str] | None, *, enabled: bool = True) -> Path:
        workspace = root / "workspace"
        (workspace / "sources" / "discovery" / "fixtures").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: search-discovery-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "  source_requests_path: sources/source-requests.jsonl",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
        ]
        if search_block is not None:
            lines.append("    search:")
            lines.extend(f"      {line}" for line in search_block)
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return workspace

    def write_fixture(self, workspace: Path, *results: dict, name: str = "results.jsonl") -> str:
        rel = f"sources/discovery/fixtures/{name}"
        (workspace / rel).write_text(
            "".join(json.dumps(record) + "\n" for record in results), encoding="utf-8"
        )
        return rel

    def fixture_workspace(self, root: Path, *results: dict, **kwargs) -> Path:
        workspace = self.write_workspace(
            root,
            ["provider: fixture", "fixture_path: sources/discovery/fixtures/results.jsonl"],
            **kwargs,
        )
        self.write_fixture(workspace, *results)
        return workspace

    def write_request(self, workspace: Path, request_id: str, kind: str) -> None:
        path = workspace / "sources" / "source-requests.jsonl"
        line = json.dumps({"request_id": request_id, "kind": kind, "status": "open"})
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def run_search(self, workspace: Path, *args: str, execute: bool = False) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "search", *args]
        if execute:
            argv.append("--execute")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def store_records(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "discovery" / "candidates.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def install_http(self, transport) -> None:
        DISCOVER.SEARCH_HTTP_TRANSPORT = transport


class SearchQueryPlannerTests(SearchTestBase):
    """E33-T02: planning is the read-only default; --execute runs the plan."""

    def test_default_plan_is_single_general_web_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            code, stdout, stderr = self.run_search(workspace, "--query", "carbon capture efficiency")
            stored = self.store_records(workspace)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("plan", report["mode"])
        self.assertEqual("web", report["intent"])
        self.assertEqual("carbon capture efficiency", report["research_need"])
        self.assertFalse(report["network_io_executed"])
        self.assertEqual(1, report["planned_query_count"])
        self.assertEqual("carbon capture efficiency", report["planned_queries"][0]["query"])
        self.assertEqual("web_page", report["planned_queries"][0]["expected_source_type"])
        # Planning writes nothing to the candidate store.
        self.assertEqual([], stored)

    def test_intents_produce_distinct_plans(self):
        plans = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            for intent in ("paper", "code", "dataset", "legal", "web"):
                code, stdout, _ = self.run_search(workspace, "--query", "topic", "--intent", intent)
                self.assertEqual(0, code)
                plans[intent] = json.loads(stdout)["planned_queries"]

        def types(intent):
            return {q["expected_source_type"] for q in plans[intent]}

        self.assertEqual({"paper"}, types("paper"))
        self.assertEqual({"code_repository"}, types("code"))
        self.assertEqual({"dataset"}, types("dataset"))
        self.assertEqual({"official_legal"}, types("legal"))
        self.assertEqual({"web_page"}, types("web"))
        # Distinct query texts per intent (not just one shared plan).
        signatures = {intent: tuple(q["query"] for q in queries) for intent, queries in plans.items()}
        self.assertEqual(len(set(signatures.values())), len(signatures), "every intent must plan distinct queries")

    def test_legal_plan_uses_official_terms_and_prefers_official(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            code, stdout, _ = self.run_search(
                workspace, "--query", "emissions reporting", "--jurisdiction", "us-federal"
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual("legal", report["intent"])
        self.assertEqual("us-federal", report["jurisdiction"])
        queries = report["planned_queries"]
        self.assertTrue(all(q["expected_source_type"] == "official_legal" for q in queries))
        self.assertTrue(all(q.get("prefer_official") is True for q in queries))
        joined = " ".join(q["query"] for q in queries)
        for term in ("statute", "regulation", "administrative rule", "agency guidance", "court opinion", "official gazette"):
            self.assertIn(term, joined)

    def test_every_planned_query_records_required_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            code, stdout, _ = self.run_search(
                workspace, "--query", "topic", "--intent", "paper",
                "--domain-allow", "arxiv.org", "--domain-block", "spam.example",
            )
        self.assertEqual(0, code)
        for planned in json.loads(stdout)["planned_queries"]:
            for field in PLANNED_QUERY_FIELDS:
                self.assertIn(field, planned)
            # User inclusion/exclusion domains flow into each planned query.
            self.assertEqual(["arxiv.org"], planned["domain_allowlist"])
            self.assertEqual(["spam.example"], planned["domain_blocklist"])
            self.assertTrue(planned["rationale"].strip())

    def test_request_kind_drives_intent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            self.write_request(workspace, "req-paper00001", "paper")
            self.write_request(workspace, "req-code000001", "code")
            self.write_request(workspace, "req-data000001", "dataset")
            intents = {}
            for request_id in ("req-paper00001", "req-code000001", "req-data000001"):
                code, stdout, _ = self.run_search(workspace, "--query", "topic", "--request-id", request_id)
                self.assertEqual(0, code)
                report = json.loads(stdout)
                intents[request_id] = report["intent"]
                self.assertEqual(request_id, report["request_id"])
        self.assertEqual("paper", intents["req-paper00001"])
        self.assertEqual("code", intents["req-code000001"])
        self.assertEqual("dataset", intents["req-data000001"])

    def test_unknown_request_id_is_recorded_without_error(self):
        # Lenient like github discovery: an unknown request_id is still linked; it
        # just does not drive intent (falls back to the general web intent).
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            code, stdout, _ = self.run_search(workspace, "--query", "topic", "--request-id", "req-missing00")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual("req-missing00", report["request_id"])
        self.assertIsNone(report["request_kind"])
        self.assertEqual("web", report["intent"])

    def test_explicit_intent_overrides_request_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            self.write_request(workspace, "req-paper00001", "paper")
            code, stdout, _ = self.run_search(
                workspace, "--query", "topic", "--request-id", "req-paper00001", "--intent", "code"
            )
        self.assertEqual(0, code)
        self.assertEqual("code", json.loads(stdout)["intent"])

    def test_plan_works_without_provider_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            code, stdout, _ = self.run_search(workspace, "--query", "topic", "--intent", "paper")
        self.assertEqual(0, code, "planning must not require a search provider")
        self.assertEqual("plan", json.loads(stdout)["mode"])

    def test_plan_disabled_discovery_refuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None, enabled=False)
            code, _, stderr = self.run_search(workspace, "--query", "topic")
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])

    def test_plan_opens_no_socket(self):
        def forbid_socket(*args, **kwargs):  # pragma: no cover - only fires on a bug
            raise AssertionError("query planning must not open a network socket")

        original = socket.socket
        socket.socket = forbid_socket
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.write_workspace(Path(tmpdir), None)
                code, _, _ = self.run_search(workspace, "--query", "topic", "--intent", "legal")
        finally:
            socket.socket = original
        self.assertEqual(0, code)

    def test_execute_runs_all_planned_queries(self):
        # A command provider that echoes each query as a distinct URL proves every
        # planned query in the paper plan (3 queries) is executed and aggregated.
        program = (
            "import json,sys,hashlib;q=sys.argv[-1];"
            "print(json.dumps([{'title':q,'url':'https://example.org/'+hashlib.sha1(q.encode()).hexdigest()[:8]}]))"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: command", f'command: [{sys.executable!r}, "-c", {program!r}]'],
            )
            code, stdout, stderr = self.run_search(workspace, "--query", "topic", "--intent", "paper", execute=True)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("execute", report["mode"])
        self.assertEqual(3, report["planned_query_count"])
        # Three distinct planned queries -> three distinct executed candidates.
        self.assertEqual(3, report["count"])


class SearchExecutionTests(SearchTestBase):
    """E33-T01 backend behavior, reached through `--execute`."""

    def test_fixture_results_normalize_to_candidates_and_write_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Official CFR", "https://www.govinfo.gov/app/collection/cfr", snippet="Code of Federal Regulations"),
            )
            code, stdout, stderr = self.run_search(workspace, "--query", "code of federal regulations", execute=True)
            stored = self.store_records(workspace)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        report = json.loads(stdout)
        self.assertEqual("search", report["provider"])
        self.assertEqual("execute", report["mode"])
        self.assertEqual("fixture", report["search_provider"])
        self.assertEqual(1, report["count"])
        self.assertEqual(1, report["written"])
        self.assertFalse(report["network_io_executed"])
        self.assertEqual("sources/discovery/candidates.jsonl", report["candidates_path"])

        candidate = report["candidates"][0]
        for field in REQUIRED_CANDIDATE_FIELDS:
            self.assertIn(field, candidate)
        self.assertEqual("search", candidate["provider"])
        self.assertEqual("web_page", candidate["source_type"])
        # E33-T03: govinfo.gov is recognized as an official source via the .gov
        # top-level domain, so it is classified official_primary with an exact
        # phrase match and recommended for fetch (clean official, no risk flags).
        self.assertEqual("official_primary", candidate["trust_tier"])
        self.assertEqual("fetch", candidate["recommended_action"])
        self.assertIs(True, candidate["official_source"])
        self.assertFalse(candidate["network_io_executed"])
        self.assertEqual([], candidate["reasoning"]["risk_flags"])
        self.assertTrue(candidate["reasoning"]["matched_query_terms"])
        self.assertTrue(candidate["search"]["exact_phrase"])
        self.assertEqual("govinfo.gov", candidate["search"]["host"])
        self.assertEqual([candidate["candidate_id"]], [r["candidate_id"] for r in stored])

    def test_request_id_links_candidates_else_discovery_run_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(Path(tmpdir), result("A", "https://example.org/a"))
            code, stdout, _ = self.run_search(
                workspace, "--query", "thing", "--request-id", "req-1a2b3c4d5e", execute=True
            )
            linked = json.loads(stdout)["candidates"][0]

            workspace2 = self.fixture_workspace(Path(tmpdir) / "two", result("A", "https://example.org/a"))
            _, stdout2, _ = self.run_search(workspace2, "--query", "thing", execute=True)
            exploratory = json.loads(stdout2)["candidates"][0]

        self.assertEqual(0, code)
        self.assertEqual("req-1a2b3c4d5e", linked["request_id"])
        self.assertIsNone(linked["discovery_run_id"])
        self.assertIsNone(exploratory["request_id"])
        self.assertRegex(exploratory["discovery_run_id"], r"^disc-[0-9a-f]{10}$")

    def test_jurisdiction_passes_through_to_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(Path(tmpdir), result("A", "https://example.org/a"))
            code, stdout, _ = self.run_search(
                workspace, "--query", "thing", "--jurisdiction", "us-federal", execute=True
            )
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual("us-federal", report["jurisdiction"])
        self.assertEqual("us-federal", report["candidates"][0]["jurisdiction"])

    def test_max_results_caps_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                *[result(f"R{i}", f"https://example.org/{i}") for i in range(10)],
            )
            code, stdout, _ = self.run_search(workspace, "--query", "thing", "--max-results", "3", execute=True)
        self.assertEqual(0, code)
        self.assertEqual(3, json.loads(stdout)["count"])

    def test_domain_allowlist_keeps_only_matching_hosts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("gov", "https://www.govinfo.gov/x"),
                result("blog", "https://blog.example.com/x"),
                result("sub", "https://data.govinfo.gov/y"),
            )
            code, stdout, _ = self.run_search(
                workspace, "--query", "thing", "--domain-allow", "govinfo.gov", execute=True
            )
        self.assertEqual(0, code)
        hosts = sorted(c["search"]["host"] for c in json.loads(stdout)["candidates"])
        self.assertEqual(["data.govinfo.gov", "govinfo.gov"], hosts)

    def test_domain_blocklist_drops_matching_hosts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("gov", "https://www.govinfo.gov/x"),
                result("blog", "https://blog.example.com/x"),
            )
            code, stdout, _ = self.run_search(
                workspace, "--query", "thing", "--domain-block", "example.com", execute=True
            )
        self.assertEqual(0, code)
        hosts = [c["search"]["host"] for c in json.loads(stdout)["candidates"]]
        self.assertEqual(["govinfo.gov"], hosts)

    def test_duplicate_urls_are_collapsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("first", "https://example.org/a"),
                result("dup", "https://example.org/a"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(0, code)
        self.assertEqual(1, json.loads(stdout)["count"])

    def test_results_without_http_url_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                {"title": "no url"},
                {"title": "ftp", "url": "ftp://example.org/x"},
                result("ok", "https://example.org/ok"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(0, code)
        candidates = json.loads(stdout)["candidates"]
        self.assertEqual(1, len(candidates))
        self.assertEqual("https://example.org/ok", candidates[0]["url"])

    def test_rerunning_same_query_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(Path(tmpdir), result("A", "https://example.org/a"))
            _, out1, _ = self.run_search(workspace, "--query", "thing", execute=True)
            _, out2, _ = self.run_search(workspace, "--query", "thing", execute=True)
            stored = self.store_records(workspace)
        self.assertEqual(1, json.loads(out1)["written"])
        self.assertEqual(1, json.loads(out2)["count"])
        self.assertEqual(0, json.loads(out2)["written"])
        self.assertEqual(1, len(stored))

    def test_no_provider_configured_refuses_on_execute(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), None)
            code, stdout, stderr = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("SEARCH_PROVIDER_DISABLED", envelope["error_code"])
        self.assertFalse(envelope["details"]["network_io_executed"])

    def test_provider_none_refuses_on_execute(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), ["provider: none"])
            code, _, stderr = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("SEARCH_PROVIDER_DISABLED", json.loads(stderr)["error_code"])

    def test_unknown_provider_is_config_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), ["provider: bing"])
            code, _, stderr = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("CONFIG_INVALID", json.loads(stderr)["error_code"])

    def test_disabled_discovery_refuses_before_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir), result("A", "https://example.org/a"), enabled=False
            )
            code, _, stderr = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])

    def test_missing_fixture_file_is_provider_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: fixture", "fixture_path: sources/discovery/fixtures/absent.jsonl"],
            )
            code, _, stderr = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("SEARCH_PROVIDER_FAILED", json.loads(stderr)["error_code"])

    def test_malformed_fixture_is_response_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: fixture", "fixture_path: sources/discovery/fixtures/results.jsonl"],
            )
            (workspace / "sources" / "discovery" / "fixtures" / "results.jsonl").write_text(
                "{not valid json\n", encoding="utf-8"
            )
            code, _, stderr = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_RESPONSE_INVALID", json.loads(stderr)["error_code"])

    def test_fixture_path_traversal_is_config_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: fixture", "fixture_path: ../../etc/passwd"],
            )
            code, _, stderr = self.run_search(workspace, "--query", "thing", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("CONFIG_INVALID", json.loads(stderr)["error_code"])

    def test_command_provider_returns_candidates(self):
        program = (
            "import json,sys;"
            "print(json.dumps([{'title':'cmd hit','url':'https://example.org/cmd','snippet':sys.argv[-1]}]))"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: command", f'command: [{sys.executable!r}, "-c", {program!r}]'],
            )
            code, stdout, stderr = self.run_search(workspace, "--query", "needle", execute=True)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("command", report["search_provider"])
        self.assertEqual(1, report["count"])
        self.assertFalse(report["network_io_executed"])
        self.assertEqual("example.org", report["candidates"][0]["search"]["host"])

    def test_command_provider_nonzero_exit_is_provider_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: command", f'command: [{sys.executable!r}, "-c", "import sys; sys.exit(3)"]'],
            )
            code, _, stderr = self.run_search(workspace, "--query", "x", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("SEARCH_PROVIDER_FAILED", json.loads(stderr)["error_code"])

    def test_command_provider_missing_binary_is_provider_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: command", 'command: ["this-binary-does-not-exist-12345"]'],
            )
            code, _, stderr = self.run_search(workspace, "--query", "x", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("SEARCH_PROVIDER_FAILED", json.loads(stderr)["error_code"])

    def test_command_provider_bad_json_is_response_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: command", f'command: [{sys.executable!r}, "-c", "print(\'not json\')"]'],
            )
            code, _, stderr = self.run_search(workspace, "--query", "x", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_RESPONSE_INVALID", json.loads(stderr)["error_code"])

    def test_http_provider_returns_candidates_and_records_network(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: http", "endpoint: https://search.example.internal/api", "query_param: q"],
            )
            seen = {}

            def transport(url, timeout, headers):
                seen["url"] = url
                seen["headers"] = headers
                return http_body(result("HTTP hit", "https://example.org/http"))

            self.install_http(transport)
            code, stdout, stderr = self.run_search(workspace, "--query", "needle", execute=True)
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("http", report["search_provider"])
        self.assertTrue(report["network_io_executed"])
        self.assertTrue(report["candidates"][0]["network_io_executed"])
        self.assertIn("q=needle", seen["url"])
        self.assertIn("search.example.internal", seen["url"])

    def test_http_provider_requires_explicit_endpoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), ["provider: http", "query_param: q"])
            self.install_http(lambda *a: http_body())
            code, _, stderr = self.run_search(workspace, "--query", "x", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("CONFIG_INVALID", json.loads(stderr)["error_code"])

    def test_http_provider_network_error_is_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: http", "endpoint: https://search.example.internal/api"],
            )

            def transport(_url, _timeout, _headers):
                raise URLError("connection refused")

            self.install_http(transport)
            code, _, stderr = self.run_search(workspace, "--query", "x", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_NETWORK_ERROR", json.loads(stderr)["error_code"])

    def test_http_provider_bad_json_is_response_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                ["provider: http", "endpoint: https://search.example.internal/api"],
            )
            self.install_http(lambda *a: b"<html>not json</html>")
            code, _, stderr = self.run_search(workspace, "--query", "x", execute=True)
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_RESPONSE_INVALID", json.loads(stderr)["error_code"])

    def test_fixture_and_disabled_paths_open_no_socket(self):
        def forbid_socket(*args, **kwargs):  # pragma: no cover - only fires on a bug
            raise AssertionError("local search discovery must not open a network socket")

        original = socket.socket
        socket.socket = forbid_socket
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                ok = self.fixture_workspace(Path(tmpdir) / "ok", result("A", "https://example.org/a"))
                off = self.write_workspace(Path(tmpdir) / "off", None, enabled=False)
                no_provider = self.write_workspace(Path(tmpdir) / "np", None)
                code_ok, _, _ = self.run_search(ok, "--query", "x", execute=True)
                code_off, _, _ = self.run_search(off, "--query", "x", execute=True)
                code_np, _, _ = self.run_search(no_provider, "--query", "x", execute=True)
        finally:
            socket.socket = original
        self.assertEqual(0, code_ok)
        self.assertEqual(2, code_off)
        self.assertEqual(2, code_np)


class SearchTrustRankingTests(SearchTestBase):
    """E33-T03: search results are ranked by the trust-tier policy, not the
    provider's ordering. Official sources outrank generic SEO results; mirrors,
    scraped copies, suspicious downloads, and lower-trust duplicates of an
    official source are rejected with rationale; unknown provenance is reviewed.
    """

    def official_workspace(self, root: Path, official_domains: list[str], *results: dict) -> Path:
        block = ["provider: fixture", "fixture_path: sources/discovery/fixtures/results.jsonl"]
        if official_domains:
            block.append("official_domains:")
            block.extend(f"      - {d}" for d in official_domains)
        workspace = self.write_workspace(root, block)
        self.write_fixture(workspace, *results)
        return workspace

    def test_official_source_outranks_higher_ranked_seo_result(self):
        # The blog is returned first by the provider (rank 1) but a generic
        # secondary source; the official .gov result (rank 2) wins on tier.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Air quality trends blog post", "https://enviro-explainers.example/trends",
                       snippet="background on air quality"),
                result("Clean Air Act Section 60 text", "https://www.govinfo.gov/clean-air-act-60",
                       snippet="official text of section 60"),
            )
            code, stdout, stderr = self.run_search(workspace, "--query", "clean air act section 60", execute=True)
        self.assertEqual(0, code, stderr)
        candidates = json.loads(stdout)["candidates"]
        self.assertEqual("https://www.govinfo.gov/clean-air-act-60", candidates[0]["url"])
        self.assertEqual("official_primary", candidates[0]["trust_tier"])
        self.assertIs(True, candidates[0]["official_source"])
        self.assertEqual(2, candidates[0]["search"]["provider_rank"], "provider rank is not the authority")
        self.assertEqual("https://enviro-explainers.example/trends", candidates[1]["url"])
        self.assertEqual("secondary_unknown", candidates[1]["trust_tier"])
        self.assertEqual(1, candidates[1]["search"]["provider_rank"])

    def test_official_candidate_recommended_for_fetch_when_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Clean Air Act Section 60 text", "https://www.govinfo.gov/clean-air-act-60",
                       snippet="official text of section 60"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "clean air act section 60", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("fetch", candidate["recommended_action"])
        self.assertEqual([], candidate["reasoning"]["risk_flags"])

    def test_unknown_officialness_requires_review_never_fetch(self):
        # A generic blog has unverified provenance: official_source null, review.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Random blog summary", "https://someone.example/post", snippet="a post"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "topic", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("secondary_unknown", candidate["trust_tier"])
        self.assertIsNone(candidate["official_source"])
        self.assertEqual("review", candidate["recommended_action"])
        self.assertNotEqual("fetch", candidate["recommended_action"])
        self.assertIn("unknown_officialness", candidate["reasoning"]["risk_flags"])

    def test_suspicious_executable_download_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Installer", "https://example.org/tools/installer.exe", snippet="download"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "tools", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("unsafe_or_unusable", candidate["trust_tier"])
        self.assertEqual("reject", candidate["recommended_action"])
        self.assertIn("suspicious_download", candidate["reasoning"]["risk_flags"])

    def test_suspicious_archive_download_is_rejected_for_web_intent(self):
        # A .zip is suspicious for a generic web result, but legitimate for a
        # dataset intent (covered by the next test).
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Bundle", "https://example.org/data/bundle.zip", snippet="download"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "bundle", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("unsafe_or_unusable", candidate["trust_tier"])
        self.assertIn("suspicious_download", candidate["reasoning"]["risk_flags"])

    def test_archive_is_not_suspicious_for_dataset_intent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Survey dataset", "https://data.example/survey.zip", snippet="data distribution"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "survey", "--intent", "dataset", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertNotIn("suspicious_download", candidate["reasoning"]["risk_flags"])
        self.assertNotEqual("unsafe_or_unusable", candidate["trust_tier"])

    def test_mirror_duplicate_of_official_is_rejected(self):
        # A non-official mirror of the same content as an official source in the
        # same run is rejected (recorded with rationale, not silently dropped).
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Clean Air Act Section 60 text mirror", "https://mirror.example/cache/clean-air",
                       snippet="cached copy of the text"),
                result("Clean Air Act Section 60 text", "https://www.govinfo.gov/clean-air-act-60",
                       snippet="official text of section 60"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "clean air act section 60", execute=True)
        self.assertEqual(0, code)
        candidates = json.loads(stdout)["candidates"]
        by_url = {c["url"]: c for c in candidates}
        mirror = by_url["https://mirror.example/cache/clean-air"]
        self.assertEqual("reject", mirror["recommended_action"])
        self.assertIn("possible_mirror", mirror["reasoning"]["risk_flags"])
        self.assertIn("duplicate_of_official", mirror["reasoning"]["risk_flags"])
        official = by_url["https://www.govinfo.gov/clean-air-act-60"]
        self.assertEqual("official_primary", official["trust_tier"])
        self.assertEqual("fetch", official["recommended_action"])

    def test_terms_prohibited_mirror_is_unsafe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Unauthorized full-text mirror", "https://paper-mirror.example/full-text",
                       snippet="pirated copy"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "full text", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("unsafe_or_unusable", candidate["trust_tier"])
        self.assertEqual("reject", candidate["recommended_action"])
        self.assertIn("terms_prohibited", candidate["reasoning"]["risk_flags"])

    def test_official_domains_config_promotes_to_official_primary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.official_workspace(
                Path(tmpdir),
                ["acme-standards.org"],
                result("Acme specification 123", "https://acme-standards.org/spec/123", snippet="the standard"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "acme specification 123", execute=True)
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual(["acme-standards.org"], report["official_domains"])
        candidate = report["candidates"][0]
        self.assertEqual("official_primary", candidate["trust_tier"])
        self.assertIs(True, candidate["official_source"])

    def test_domain_allowlist_treated_as_official_for_legal_query(self):
        # With prefer_official (legal intent via --jurisdiction), an allowlisted
        # official domain is recognized as official_primary.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Emissions reporting statute", "https://regulator.example/statute",
                       snippet="official statute text"),
            )
            code, stdout, _ = self.run_search(
                workspace, "--query", "emissions reporting", "--jurisdiction", "us-federal",
                "--domain-allow", "regulator.example", execute=True,
            )
        self.assertEqual(0, code)
        # Legal intent emits 7 planned queries; the same URL dedups to one candidate.
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("official_primary", candidate["trust_tier"])
        self.assertIs(True, candidate["official_source"])

    def test_result_official_hint_is_respected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                {"title": "Some page", "url": "https://example.org/some", "official": True, "snippet": "page"},
            )
            code, stdout, _ = self.run_search(workspace, "--query", "some page", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("official_primary", candidate["trust_tier"])
        self.assertIs(True, candidate["official_source"])

    def test_stale_published_date_raises_risk_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("Old note", "https://example.org/note", snippet="a note", published="2010-05-01"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "note", execute=True)
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertIn("stale_source", candidate["reasoning"]["risk_flags"])
        self.assertEqual("review", candidate["recommended_action"])

    def test_exact_phrase_match_recorded_and_boosts_relevance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.fixture_workspace(
                Path(tmpdir),
                result("The quick brown fox jumps", "https://example.org/exact", snippet="over the lazy dog"),
                result("Some unrelated other content", "https://example.org/other", snippet="tokens only"),
            )
            code, stdout, _ = self.run_search(workspace, "--query", "quick brown fox", execute=True)
        self.assertEqual(0, code)
        by_url = {c["url"]: c for c in json.loads(stdout)["candidates"]}
        exact = by_url["https://example.org/exact"]
        other = by_url["https://example.org/other"]
        self.assertTrue(exact["search"]["exact_phrase"])
        self.assertFalse(other["search"]["exact_phrase"])
        self.assertGreater(exact["relevance_score"], other["relevance_score"])


if __name__ == "__main__":
    unittest.main()
