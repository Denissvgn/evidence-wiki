"""Focused tests for request-backed arXiv/OpenAlex discovery."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

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


DISCOVER = load_script_module("academic_discovery_under_test", "discover_sources.py")
SOURCE_REQUESTS = load_script_module("academic_source_requests_under_test", "source_requests.py")
REQUEST_ID = "req-paper-1234567890"
QUERY = "solid state electrolyte conductivity"
DOI = "10.5555/solid-electrolyte"

ARXIV_PAYLOAD = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>https://arxiv.org/abs/2601.12345v2</id>
    <published>2026-01-10T00:00:00Z</published>
    <updated>2026-01-12T00:00:00Z</updated>
    <title>Solid Electrolyte Conductivity Survey</title>
    <summary>Compares solid electrolyte families.</summary>
    <author><name>Ada Example</name></author>
    <arxiv:doi>{DOI}</arxiv:doi>
    <link rel="alternate" href="https://arxiv.org/abs/2601.12345v2" />
    <link title="pdf" href="https://arxiv.org/pdf/2601.12345v2" />
  </entry>
</feed>
""".encode()


def openalex_payload() -> bytes:
    work = {
        "id": "https://openalex.org/W12345",
        "doi": f"https://doi.org/{DOI}",
        "display_name": "Solid Electrolyte Conductivity Survey",
        "publication_year": 2026,
        "type": "article",
        "cited_by_count": 12,
        "authorships": [{"author": {"display_name": "Ada Example"}}],
        "open_access": {"is_oa": True, "oa_status": "green"},
        "best_oa_location": {
            "landing_page_url": "https://arxiv.org/abs/2601.12345v2",
            "pdf_url": "https://arxiv.org/pdf/2601.12345v2",
            "license": "cc-by-4.0",
        },
    }
    return json.dumps({"meta": {"count": 1}, "results": [work]}).encode()


class AcademicDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.pop("OPENALEX_API_KEY", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved_key is None:
            os.environ.pop("OPENALEX_API_KEY", None)
        else:
            os.environ["OPENALEX_API_KEY"] = self._saved_key
        DISCOVER.ARXIV_TRANSPORT = None
        DISCOVER.OPENALEX_TRANSPORT = None
        DISCOVER.ARXIV_LAST_REQUEST_AT = None
        DISCOVER.OPENALEX_LAST_REQUEST_AT = None

    def workspace(self, root: Path, *, providers: list[str], custom_store: bool = True) -> Path:
        target = root / "ws"
        (target / "sources").mkdir(parents=True)
        config = {
            "project": {"name": "academic-discovery-test"},
            "sources": {"source_requests_path": "sources/source-requests.jsonl"},
            "integrations": {
                "discovery": {
                    "enabled": True,
                    "providers": providers,
                    "candidate_store_path": (
                        "sources/custom/paper-candidates.jsonl"
                        if custom_store
                        else "sources/discovery/candidates.jsonl"
                    ),
                },
                "acquisition": {
                    "enabled": True,
                    "providers": ["arxiv", "openalex"],
                },
            },
        }
        (target / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        request = {
            "schema_version": "1.0",
            "request_id": REQUEST_ID,
            "kind": "paper",
            "query_or_identifier": QUERY,
            "rationale": "Answer the linked research question.",
            "priority": "high",
            "question_slugs": ["electrolyte-conductivity"],
            "status": "open",
            "created_at": "2026-07-20T00:00:00Z",
            "updated_at": "2026-07-20T00:00:00Z",
            "source_id": None,
        }
        (target / "sources" / "source-requests.jsonl").write_text(json.dumps(request) + "\n", encoding="utf-8")
        return target

    def run_cli(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "academic", *args]
        return self.run_raw_cli(argv)

    def run_raw_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def select_candidate(self, workspace: Path, candidate_id: str) -> dict:
        code, stdout, stderr = self.run_raw_cli(
            [
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "candidates",
                "select",
                "--candidate-id",
                candidate_id,
                "--request-id",
                REQUEST_ID,
                "--reason",
                "Selected for acquisition-route verification.",
            ]
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def plan_fetch(self, workspace: Path) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = SOURCE_REQUESTS.main(
                [
                    "--project-root",
                    str(workspace),
                    "plan-fetch",
                    "--request-id",
                    REQUEST_ID,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, int(code or 0), stderr.getvalue())
        return json.loads(stdout.getvalue())

    def install_transports(self, calls: list[tuple[str, str]]) -> None:
        DISCOVER.ARXIV_CLOCK = lambda: 0.0
        DISCOVER.OPENALEX_CLOCK = lambda: 0.0
        DISCOVER.ARXIV_SLEEP = lambda _seconds: None
        DISCOVER.OPENALEX_SLEEP = lambda _seconds: None

        def arxiv(url, _timeout, _headers):
            calls.append(("arxiv", url))
            return ARXIV_PAYLOAD

        def openalex(url, _timeout, _headers):
            calls.append(("openalex", url))
            return openalex_payload()

        DISCOVER.ARXIV_TRANSPORT = arxiv
        DISCOVER.OPENALEX_TRANSPORT = openalex

    def test_two_providers_dedupe_and_honor_configured_store(self):
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["arxiv", "openalex"])
            self.install_transports(calls)
            code, stdout, stderr = self.run_cli(
                workspace,
                "--request-id",
                REQUEST_ID,
                "--provider",
                "arxiv",
                "--provider",
                "openalex",
                "--max-results",
                "15",
            )
            report = json.loads(stdout)
            store = workspace / "sources" / "custom" / "paper-candidates.jsonl"
            records = [json.loads(line) for line in store.read_text().splitlines() if line.strip()]
            self.select_candidate(workspace, report["candidates"][0]["candidate_id"])
            fetch_plan = self.plan_fetch(workspace)

        self.assertEqual(0, code, stderr)
        self.assertEqual("source_request", report["query_source"])
        self.assertEqual(1, report["count"])
        self.assertEqual(1, report["written"])
        self.assertEqual(["arxiv", "openalex"], report["candidates"][0]["discovery_providers"])
        self.assertEqual("2601.12345v2", report["candidates"][0]["paper"]["provider_ids"]["arxiv"])
        self.assertEqual("W12345", report["candidates"][0]["paper"]["provider_ids"]["openalex"])
        self.assertEqual(DOI, report["candidates"][0]["paper"]["provider_ids"]["doi"])
        self.assertEqual("sources/custom/paper-candidates.jsonl", report["candidates_path"])
        self.assertEqual(1, len(records))
        self.assertEqual({"arxiv", "openalex"}, {provider for provider, _url in calls})
        self.assertTrue(all(QUERY.replace(" ", "+") in url or "solid+state" in url for _, url in calls))
        self.assertEqual(1, fetch_plan["selected_candidate_count"])
        self.assertEqual("arxiv", fetch_plan["candidate_routes"][0]["provider"])
        self.assertEqual("download-source", fetch_plan["candidate_routes"][0]["route"])
        self.assertFalse(any("below the required" in warning for warning in fetch_plan["warnings"]))

    def test_rerun_is_idempotent_and_query_override_is_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["arxiv"], custom_store=False)
            self.install_transports([])
            args = (
                "--request-id",
                REQUEST_ID,
                "--provider",
                "arxiv",
                "--query",
                "sulfide solid electrolyte",
            )
            first_code, first_stdout, first_stderr = self.run_cli(workspace, *args)
            DISCOVER.ARXIV_LAST_REQUEST_AT = None
            second_code, second_stdout, second_stderr = self.run_cli(workspace, *args)

        self.assertEqual(0, first_code, first_stderr)
        self.assertEqual(0, second_code, second_stderr)
        self.assertEqual("argument", json.loads(first_stdout)["query_source"])
        self.assertEqual(1, json.loads(first_stdout)["written"])
        self.assertEqual(0, json.loads(second_stdout)["written"])

    def test_disabled_provider_refuses_before_transport(self):
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["openalex"])
            self.install_transports(calls)
            code, stdout, stderr = self.run_cli(
                workspace,
                "--request-id",
                REQUEST_ID,
                "--provider",
                "arxiv",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("DISCOVERY_PROVIDER_DISABLED", json.loads(stderr)["error_code"])
        self.assertEqual([], calls)

    def test_unknown_request_refuses_before_transport(self):
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["arxiv"])
            self.install_transports(calls)
            code, _, stderr = self.run_cli(
                workspace,
                "--request-id",
                "req-missing",
                "--provider",
                "arxiv",
            )

        self.assertEqual(2, code)
        self.assertEqual("REQUEST_UNKNOWN", json.loads(stderr)["error_code"])
        self.assertEqual([], calls)

    def test_non_open_request_refuses_before_transport(self):
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["arxiv"])
            request_path = workspace / "sources" / "source-requests.jsonl"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["status"] = "fulfilled"
            request["source_id"] = "paper:already-present"
            request_path.write_text(json.dumps(request) + "\n", encoding="utf-8")
            self.install_transports(calls)
            code, _, stderr = self.run_cli(
                workspace,
                "--request-id",
                REQUEST_ID,
                "--provider",
                "arxiv",
            )

        self.assertEqual(2, code)
        error = json.loads(stderr)
        self.assertEqual("REQUEST_NOT_OPEN", error["error_code"])
        self.assertEqual("fulfilled", error["details"]["request_status"])
        self.assertEqual([], calls)

    def test_duplicate_provider_refuses_before_transport(self):
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["arxiv"])
            self.install_transports(calls)
            code, _, stderr = self.run_cli(
                workspace,
                "--request-id",
                REQUEST_ID,
                "--provider",
                "arxiv",
                "--provider",
                "arxiv",
            )

        self.assertEqual(2, code)
        self.assertEqual("VALUE_INVALID", json.loads(stderr)["error_code"])
        self.assertEqual([], calls)

    def test_trailing_format_and_provider_budget_cap(self):
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["arxiv"])
            self.install_transports(calls)
            code, stdout, stderr = self.run_raw_cli(
                [
                    "--project-root",
                    str(workspace),
                    "academic",
                    "--request-id",
                    REQUEST_ID,
                    "--provider",
                    "arxiv",
                    "--max-results",
                    "999",
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual(50, report["max_results"])
        self.assertEqual(50, report["candidates"][0]["provider_budget"]["max_results"])
        self.assertEqual(50, report["candidates"][0]["provider_budget"]["max_results_cap"])
        self.assertTrue(any("max_results=50" in url for _, url in calls))

    def test_openalex_auth_and_rate_errors_are_academic_envelopes(self):
        secret = "openalex-secret-auth-rate"
        os.environ["OPENALEX_API_KEY"] = secret
        cases = (
            (401, "OPENALEX_AUTH_REQUIRED"),
            (403, "OPENALEX_AUTH_REQUIRED"),
            (429, "OPENALEX_RATE_LIMITED"),
            (500, "DISCOVERY_NETWORK_ERROR"),
        )
        for status, expected in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.workspace(Path(tmpdir), providers=["openalex"])
                requested_urls: list[str] = []

                def transport(url, _timeout, _headers, *, status=status, requested_urls=requested_urls):
                    requested_urls.append(url)
                    raise HTTPError(url, status, "provider error", {}, io.BytesIO(b""))

                DISCOVER.OPENALEX_TRANSPORT = transport
                DISCOVER.OPENALEX_CLOCK = lambda: 0.0
                DISCOVER.OPENALEX_SLEEP = lambda _seconds: None
                code, stdout, stderr = self.run_cli(
                    workspace,
                    "--request-id",
                    REQUEST_ID,
                    "--provider",
                    "openalex",
                )
                error = json.loads(stderr)
                artifact_text = "\n".join(
                    path.read_text(encoding="utf-8", errors="replace")
                    for path in workspace.rglob("*")
                    if path.is_file()
                )
                self.assertEqual(2, code)
                self.assertEqual(expected, error["error_code"])
                self.assertEqual("academic", error["details"]["command"])
                self.assertTrue(error["details"]["network_io_executed"])
                self.assertTrue(any("api_key=" in url for url in requested_urls))
                self.assertNotIn(secret, stdout)
                self.assertNotIn(secret, stderr)
                self.assertNotIn(secret, artifact_text)
                self.assertIn("api_key=[REDACTED]", error["message"])
                DISCOVER.OPENALEX_LAST_REQUEST_AT = None

    def test_openalex_network_error_redacts_key_from_exception_and_artifacts(self):
        secret = "openalex secret+/network"
        encoded_secret = DISCOVER.urlencode({"api_key": secret}).partition("=")[2]
        os.environ["OPENALEX_API_KEY"] = secret
        calls: list[str] = []

        def transport(url, _timeout, _headers):
            calls.append(url)
            raise URLError(f"connection failed for {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["openalex"])
            DISCOVER.OPENALEX_TRANSPORT = transport
            DISCOVER.OPENALEX_CLOCK = lambda: 0.0
            DISCOVER.OPENALEX_SLEEP = lambda _seconds: None
            code, stdout, stderr = self.run_cli(
                workspace,
                "--request-id",
                REQUEST_ID,
                "--provider",
                "openalex",
            )
            artifact_text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in workspace.rglob("*")
                if path.is_file()
            )

        error = json.loads(stderr)
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_NETWORK_ERROR", error["error_code"])
        self.assertEqual(DISCOVER.OPENALEX_MAX_ATTEMPTS, len(calls))
        self.assertNotIn(secret, stdout)
        self.assertNotIn(secret, stderr)
        self.assertNotIn(secret, artifact_text)
        self.assertNotIn(encoded_secret, stdout)
        self.assertNotIn(encoded_secret, stderr)
        self.assertNotIn(encoded_secret, artifact_text)
        self.assertIn("api_key=[REDACTED]", error["message"])

    def test_academic_providers_reject_oversized_responses_before_parsing(self):
        providers = (
            ("openalex", "OPENALEX_TRANSPORT", DISCOVER.OPENALEX_MAX_RESPONSE_BYTES),
            ("arxiv", "ARXIV_TRANSPORT", DISCOVER.ARXIV_MAX_RESPONSE_BYTES),
        )
        for provider, transport_name, limit in providers:
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.workspace(Path(tmpdir), providers=[provider])
                oversized = b"not-valid-provider-data" + (b"x" * limit)
                setattr(
                    DISCOVER,
                    transport_name,
                    lambda _url, _timeout, _headers, oversized=oversized: oversized,
                )
                DISCOVER.ARXIV_CLOCK = lambda: 0.0
                DISCOVER.OPENALEX_CLOCK = lambda: 0.0
                DISCOVER.ARXIV_SLEEP = lambda _seconds: None
                DISCOVER.OPENALEX_SLEEP = lambda _seconds: None
                code, stdout, stderr = self.run_cli(
                    workspace,
                    "--request-id",
                    REQUEST_ID,
                    "--provider",
                    provider,
                )

                error = json.loads(stderr)
                store = workspace / "sources" / "custom" / "paper-candidates.jsonl"
                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertEqual("DISCOVERY_RESPONSE_INVALID", error["error_code"])
                self.assertIn("exceeded the fixed", error["message"])
                self.assertEqual(limit, error["details"]["response_limit_bytes"])
                self.assertFalse(store.exists())
                DISCOVER.ARXIV_LAST_REQUEST_AT = None
                DISCOVER.OPENALEX_LAST_REQUEST_AT = None

    def test_default_academic_transports_bound_reads_to_limit_plus_one(self):
        read_sizes: list[int] = []

        class Response:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def read(self, size: int) -> bytes:
                read_sizes.append(size)
                return self.payload

        responses = [Response(b"{}"), Response(b"<feed />")]
        with mock.patch.object(DISCOVER, "urlopen", side_effect=responses):
            openalex_payload_bytes = DISCOVER.urllib_openalex_transport(
                "https://api.openalex.org/works",
                1.0,
                {},
            )
            arxiv_payload_bytes = DISCOVER._urllib_arxiv_transport(
                "https://export.arxiv.org/api/query",
                1.0,
                {},
            )

        self.assertEqual(b"{}", openalex_payload_bytes)
        self.assertEqual(b"<feed />", arxiv_payload_bytes)
        self.assertEqual(
            [
                DISCOVER.OPENALEX_MAX_RESPONSE_BYTES + 1,
                DISCOVER.ARXIV_MAX_RESPONSE_BYTES + 1,
            ],
            read_sizes,
        )

    def test_arxiv_rate_error_is_academic_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=["arxiv"])

            def transport(url, _timeout, _headers):
                raise HTTPError(url, 429, "provider error", {}, io.BytesIO(b""))

            DISCOVER.ARXIV_TRANSPORT = transport
            DISCOVER.ARXIV_CLOCK = lambda: 0.0
            DISCOVER.ARXIV_SLEEP = lambda _seconds: None
            code, _, stderr = self.run_cli(
                workspace,
                "--request-id",
                REQUEST_ID,
                "--provider",
                "arxiv",
            )

        error = json.loads(stderr)
        self.assertEqual(2, code)
        self.assertEqual("ARXIV_RATE_LIMITED", error["error_code"])
        self.assertEqual("academic", error["details"]["command"])
        self.assertTrue(error["details"]["network_io_executed"])


if __name__ == "__main__":
    unittest.main()
