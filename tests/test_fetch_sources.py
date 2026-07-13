import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FETCH_PATH = SCRIPTS / "fetch_sources.py"
INVENTORY_PATH = SCRIPTS / "source_inventory.py"
INIT_PATH = SCRIPTS / "init_research_workspace.py"
SMOKE_PATH = SCRIPTS / "smoke_validate_workspace.py"
ACADEMIC_REPLAY_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "academic-replay"
OPENALEX_PROVIDER_CONTRACTS = json.loads(
    (ACADEMIC_REPLAY_FIXTURES / "openalex-provider-contracts.json").read_text(encoding="utf-8")
)


ATOM_SEARCH_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>ArXiv Query: search_query=language models</title>
  <entry>
    <id>http://arxiv.org/abs/2601.00001v1</id>
    <updated>2026-01-02T03:04:05Z</updated>
    <published>2026-01-01T01:02:03Z</published>
    <title>  Synthetic Retrieval Paper  </title>
    <summary>
      A compact abstract with
      extra whitespace.
    </summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Grace Hopper</name></author>
    <category term="cs.CL" />
    <category term="cs.IR" />
    <arxiv:doi>10.5555/example</arxiv:doi>
    <arxiv:comment>12 pages</arxiv:comment>
    <arxiv:journal_ref>Journal of Synthetic Fixtures 1(1)</arxiv:journal_ref>
    <link rel="alternate" href="https://arxiv.org/abs/2601.00001v1" />
    <link title="pdf" href="https://arxiv.org/pdf/2601.00001v1" />
  </entry>
</feed>
"""


OPENALEX_WORK = {
    "id": "https://openalex.org/W260100001",
    "doi": "https://doi.org/10.5555/openalex",
    "display_name": "Synthetic Retrieval Paper",
    "publication_year": 2026,
    "type": "article",
    "open_access": {"is_oa": True, "oa_status": "gold", "oa_url": "https://example.org/synthetic.pdf"},
    "primary_location": {
        "is_oa": False,
        "landing_page_url": "https://publisher.example/synthetic",
        "pdf_url": None,
        "license": None,
        "license_id": None,
        "source": {"display_name": "Journal of Synthetic Retrieval"},
    },
    "best_oa_location": {
        "is_oa": True,
        "landing_page_url": "https://example.org/synthetic",
        "pdf_url": "https://example.org/synthetic.pdf",
        "license": "cc-by-4.0",
        "license_id": "cc-by-4.0",
        "source": {"display_name": "Journal of Synthetic Retrieval"},
    },
    "locations": [
        {
            "is_oa": True,
            "landing_page_url": "https://example.org/synthetic",
            "pdf_url": "https://example.org/synthetic.pdf",
            "license": "cc-by-4.0",
            "license_id": "cc-by-4.0",
            "source": {"display_name": "Journal of Synthetic Retrieval"},
        }
    ],
}

OPENALEX_SEARCH_RESPONSE = json.dumps(
    {
        "meta": {"count": 1, "page": 1, "per_page": 1},
        "results": [OPENALEX_WORK],
    }
).encode("utf-8")

OPENALEX_NON_EXACT_RESPONSE = json.dumps(
    {
        "meta": {"count": 1, "page": 1, "per_page": 1},
        "results": [
            {
                **OPENALEX_WORK,
                "id": "https://openalex.org/W260100002",
                "display_name": "Related Synthetic Retrieval Study",
            }
        ],
    }
).encode("utf-8")

OPENALEX_CLOSED_WORK = {
    **OPENALEX_WORK,
    "id": "https://openalex.org/W260100003",
    "open_access": {"is_oa": False, "oa_status": "closed", "oa_url": None},
    "primary_location": {"is_oa": False, "pdf_url": None, "license": None, "license_id": None},
    "best_oa_location": None,
    "locations": [],
}


def load_script_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"Missing script: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def patched_argv(*args: str):
    old = sys.argv
    sys.argv = ["script", *args]
    try:
        yield
    finally:
        sys.argv = old


def tar_gz_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class FetchSourcesTests(unittest.TestCase):
    def setUp(self):
        self.fetch = load_script_module("research_fetch_sources", FETCH_PATH)

    def build_workspace(self, root: Path, acquisition: dict | None = None) -> Path:
        workspace = root / "workspace"
        for relative in ("raw/papers", "sources", "wiki"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        config = {
            "project": {"name": "fetch-sources-test"},
            "raw": {"source_roots": ["raw/papers"]},
            "sources": {"manifest_path": "sources/manifest.jsonl"},
            "wiki": {"root": "wiki", "required_dirs": []},
            "integrations": {
                "acquisition": acquisition
                if acquisition is not None
                else {
                    "enabled": False,
                    "providers": [],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                }
            },
        }
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return workspace

    def build_enabled_workspace(self, root: Path, limit: int = 10) -> Path:
        return self.build_workspace(
            root,
            {
                "enabled": True,
                "providers": ["arxiv"],
                "target_root": "raw/papers",
                "max_downloads_per_run": limit,
                "require_license_check": True,
            },
        )

    def build_openalex_workspace(self, root: Path, limit: int = 10) -> Path:
        return self.build_workspace(
            root,
            {
                "enabled": True,
                "providers": ["openalex"],
                "target_root": "raw/papers",
                "max_downloads_per_run": limit,
                "require_license_check": True,
            },
        )

    def build_web_workspace(
        self,
        root: Path,
        *,
        allowed_domains: list[str] | None = None,
        limit: int = 10,
    ) -> Path:
        return self.build_workspace(
            root,
            {
                "enabled": True,
                "providers": ["web"],
                "target_root": "raw/papers",
                "max_downloads_per_run": limit,
                "require_license_check": True,
                "web": {
                    "allowed_domains": allowed_domains or ["official.example"],
                    "target_root": "raw/web",
                    "max_download_bytes": 128,
                },
            },
        )

    def write_active_run(self, workspace: Path, run_id: str = "run-acquisition-budget") -> str:
        run_dir = workspace / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "started_at": "2000-01-01T00:00:00Z",
                    "state": {"current": "fetching"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return run_id

    def install_transport(self, transport, *, clock=None, sleep=None) -> None:
        self.fetch.ARXIV_TRANSPORT = transport
        self.fetch.ARXIV_CLOCK = clock or (lambda: 0.0)
        self.fetch.ARXIV_SLEEP = sleep or (lambda _seconds: None)
        self.fetch.ARXIV_LAST_REQUEST_AT = None

    def install_openalex_transport(self, transport, *, clock=None, sleep=None) -> None:
        self.fetch.OPENALEX_TRANSPORT = transport
        self.fetch.OPENALEX_CLOCK = clock or (lambda: 0.0)
        self.fetch.OPENALEX_SLEEP = sleep or (lambda _seconds: None)
        self.fetch.OPENALEX_LAST_REQUEST_AT = None

    def install_doi_transport(self, transport) -> None:
        self.fetch.DOI_TRANSPORT = transport

    def test_urllib_arxiv_transport_routes_expected_media_types(self):
        calls = []

        def spy_bounded_fetch_bytes(url, **kwargs):
            calls.append((url, kwargs))
            return b"payload"

        original = self.fetch.bounded_fetch_bytes
        self.fetch.bounded_fetch_bytes = spy_bounded_fetch_bytes
        try:
            for url in (
                "https://export.arxiv.org/api/query?id_list=2601.00001v1",
                "https://arxiv.org/pdf/2601.00001v1",
                "https://arxiv.org/e-print/2601.00001v1",
            ):
                self.fetch.urllib_transport(url, 5.0)
        finally:
            self.fetch.bounded_fetch_bytes = original

        self.assertEqual(
            [
                self.fetch.ARXIV_METADATA_CONTENT_TYPES,
                self.fetch.ARXIV_PDF_CONTENT_TYPES,
                self.fetch.ARXIV_SOURCE_CONTENT_TYPES,
            ],
            [kwargs["expected_content_types"] for _url, kwargs in calls],
        )
        self.assertTrue(all(kwargs["resolve_hostnames"] for _url, kwargs in calls))

    def test_urllib_doi_transport_uses_head_and_does_not_read_body(self):
        calls: dict[str, object] = {}

        class FakeResponse:
            headers = {"Content-Type": "text/html"}

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

            def geturl(self) -> str:
                return "https://arxiv.org/abs/2504.19874"

            def getcode(self) -> int:
                return 200

            def read(self, *_args, **_kwargs):
                raise AssertionError("DOI corroboration must not read response bodies")

        def fake_build_default_opener(**kwargs):
            calls["opener_kwargs"] = kwargs

            def opener(request, timeout):
                calls["method"] = request.get_method()
                calls["timeout"] = timeout
                return FakeResponse()

            return opener

        original_build_default_opener = self.fetch.build_default_opener
        original_validate_https_url = self.fetch.validate_https_url
        self.fetch.build_default_opener = fake_build_default_opener
        self.fetch.validate_https_url = lambda url, **kwargs: calls.setdefault("validated_urls", []).append(
            (url, kwargs)
        ) or (urlparse(url).hostname or "")
        try:
            result = self.fetch.urllib_doi_transport(
                "https://doi.org/10.48550/arXiv.2504.19874",
                10.0,
                self.fetch.doi_headers(),
            )
        finally:
            self.fetch.build_default_opener = original_build_default_opener
            self.fetch.validate_https_url = original_validate_https_url

        self.assertEqual("HEAD", calls["method"])
        self.assertEqual(10.0, calls["timeout"])
        self.assertTrue(calls["opener_kwargs"]["resolve_hostnames"])
        self.assertTrue(all(kwargs["resolve_hostnames"] for _url, kwargs in calls["validated_urls"]))
        self.assertEqual("https://arxiv.org/abs/2504.19874", result.final_url)
        self.assertEqual(b"", result.content)

    def write_arxiv_source_for_enrichment(
        self,
        workspace: Path,
        *,
        source_id: str = "paper:2601.00001v1",
        arxiv_id: str = "2601.00001v1",
        title: str = "Synthetic Retrieval Paper",
        authors: list[str] | None = None,
        publication_year: int = 2026,
        license_value: str = "unresolved",
    ) -> Path:
        target = workspace / "raw" / "papers" / f"{arxiv_id}.pdf"
        target.write_bytes(b"%PDF-1.4\nSynthetic PDF bytes\n")
        sidecar = target.with_name(f"{arxiv_id}.pdf.provenance.yml")
        sidecar.write_text(
            yaml.safe_dump(
                {
                    "origin_url": f"https://arxiv.org/abs/{arxiv_id}",
                    "license": license_value,
                    "terms_url": f"https://arxiv.org/abs/{arxiv_id}",
                    "retrieved_at": "2026-07-05T12:00:00Z",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "academic_provider": "arxiv",
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "authors": authors or ["Ada Lovelace", "Grace Hopper"],
                    "publication_year": publication_year,
                    "checksum": "sha256:" + "0" * 64,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (workspace / "sources" / "manifest.jsonl").write_text(
            json.dumps(
                {
                    "id": source_id,
                    "kind": "pdf",
                    "raw_paths": [f"raw/papers/{arxiv_id}.pdf"],
                    "status": "pending",
                    "provenance": {
                        "sidecar_path": f"raw/papers/{arxiv_id}.pdf.provenance.yml",
                        "retrieved_by": "fetch_sources.py/arxiv",
                        "academic_provider": "arxiv",
                        "arxiv_id": arxiv_id,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return sidecar

    def install_web_transport(self, transport) -> None:
        self.fetch.WEB_TRANSPORT = transport

    def run_fetch(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.fetch.main(list(args))
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_default_workspace_rejects_json_provider_command_before_network(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            calls = []

            def transport(url: str, timeout: float) -> bytes:
                calls.append((url, timeout))
                raise AssertionError("disabled acquisition must not call transport")

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                "chain-of-thought",
                "--max-results",
                "1",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("1.0", envelope["schema_version"])
        self.assertEqual("ACQUISITION_DISABLED", envelope["error_code"])
        self.assertTrue(envelope["recoverable"])
        self.assertIn("integrations.acquisition.enabled: true", envelope["remediation"])
        self.assertEqual([], calls)

    def test_enabled_workspace_rejects_provider_not_in_allow_list(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                {
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                "retrieval augmented generation",
                "--max-results",
                "1",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_PROVIDER_DISABLED", envelope["error_code"])
        self.assertIn("not listed", envelope["message"])

    def test_unsafe_target_root_is_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                {
                    "enabled": True,
                    "providers": ["arxiv"],
                    "target_root": "../outside",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                "safety",
                "--max-results",
                "1",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("CONFIG_INVALID", envelope["error_code"])
        self.assertIn("target_root", envelope["message"])

    def test_max_downloads_per_run_is_enforced_for_bounded_commands(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                {
                    "enabled": True,
                    "providers": ["arxiv"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 2,
                    "require_license_check": True,
                },
            )

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                "language models",
                "--max-results",
                "3",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_LIMIT_EXCEEDED", envelope["error_code"])
        self.assertIn("max_downloads_per_run", envelope["message"])

    def test_provenance_helper_writes_sidecar_with_checksum(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            root = Path(tmpdir)
            target = root / "paper.pdf"
            target.write_bytes(b"pdf bytes")

            sidecar = self.fetch.write_provenance_sidecar(
                target,
                origin_url="https://arxiv.org/abs/2601.00001v1",
                license_value=None,
                retrieved_by="fetch_sources.py/arxiv",
                title="Synthetic Retrieval Paper",
                authors=["Ada Lovelace", "Grace Hopper"],
                request_id="req-123",
                notes="License not inferable from provider metadata.",
            )

            payload = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(target.with_name("paper.pdf.provenance.yml"), sidecar)
        self.assertEqual("https://arxiv.org/abs/2601.00001v1", payload["origin_url"])
        self.assertIsNone(payload["license"])
        self.assertEqual("fetch_sources.py/arxiv", payload["retrieved_by"])
        self.assertEqual("Synthetic Retrieval Paper", payload["title"])
        self.assertEqual(["Ada Lovelace", "Grace Hopper"], payload["authors"])
        self.assertEqual("req-123", payload["request_id"])
        self.assertEqual("License not inferable from provider metadata.", payload["notes"])
        self.assertRegex(payload["retrieved_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(payload["checksum"], r"^sha256:[0-9a-f]{64}$")

    def test_arxiv_search_parses_atom_response_to_compact_json(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            calls = []

            def transport(url: str, timeout: float) -> bytes:
                calls.append((url, timeout))
                return ATOM_SEARCH_RESPONSE

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                "language models",
                "--max-results",
                "1",
            )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertNotIn('\n  "', stdout, "search JSON should be compact for agent-safe redirection")
        payload = json.loads(stdout)
        self.assertEqual("1.0", payload["schema_version"])
        self.assertEqual("arxiv", payload["provider"])
        self.assertEqual("search", payload["command"])
        self.assertEqual("language models", payload["query"])
        self.assertEqual(1, payload["count"])
        result = payload["results"][0]
        self.assertEqual("2601.00001v1", result["id"])
        self.assertEqual("Synthetic Retrieval Paper", result["title"])
        self.assertEqual("A compact abstract with extra whitespace.", result["summary"])
        self.assertEqual(["Ada Lovelace", "Grace Hopper"], result["authors"])
        self.assertEqual(["cs.CL", "cs.IR"], result["categories"])
        self.assertEqual("2026-01-01T01:02:03Z", result["published"])
        self.assertEqual("2026-01-02T03:04:05Z", result["updated"])
        self.assertEqual("10.5555/example", result["doi"])
        self.assertEqual("12 pages", result["comment"])
        self.assertEqual("Journal of Synthetic Fixtures 1(1)", result["journal_ref"])
        self.assertEqual("https://arxiv.org/abs/2601.00001v1", result["abs_url"])
        self.assertEqual("https://arxiv.org/pdf/2601.00001v1", result["pdf_url"])
        self.assertEqual("https://arxiv.org/e-print/2601.00001v1", result["source_url"])
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(["language models"], query["search_query"])
        self.assertEqual(["1"], query["max_results"])
        self.assertEqual(30.0, calls[0][1])

    def test_arxiv_id_list_search_writes_output_file_and_small_stdout(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            calls = []

            def transport(url: str, timeout: float) -> bytes:
                calls.append(url)
                return ATOM_SEARCH_RESPONSE

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--id-list",
                "2601.00001v1, 2601.00002v2",
                "--max-results",
                "2",
                "--output",
                "sources/arxiv-search.json",
            )
            output_path = workspace / "sources" / "arxiv-search.json"
            file_payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        stdout_payload = json.loads(stdout)
        self.assertEqual("sources/arxiv-search.json", stdout_payload["output_path"])
        self.assertEqual(1, stdout_payload["count"])
        self.assertNotIn("results", stdout_payload)
        self.assertEqual("2601.00001v1", file_payload["results"][0]["id"])
        query = parse_qs(urlparse(calls[0]).query)
        self.assertEqual(["2601.00001v1,2601.00002v2"], query["id_list"])
        self.assertEqual(["2"], query["max_results"])

    def test_arxiv_rate_limiter_waits_between_requests(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            sleeps = []
            now = [100.0]

            def transport(_url: str, _timeout: float) -> bytes:
                return ATOM_SEARCH_RESPONSE

            def sleep(seconds: float) -> None:
                sleeps.append(seconds)
                now[0] += seconds

            self.install_transport(transport, clock=lambda: now[0], sleep=sleep)

            first = self.run_fetch(
                "--project-root",
                str(workspace),
                "arxiv",
                "search",
                "--query",
                "first",
                "--max-results",
                "1",
            )
            second = self.run_fetch(
                "--project-root",
                str(workspace),
                "arxiv",
                "search",
                "--query",
                "second",
                "--max-results",
                "1",
            )

        self.assertEqual(0, first[0])
        self.assertEqual(0, second[0])
        self.assertEqual([3.0], sleeps)

    def test_arxiv_retry_succeeds_after_transient_failure(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            attempts = []

            def transport(url: str, _timeout: float) -> bytes:
                attempts.append(url)
                if len(attempts) == 1:
                    raise URLError("temporary network failure")
                return ATOM_SEARCH_RESPONSE

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                "retry",
                "--max-results",
                "1",
            )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(2, len(attempts))
        self.assertEqual("2601.00001v1", json.loads(stdout)["results"][0]["id"])

    def test_openalex_429_uses_bounded_backoff_then_recovers(self):
        attempts: list[str] = []
        sleeps: list[float] = []
        original_interval = self.fetch.OPENALEX_REQUEST_INTERVAL_SECONDS
        self.fetch.OPENALEX_REQUEST_INTERVAL_SECONDS = 0.0
        self.addCleanup(setattr, self.fetch, "OPENALEX_REQUEST_INTERVAL_SECONDS", original_interval)

        def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
            attempts.append(url)
            if len(attempts) == 1:
                raise HTTPError(url=url, code=429, msg="rate limited", hdrs=None, fp=None)
            return b"{}"

        self.install_openalex_transport(transport, sleep=sleeps.append)

        payload = self.fetch.openalex_fetch_url("https://api.openalex.org/works/W1")

        self.assertEqual(b"{}", payload)
        self.assertEqual(2, len(attempts))
        self.assertEqual([1.0], sleeps)
        self.assertEqual([1.0, 2.0, 4.0, 8.0, 8.0], [self.fetch.retry_backoff_seconds(i) for i in range(1, 6)])

    def test_interrupted_marker_is_quarantined_and_retry_commits_one_evidence_pair(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = Path(tmpdir) / "raw" / "papers" / "paper.pdf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"incomplete")
            marker = self.fetch.write_acquisition_marker(target)

            self.fetch.ensure_download_target_available(target)
            with self.fetch.acquisition_artifact_transaction(target):
                self.fetch.atomic_write_bytes(target, b"complete")
                sidecar = self.fetch.write_provenance_sidecar(
                    target,
                    origin_url="https://official.example/paper.pdf",
                    license_value=None,
                    retrieved_by="fetch_sources.py/test",
                )

            quarantine = target.parent / ".acquisition-quarantine"
            self.assertEqual(b"complete", target.read_bytes())
            self.assertTrue(sidecar.is_file())
            self.assertFalse(marker.exists())
            self.assertEqual(1, len(list(quarantine.glob("paper.pdf.*.interrupted"))))
            self.assertEqual(1, len(list(target.parent.glob("paper.pdf.provenance.yml"))))

    def test_failed_artifact_transaction_never_promotes_partial_evidence(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = Path(tmpdir) / "raw" / "papers" / "paper.pdf"
            with self.assertRaises(self.fetch.FetchSourcesError) as raised:
                with self.fetch.acquisition_artifact_transaction(target):
                    self.fetch.atomic_write_bytes(target, b"partial")
                    raise OSError("simulated sidecar failure")

            self.assertEqual("ACQUISITION_WRITE_FAILED", raised.exception.error_code)
            self.assertFalse(target.exists())
            self.assertFalse(self.fetch.provenance_sidecar_path(target).exists())
            self.assertFalse(self.fetch.acquisition_marker_path(target).exists())
            quarantine = target.parent / ".acquisition-quarantine"
            self.assertEqual(1, len(list(quarantine.glob("paper.pdf.*.interrupted"))))

    def test_inventory_refuses_marker_backed_payload_until_sidecar_commit(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            target = workspace / "raw" / "papers" / "incomplete.pdf"
            target.write_bytes(b"incomplete payload")
            marker = self.fetch.write_acquisition_marker(target, workspace)
            inventory = load_script_module("fetch_sources_incomplete_inventory", INVENTORY_PATH)
            config = inventory.load_config(workspace)

            records, warnings, _summary = inventory.build_records(workspace, config, {})

            raw_paths = {
                raw_path
                for record in records
                for raw_path in record.get("raw_paths", [])
                if isinstance(raw_path, str)
            }
            self.assertNotIn("raw/papers/incomplete.pdf", raw_paths)
            self.assertTrue(any("marker-backed incomplete" in warning for warning in warnings))
            self.assertTrue(target.is_file())
            self.assertTrue(marker.is_file())

    def test_cross_process_target_lock_prevents_second_writer_overwrite(self):
        child_code = textwrap.dedent(
            """
            import importlib.util
            import json
            import pathlib
            import sys

            spec = importlib.util.spec_from_file_location("child_fetch_sources", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            target = pathlib.Path(sys.argv[2])
            try:
                with module.acquisition_artifact_transaction(target):
                    module.atomic_write_bytes(target, b"child")
                    module.write_provenance_sidecar(
                        target,
                        origin_url="https://official.example/child",
                        license_value=None,
                        retrieved_by="fetch_sources.py/test",
                    )
                print(json.dumps({"committed": True}))
            except module.FetchSourcesError as exc:
                print(json.dumps({"committed": False, "error_code": exc.error_code}))
            """
        )
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = Path(tmpdir) / "raw" / "papers" / "same.pdf"
            with self.fetch.acquisition_artifact_transaction(target):
                self.fetch.atomic_write_bytes(target, b"parent")
                self.fetch.write_provenance_sidecar(
                    target,
                    origin_url="https://official.example/parent",
                    license_value=None,
                    retrieved_by="fetch_sources.py/test",
                )
                child = subprocess.Popen(
                    [sys.executable, "-c", child_code, str(FETCH_PATH), str(target)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.2)
            stdout, stderr = child.communicate(timeout=10)

            self.assertEqual(0, child.returncode, stderr)
            self.assertEqual(
                {"committed": False, "error_code": "ACQUISITION_TARGET_EXISTS"},
                json.loads(stdout),
            )
            self.assertEqual(b"parent", target.read_bytes())
            provenance = yaml.safe_load(self.fetch.provenance_sidecar_path(target).read_text(encoding="utf-8"))
            self.assertEqual("https://official.example/parent", provenance["origin_url"])

    def test_repeated_invocations_enforce_retained_run_download_budget(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_web_workspace(Path(tmpdir), limit=1)
            run_id = self.write_active_run(workspace)

            def transport(url, _timeout, _headers, _allowed_domains, _max_bytes):
                return self.fetch.result_from_bytes(
                    f"snapshot for {url}".encode(),
                    url=url,
                    content_type="text/html",
                    http_status=200,
                )

            self.install_web_transport(transport)
            common = ("--project-root", str(workspace), "--format", "json", "--run-id", run_id, "web", "get")
            first_code, first_stdout, first_stderr = self.run_fetch(
                *common,
                "--url",
                "https://official.example/first",
            )
            second_code, second_stdout, second_stderr = self.run_fetch(
                *common,
                "--url",
                "https://official.example/second",
            )

            self.assertEqual(0, first_code, first_stderr)
            first = json.loads(first_stdout)
            first_sidecar = yaml.safe_load((workspace / first["sidecar_path"]).read_text(encoding="utf-8"))
            self.assertEqual(run_id, first_sidecar["acquisition_run_id"])
            self.assertEqual(2, second_code)
            self.assertEqual("", second_stdout)
            self.assertEqual("ACQUISITION_LIMIT_EXCEEDED", json.loads(second_stderr)["error_code"])
            self.assertFalse((workspace / "raw" / "web" / "official-example-second.html").exists())

    def test_retained_github_byte_fallback_blocks_cumulative_overrun(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                {
                    "enabled": True,
                    "providers": ["github"],
                    "target_root": "raw/code",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                    "github": {"max_archive_bytes": 10},
                },
            )
            run_id = self.write_active_run(workspace)
            retained = workspace / "raw" / "code" / "first.tar.gz"
            retained.parent.mkdir(parents=True, exist_ok=True)
            retained.write_bytes(b"1234567")
            self.fetch.write_provenance_sidecar(
                retained,
                origin_url="https://github.com/acme/first",
                license_value="MIT",
                retrieved_by=self.fetch.GITHUB_RETRIEVED_BY,
                repository_artifact_kind="source_archive",
                extra={"acquisition_run_id": run_id},
            )
            config = self.fetch.load_config(workspace)
            context = self.fetch.acquisition_context(workspace, config, "github", 1, run_id=run_id)

            usage = self.fetch.retained_acquisition_usage(workspace, context["run_budget"])
            self.assertEqual({"downloads": 1, "github_archive_bytes": 7}, usage)

            refused = workspace / "raw" / "code" / "second.tar.gz"
            with self.assertRaises(self.fetch.FetchSourcesError) as caught:
                with self.fetch.acquisition_artifact_transaction(
                    refused,
                    project_root=workspace,
                    context=context,
                    additional_github_archive_bytes=4,
                ):
                    self.fail("byte budget must fail before payload promotion")
            self.assertEqual("GITHUB_ARCHIVE_BUDGET_EXCEEDED", caught.exception.error_code)
            self.assertFalse(refused.exists())
            self.assertFalse(self.fetch.acquisition_marker_path(refused).exists())

    def test_arxiv_timeout_exhaustion_returns_json_error(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))

            def transport(_url: str, _timeout: float) -> bytes:
                raise TimeoutError("timed out")

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "search",
                "--query",
                "timeout",
                "--max-results",
                "1",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_NETWORK_ERROR", envelope["error_code"])
        self.assertIn("timed out", envelope["message"])

    def test_arxiv_pdf_download_writes_file_checksum_and_request_sidecar(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            urls = []

            def transport(url: str, _timeout: float) -> bytes:
                urls.append(url)
                if "export.arxiv.org/api/query" in url:
                    return ATOM_SEARCH_RESPONSE
                return b"%PDF-1.4\nSynthetic PDF bytes\n"

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "download",
                "--id",
                "2601.00001v1",
                "--format",
                "pdf",
                "--request-id",
                "req-123",
                "--candidate-id",
                "cand-arxiv-123",
            )
            target = workspace / "raw" / "papers" / "2601.00001v1.pdf"
            sidecar = workspace / "raw" / "papers" / "2601.00001v1.pdf.provenance.yml"
            target_bytes = target.read_bytes()
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(
            ["https://export.arxiv.org/api/query", "https://arxiv.org/pdf/2601.00001v1"],
            [url.split("?", 1)[0] for url in urls],
        )
        self.assertEqual(b"%PDF-1.4\nSynthetic PDF bytes\n", target_bytes)
        self.assertEqual("raw/papers/2601.00001v1.pdf", json.loads(stdout)["target_path"])
        self.assertEqual("https://arxiv.org/abs/2601.00001v1", provenance["origin_url"])
        self.assertEqual("fetch_sources.py/arxiv", provenance["retrieved_by"])
        self.assertEqual("unresolved", provenance["license"])
        self.assertEqual("https://arxiv.org/abs/2601.00001v1", provenance["terms_url"])
        self.assertEqual("arxiv", provenance["academic_provider"])
        self.assertEqual("preprint", provenance["academic_source_type"])
        self.assertEqual("arXiv", provenance["venue"])
        self.assertEqual(2026, provenance["publication_year"])
        self.assertEqual("green", provenance["oa_status"])
        self.assertEqual("preprint", provenance["peer_review_status"])
        self.assertEqual("2601.00001v1", provenance["arxiv_id"])
        self.assertEqual("Synthetic Retrieval Paper", provenance["title"])
        self.assertEqual(["Ada Lovelace", "Grace Hopper"], provenance["authors"])
        self.assertEqual("2026-01-01T01:02:03Z", provenance["published"])
        self.assertEqual("10.5555/example", provenance["doi"])
        self.assertEqual("arxiv-atom", provenance["doi_source"])
        self.assertEqual("req-123", provenance["request_id"])
        self.assertEqual("cand-arxiv-123", provenance["candidate_id"])
        self.assertIn("License not inferable", provenance["notes"])
        self.assertRegex(provenance["checksum"], r"^sha256:[0-9a-f]{64}$")

    def test_arxiv_pdf_download_degrades_when_metadata_lookup_fails(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            urls = []

            def transport(url: str, _timeout: float) -> bytes:
                urls.append(url)
                if "export.arxiv.org/api/query" in url:
                    raise URLError("metadata unavailable")
                return b"%PDF-1.4\nSynthetic PDF bytes\n"

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "download",
                "--id",
                "2601.00001v1",
                "--format",
                "pdf",
            )
            sidecar = workspace / "raw" / "papers" / "2601.00001v1.pdf.provenance.yml"
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertEqual("raw/papers/2601.00001v1.pdf", json.loads(stdout)["target_path"])
        self.assertEqual(
            ["https://export.arxiv.org/api/query", "https://arxiv.org/pdf/2601.00001v1"],
            [url.split("?", 1)[0] for url in urls],
        )
        self.assertNotIn("title", provenance)
        self.assertNotIn("authors", provenance)
        self.assertEqual("unresolved", provenance["license"])
        self.assertEqual("https://arxiv.org/abs/2601.00001v1", provenance["terms_url"])
        self.assertIn("metadata lookup failed", provenance["notes"].lower())

    def test_arxiv_source_download_extracts_bundle_and_inventory_records_provenance(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_enabled_workspace(Path(tmpdir))
            archive = tar_gz_bytes(
                {
                    "main.tex": b"\\documentclass{article}\n\\begin{document}\nFetched source.\n\\end{document}\n",
                    "sections/intro.tex": b"Intro evidence.\n",
                }
            )

            def transport(_url: str, _timeout: float) -> bytes:
                if "export.arxiv.org/api/query" in _url:
                    return ATOM_SEARCH_RESPONSE
                return archive

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "download",
                "--id",
                "2601.00001v1",
                "--format",
                "source",
                "--request-id",
                "req-source",
            )

            inventory = load_script_module("fetch_sources_inventory", INVENTORY_PATH)
            with patched_argv("--project-root", str(workspace), "--report"):
                with contextlib.redirect_stdout(io.StringIO()):
                    inventory_code = inventory.main()
            manifest = workspace / "sources" / "manifest.jsonl"
            records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual("raw/papers/arxiv-2601.00001v1", json.loads(stdout)["target_path"])
        self.assertEqual(0, inventory_code)
        paper = next(record for record in records if record["id"] == "paper:2601.00001v1")
        self.assertEqual("raw/papers/arxiv-2601.00001v1", paper["latex_root"])
        self.assertEqual("https://arxiv.org/abs/2601.00001v1", paper["provenance"]["origin_url"])
        self.assertEqual("unresolved", paper["provenance"]["license"])
        self.assertEqual("https://arxiv.org/abs/2601.00001v1", paper["provenance"]["terms_url"])
        self.assertEqual("arxiv", paper["provenance"]["academic_provider"])
        self.assertEqual("preprint", paper["provenance"]["academic_source_type"])
        self.assertEqual("arXiv", paper["provenance"]["venue"])
        self.assertEqual("preprint", paper["provenance"]["peer_review_status"])
        self.assertEqual("2601.00001v1", paper["provenance"]["arxiv_id"])
        self.assertEqual("Synthetic Retrieval Paper", paper["provenance"]["title"])
        self.assertEqual(["Ada Lovelace", "Grace Hopper"], paper["provenance"]["authors"])
        self.assertEqual("2026-01-01T01:02:03Z", paper["provenance"]["published"])
        self.assertEqual("10.5555/example", paper["provenance"]["doi"])
        self.assertEqual("arxiv-atom", paper["provenance"]["doi_source"])
        self.assertEqual("req-source", paper["provenance"]["request_id"])

    def test_arxiv_source_download_rejects_unsafe_archive_member(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            root = Path(tmpdir)
            workspace = self.build_enabled_workspace(root)
            archive = tar_gz_bytes({"../escape.tex": b"escaped"})

            def transport(_url: str, _timeout: float) -> bytes:
                return archive

            self.install_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "download",
                "--id",
                "2601.00001v1",
                "--format",
                "source",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_ARCHIVE_UNSAFE", envelope["error_code"])
        self.assertFalse((root / "escape.tex").exists())

    def test_arxiv_source_download_rejects_nonportable_and_colliding_members(self):
        cases = (
            {"CON.tex": b"reserved"},
            {"A.tex": b"one", "a.tex": b"two"},
            {"caf\u00e9.tex": b"one", "cafe\u0301.tex": b"two"},
        )
        for members in cases:
            with self.subTest(members=tuple(members)), tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
                workspace = self.build_enabled_workspace(Path(tmpdir))
                archive = tar_gz_bytes(members)
                self.install_transport(lambda _url, _timeout, payload=archive: payload)

                code, stdout, stderr = self.run_fetch(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "arxiv",
                    "download",
                    "--id",
                    "2601.00001v1",
                    "--format",
                    "source",
                )

                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertEqual("ACQUISITION_ARCHIVE_UNSAFE", json.loads(stderr)["error_code"])

    def test_acquisition_rejects_symlinked_target_ancestor(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            root = Path(tmpdir)
            workspace = self.build_enabled_workspace(root)
            outside = root / "outside"
            outside.mkdir()
            papers = workspace / "raw" / "papers"
            if papers.exists():
                shutil.rmtree(papers)
            try:
                papers.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks are unavailable on this platform: {exc}")
            self.install_transport(lambda _url, _timeout: b"payload")

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "arxiv",
                "download",
                "--id",
                "2601.00001v1",
                "--format",
                "pdf",
            )
            outside_contents = list(outside.iterdir())

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("ACQUISITION_PATH_UNSAFE", json.loads(stderr)["error_code"])
        self.assertEqual([], outside_contents)

    def test_arxiv_source_download_rejects_member_count_and_uncompressed_size_bombs(self):
        cases = (
            ("ARXIV_MAX_MEMBERS", 1, {"one.tex": b"1", "two.tex": b"2"}),
            ("ARXIV_MAX_UNCOMPRESSED_BYTES", 3, {"large.tex": b"1234"}),
        )
        for constant, limit, members in cases:
            with self.subTest(constant=constant), tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
                workspace = self.build_enabled_workspace(Path(tmpdir))
                archive = tar_gz_bytes(members)
                self.install_transport(lambda _url, _timeout, payload=archive: payload)

                with mock.patch.object(self.fetch, constant, limit):
                    code, stdout, stderr = self.run_fetch(
                        "--project-root",
                        str(workspace),
                        "--format",
                        "json",
                        "arxiv",
                        "download",
                        "--id",
                        "2601.00001v1",
                        "--format",
                        "source",
                    )

                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertEqual("ACQUISITION_ARCHIVE_LIMIT_EXCEEDED", json.loads(stderr)["error_code"])
        self.assertFalse((workspace / "raw" / "papers" / "arxiv-2601.00001v1").exists())

    def test_arxiv_source_archive_enforces_limits_without_materializing_all_headers(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = Path(tmpdir) / "source"
            archive = tar_gz_bytes({"main.tex": b"content"})
            with mock.patch.object(self.fetch.tarfile.TarFile, "getmembers", side_effect=AssertionError("unbounded")):
                self.fetch.extract_arxiv_source_archive(archive, target)

            self.assertEqual(b"content", (target / "main.tex").read_bytes())

    def test_openalex_resolve_parses_exact_work_to_compact_json(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            calls = []
            sleeps = []
            now = [10.0]

            def transport(url: str, timeout: float, headers: dict[str, str]) -> bytes:
                calls.append((url, timeout, headers))
                return OPENALEX_SEARCH_RESPONSE

            def sleep(seconds: float) -> None:
                sleeps.append(seconds)
                now[0] += seconds

            self.install_openalex_transport(transport, clock=lambda: now[0], sleep=sleep)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "resolve",
                "--entity",
                "works",
                "--query",
                "Synthetic Retrieval Paper",
                "--max-results",
                "1",
            )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertNotIn('\n  "', stdout, "OpenAlex JSON should be compact for agent-safe redirection")
        payload = json.loads(stdout)
        self.assertEqual("1.0", payload["schema_version"])
        self.assertEqual("openalex", payload["provider"])
        self.assertEqual("resolve", payload["command"])
        self.assertEqual("Synthetic Retrieval Paper", payload["query"])
        self.assertEqual(1, payload["count"])
        self.assertFalse(payload["api_key_used"])
        self.assertIn("OPENALEX_API_KEY", payload["notice"])
        self.assertEqual("https://openalex.org/W260100001", payload["resolved"]["id"])
        self.assertEqual("https://example.org/synthetic.pdf", payload["resolved"]["oa_pdf_url"])
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(["Synthetic Retrieval Paper"], query["search"])
        self.assertEqual(["1"], query["per_page"])
        self.assertEqual([self.fetch.OPENALEX_SELECT_FIELDS], query["select"])
        self.assertNotIn("api_key", query)
        self.assertEqual(30.0, calls[0][1])
        self.assertIn("User-Agent", calls[0][2])
        self.assertEqual([], sleeps)

    def test_openalex_resolve_requires_exact_enough_result(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return OPENALEX_NON_EXACT_RESPONSE

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "resolve",
                "--entity",
                "works",
                "--query",
                "Synthetic Retrieval Paper",
                "--max-results",
                "1",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("OPENALEX_RESOLUTION_UNCERTAIN", envelope["error_code"])
        self.assertIn("no exact-enough", envelope["message"].lower())
        self.assertIn("get --id-or-doi", envelope["remediation"])

    def test_openalex_resolve_allow_unconfirmed_returns_bounded_probe_report(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return OPENALEX_NON_EXACT_RESPONSE

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "resolve",
                "--entity",
                "works",
                "--query",
                "Synthetic Retrieval Paper",
                "--max-results",
                "1",
                "--allow-unconfirmed",
            )

        self.assertEqual(0, code, stderr)
        payload = json.loads(stdout)
        self.assertEqual("openalex", payload["provider"])
        self.assertEqual("resolve", payload["command"])
        self.assertEqual("Synthetic Retrieval Paper", payload["query"])
        self.assertEqual(1, payload["count"])
        self.assertEqual(1, payload["candidate_count"])
        self.assertEqual(0, payload["exact_match_count"])
        self.assertIsNone(payload["resolved"])
        self.assertEqual("unconfirmed", payload["resolution_status"])
        self.assertIn("not a global nonexistence claim", payload["limitation"])
        self.assertEqual(1, len(payload["results"]))

    def test_openalex_resolve_deduplicates_repeated_work_and_rejects_ambiguous_exact_title(self):
        response = json.dumps(OPENALEX_PROVIDER_CONTRACTS["ambiguous_title_search"]).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return response

            self.install_openalex_transport(transport)
            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "resolve",
                "--entity",
                "works",
                "--query",
                "Synthetic Retrieval Paper",
                "--max-results",
                "3",
            )

            self.install_openalex_transport(transport)
            review_code, review_stdout, review_stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "resolve",
                "--entity",
                "works",
                "--query",
                "Synthetic Retrieval Paper",
                "--max-results",
                "3",
                "--allow-unconfirmed",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("OPENALEX_RESOLUTION_AMBIGUOUS", envelope["error_code"])
        self.assertTrue(envelope["recoverable"])
        self.assertIn("--id-or-doi", envelope["remediation"])

        self.assertEqual(0, review_code, review_stderr)
        review = json.loads(review_stdout)
        self.assertEqual(3, review["provider_result_count"])
        self.assertEqual(2, review["candidate_count"])
        self.assertEqual(2, review["exact_match_count"])
        self.assertEqual("ambiguous", review["resolution_status"])
        self.assertEqual("review", review["recommended_action"])
        self.assertEqual("multiple_distinct_exact_title_matches", review["ambiguity_reason"])
        self.assertIsNone(review["resolved"])

    def test_openalex_get_uses_env_api_key_and_normalizes_doi(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            calls = []

            def transport(url: str, _timeout: float, headers: dict[str, str]) -> bytes:
                calls.append((url, headers))
                return json.dumps(OPENALEX_WORK).encode("utf-8")

            self.install_openalex_transport(transport)
            old_key = os.environ.get("OPENALEX_API_KEY")
            os.environ["OPENALEX_API_KEY"] = "test-openalex-key"
            try:
                code, stdout, stderr = self.run_fetch(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "openalex",
                    "get",
                    "--id-or-doi",
                    "10.5555/openalex",
                )
            finally:
                if old_key is None:
                    os.environ.pop("OPENALEX_API_KEY", None)
                else:
                    os.environ["OPENALEX_API_KEY"] = old_key

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["api_key_used"])
        self.assertNotIn("notice", payload)
        self.assertEqual("https://openalex.org/W260100001", payload["work"]["id"])
        self.assertEqual("https://doi.org/10.5555/openalex", payload["work"]["doi"])
        self.assertIn("/works/doi%3A10.5555%2Fopenalex", urlparse(calls[0][0]).path)
        query = parse_qs(urlparse(calls[0][0]).query)
        self.assertEqual(["test-openalex-key"], query["api_key"])
        self.assertNotIn("test-openalex-key", calls[0][1].get("User-Agent", ""))

    def test_openalex_network_error_redacts_env_api_key(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))

            def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                raise URLError(f"Temporary failure while requesting {url}")

            self.install_openalex_transport(transport)
            old_key = os.environ.get("OPENALEX_API_KEY")
            os.environ["OPENALEX_API_KEY"] = "test-openalex-key"
            try:
                code, stdout, stderr = self.run_fetch(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "openalex",
                    "get",
                    "--id-or-doi",
                    "W260100001",
                )
            finally:
                if old_key is None:
                    os.environ.pop("OPENALEX_API_KEY", None)
                else:
                    os.environ["OPENALEX_API_KEY"] = old_key

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_NETWORK_ERROR", envelope["error_code"])
        self.assertNotIn("test-openalex-key", envelope["message"])
        self.assertNotIn("api_key=test-openalex-key", envelope["message"])
        self.assertIn("api_key=%5BREDACTED%5D", envelope["message"])

    def test_openalex_download_pdf_writes_license_provenance_and_inventory_record(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            calls = []

            def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                calls.append(url)
                parsed = urlparse(url)
                if parsed.netloc == "api.openalex.org":
                    return json.dumps(OPENALEX_WORK).encode("utf-8")
                return b"%PDF-1.4\nOpenAlex PDF bytes\n"

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "download-pdf",
                "--work-id",
                "W260100001",
                "--request-id",
                "req-openalex-pdf",
                "--candidate-id",
                "cand-openalex-pdf",
            )
            target = workspace / "raw" / "papers" / "openalex-W260100001.pdf"
            sidecar = workspace / "raw" / "papers" / "openalex-W260100001.pdf.provenance.yml"
            target_bytes = target.read_bytes()
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

            inventory = load_script_module("fetch_sources_inventory_openalex", INVENTORY_PATH)
            with patched_argv("--project-root", str(workspace), "--report"):
                with contextlib.redirect_stdout(io.StringIO()):
                    inventory_code = inventory.main()
            manifest = workspace / "sources" / "manifest.jsonl"
            records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(
            ["https://api.openalex.org/works/W260100001", "https://example.org/synthetic.pdf"],
            [url.split("?", 1)[0] for url in calls],
        )
        self.assertEqual(b"%PDF-1.4\nOpenAlex PDF bytes\n", target_bytes)
        payload = json.loads(stdout)
        self.assertEqual("raw/papers/openalex-W260100001.pdf", payload["target_path"])
        self.assertEqual("https://openalex.org/W260100001", provenance["origin_url"])
        self.assertEqual("https://example.org/synthetic.pdf", provenance["downloaded_pdf_url"])
        self.assertEqual("fetch_sources.py/openalex", provenance["retrieved_by"])
        self.assertEqual("CC-BY-4.0", provenance["license"])
        self.assertEqual("openalex", provenance["academic_provider"])
        self.assertEqual("journal_article", provenance["academic_source_type"])
        self.assertEqual("Journal of Synthetic Retrieval", provenance["venue"])
        self.assertEqual(2026, provenance["publication_year"])
        self.assertEqual("gold", provenance["oa_status"])
        self.assertEqual("publisher_indexed", provenance["peer_review_status"])
        self.assertEqual("W260100001", provenance["openalex_work_id"])
        self.assertEqual("10.5555/openalex", provenance["doi"])
        self.assertEqual("req-openalex-pdf", provenance["request_id"])
        self.assertEqual("cand-openalex-pdf", provenance["candidate_id"])
        self.assertRegex(provenance["checksum"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(0, inventory_code)
        pdf = next(record for record in records if record["raw_paths"] == ["raw/papers/openalex-W260100001.pdf"])
        self.assertEqual("pdf", pdf["kind"])
        self.assertEqual("https://openalex.org/W260100001", pdf["provenance"]["origin_url"])
        self.assertEqual("https://example.org/synthetic.pdf", pdf["provenance"]["downloaded_pdf_url"])
        self.assertEqual("CC-BY-4.0", pdf["provenance"]["license"])
        self.assertEqual("openalex", pdf["provenance"]["academic_provider"])
        self.assertEqual("journal_article", pdf["provenance"]["academic_source_type"])
        self.assertEqual("Journal of Synthetic Retrieval", pdf["provenance"]["venue"])
        self.assertEqual("publisher_indexed", pdf["provenance"]["peer_review_status"])
        self.assertTrue(pdf["provenance"]["checksum_verified"])

    def test_openalex_get_and_download_preserve_unmapped_provider_license_and_currentness(self):
        work = OPENALEX_PROVIDER_CONTRACTS["unknown_license_work"]
        response = json.dumps(work).encode("utf-8")
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))

            def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return response if (urlparse(url).hostname or "") == "api.openalex.org" else b"%PDF-1.4\n"

            self.install_openalex_transport(transport)
            get_code, get_stdout, get_stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "get",
                "--id-or-doi",
                "W260100004",
                "--output",
                "raw/papers/openalex-W260100004-metadata.json",
            )
            metadata_path = workspace / "raw" / "papers" / "openalex-W260100004-metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata_provenance = yaml.safe_load(
                metadata_path.with_name(f"{metadata_path.name}.provenance.yml").read_text(encoding="utf-8")
            )

            self.install_openalex_transport(transport)
            download_code, download_stdout, download_stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "download-pdf",
                "--work-id",
                "W260100004",
            )
            pdf_path = workspace / "raw" / "papers" / "openalex-W260100004.pdf"
            pdf_provenance = yaml.safe_load(
                pdf_path.with_name(f"{pdf_path.name}.provenance.yml").read_text(encoding="utf-8")
            )

        self.assertEqual(0, get_code, get_stderr)
        get_report = json.loads(get_stdout)
        self.assertEqual("publisher-open-v1", metadata["provider_license_slug"])
        self.assertNotIn("license", metadata)
        self.assertEqual("unresolved", get_report["license"])
        self.assertEqual("publisher-open-v1", get_report["provider_license_slug"])
        self.assertEqual("unresolved", metadata_provenance["license"])
        self.assertEqual("publisher-open-v1", metadata_provenance["provider_license_slug"])
        self.assertEqual("openalex", metadata_provenance["license_source"])
        self.assertEqual(2025, metadata_provenance["publication_year"])

        self.assertEqual(0, download_code, download_stderr)
        download_report = json.loads(download_stdout)
        self.assertEqual("unresolved", download_report["license"])
        self.assertEqual("publisher-open-v1", download_report["provider_license_slug"])
        self.assertEqual("unresolved", pdf_provenance["license"])
        self.assertEqual("publisher-open-v1", pdf_provenance["provider_license_slug"])
        self.assertEqual("openalex", pdf_provenance["license_source"])
        self.assertEqual("https://publisher.example/open-license", pdf_provenance["terms_url"])
        self.assertEqual(2025, pdf_provenance["publication_year"])
        self.assertIn("no safe SPDX mapping", pdf_provenance["notes"])

    def test_openalex_enrich_updates_existing_arxiv_sidecar_with_identity(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            target = workspace / "raw" / "papers" / "2601.00001v1.pdf"
            target.write_bytes(b"%PDF-1.4\nSynthetic PDF bytes\n")
            sidecar = target.with_name("2601.00001v1.pdf.provenance.yml")
            sidecar.write_text(
                yaml.safe_dump(
                    {
                        "origin_url": "https://arxiv.org/abs/2601.00001v1",
                        "license": "unresolved",
                        "retrieved_at": "2026-07-05T12:00:00Z",
                        "retrieved_by": "fetch_sources.py/arxiv",
                        "arxiv_id": "2601.00001v1",
                        "publication_year": 2026,
                        "checksum": "sha256:" + "0" * 64,
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (workspace / "sources" / "manifest.jsonl").write_text(
                json.dumps(
                    {
                        "id": "paper:2601.00001v1",
                        "kind": "pdf",
                        "raw_paths": ["raw/papers/2601.00001v1.pdf"],
                        "status": "pending",
                        "provenance": {
                            "sidecar_path": "raw/papers/2601.00001v1.pdf.provenance.yml",
                            "retrieved_by": "fetch_sources.py/arxiv",
                            "arxiv_id": "2601.00001v1",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            calls = []

            def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                calls.append(url)
                return json.dumps(OPENALEX_WORK).encode("utf-8")

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "enrich",
                "--source-id",
                "paper:2601.00001v1",
            )
            payload = json.loads(stdout)
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertEqual("enrich", payload["command"])
        self.assertTrue(payload["network_io_executed"])
        self.assertEqual(1, payload["resolved_count"])
        self.assertEqual("resolved", payload["results"][0]["status"])
        self.assertTrue(payload["results"][0]["network_io_executed"])
        self.assertIn("/works/doi%3A10.48550%2FarXiv.2601.00001", urlparse(calls[0]).path)
        self.assertEqual("sha256:" + "0" * 64, provenance["checksum"])
        self.assertEqual("W260100001", provenance["openalex_work_id"])
        self.assertEqual("10.5555/openalex", provenance["doi"])
        self.assertEqual("datacite-derived", provenance["doi_source"])
        self.assertEqual("CC-BY-4.0", provenance["license"])
        self.assertEqual("gold", provenance["oa_status"])
        self.assertEqual(2026, provenance["openalex_publication_year"])
        self.assertEqual("resolved", provenance["openalex_enrichment_status"])

    def test_openalex_enrich_records_provider_license_slug_spdx_safely(self):
        cases = [
            ("public-domain", "CC0-1.0"),
            ("cc-by", "unresolved"),
        ]
        for slug, expected_license in cases:
            with self.subTest(slug=slug), tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
                workspace = self.build_openalex_workspace(Path(tmpdir))
                sidecar = self.write_arxiv_source_for_enrichment(workspace)
                work = {
                    **OPENALEX_WORK,
                    "best_oa_location": {
                        **OPENALEX_WORK["best_oa_location"],
                        "license": slug,
                        "license_id": slug,
                    },
                    "locations": [
                        {
                            **OPENALEX_WORK["locations"][0],
                            "license": slug,
                            "license_id": slug,
                        }
                    ],
                }

                response_payload = json.dumps(work).encode("utf-8")

                def transport(
                    _url: str,
                    _timeout: float,
                    _headers: dict[str, str],
                    _response_payload: bytes = response_payload,
                ) -> bytes:
                    return _response_payload

                self.install_openalex_transport(transport)

                code, stdout, stderr = self.run_fetch(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "openalex",
                    "enrich",
                    "--source-id",
                    "paper:2601.00001v1",
                )
                payload = json.loads(stdout)
                provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

            self.assertEqual(0, code, stderr)
            self.assertEqual(1, payload["resolved_count"])
            self.assertEqual(expected_license, provenance["license"])
            self.assertEqual(slug, provenance["provider_license_slug"])
            self.assertEqual("openalex", provenance["license_source"])
            if expected_license == "unresolved":
                self.assertEqual("https://arxiv.org/abs/2601.00001v1", provenance["terms_url"])

    def test_openalex_enrich_records_title_lag_evidence(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            sidecar = self.write_arxiv_source_for_enrichment(
                workspace,
                source_id="paper:2402.02750v2",
                arxiv_id="2402.02750v2",
                title="KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache",
                authors=["Yuhang Li", "Mingzhe Wang"],
                publication_year=2024,
            )
            work = {
                **OPENALEX_WORK,
                "id": "https://openalex.org/W424202750",
                "doi": "https://doi.org/10.48550/arXiv.2402.02750",
                "display_name": "KIVI : Plug-and-play 2bit KV Cache Quantization with Streaming Asymmetric Quantization",
                "publication_year": 2023,
                "authorships": [
                    {"author": {"display_name": "Yuhang Li"}},
                    {"author": {"display_name": "Mingzhe Wang"}},
                ],
            }

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return json.dumps(work).encode("utf-8")

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "enrich",
                "--source-id",
                "paper:2402.02750v2",
            )
            payload = json.loads(stdout)
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, payload["resolved_count"])
        self.assertTrue(provenance["openalex_title_lag"])
        self.assertEqual(work["display_name"], provenance["openalex_reported_title"])
        self.assertEqual(2023, provenance["openalex_reported_publication_year"])
        self.assertEqual("matched", provenance["openalex_identity_evidence"]["authors"])

    def test_openalex_enrich_records_wrong_work_conflict_evidence(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            sidecar = self.write_arxiv_source_for_enrichment(
                workspace,
                source_id="paper:2504.19874v1",
                arxiv_id="2504.19874v1",
                title="TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
                authors=["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
                publication_year=2025,
            )
            work = {
                **OPENALEX_WORK,
                "id": "https://openalex.org/W4416915242",
                "doi": "https://doi.org/10.48550/arXiv.2504.19874",
                "display_name": "Formal Verification of TurboQuant: Machine-Checked Proofs and Gap Closures",
                "publication_year": 2025,
                "authorships": [
                    {"author": {"display_name": "Alex Proof"}},
                    {"author": {"display_name": "Taylor Logic"}},
                ],
            }

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return json.dumps(work).encode("utf-8")

            def doi_transport(_url: str, _timeout: float, _headers: dict[str, str]):
                return self.fetch.result_from_bytes(
                    b"",
                    url="https://arxiv.org/abs/2504.19874",
                    http_status=200,
                    redirect_chain=["https://arxiv.org/abs/2504.19874"],
                )

            self.install_openalex_transport(transport)
            self.install_doi_transport(doi_transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "enrich",
                "--source-id",
                "paper:2504.19874v1",
            )
            payload = json.loads(stdout)
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, payload["resolved_count"])
        self.assertTrue(provenance["openalex_identity_conflict"])
        self.assertEqual(work["display_name"], provenance["openalex_reported_title"])
        self.assertEqual(["Alex Proof", "Taylor Logic"], provenance["openalex_reported_authors"])
        self.assertEqual("mismatch", provenance["openalex_identity_evidence"]["authors"])
        self.assertTrue(provenance["doi_resolution"]["matches_arxiv_id"])
        self.assertEqual("resolved", provenance["doi_resolution"]["status"])
        self.assertEqual("https://arxiv.org/abs/2504.19874", provenance["doi_resolution"]["resolved_url"])

    def test_openalex_enrich_records_uncorroborated_wrong_work_conflict(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            sidecar = self.write_arxiv_source_for_enrichment(
                workspace,
                source_id="paper:2504.19874v1",
                arxiv_id="2504.19874v1",
                title="TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
                authors=["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
                publication_year=2025,
            )
            work = {
                **OPENALEX_WORK,
                "id": "https://openalex.org/W4416915242",
                "doi": "https://doi.org/10.48550/arXiv.2504.19874",
                "display_name": "Formal Verification of TurboQuant: Machine-Checked Proofs and Gap Closures",
                "publication_year": 2025,
                "authorships": [
                    {"author": {"display_name": "Alex Proof"}},
                    {"author": {"display_name": "Taylor Logic"}},
                ],
            }

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return json.dumps(work).encode("utf-8")

            def doi_transport(_url: str, _timeout: float, _headers: dict[str, str]):
                return self.fetch.result_from_bytes(
                    b"",
                    url="https://example.org/not-the-paper",
                    http_status=200,
                    redirect_chain=["https://example.org/not-the-paper"],
                )

            self.install_openalex_transport(transport)
            self.install_doi_transport(doi_transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "enrich",
                "--source-id",
                "paper:2504.19874v1",
            )
            payload = json.loads(stdout)
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, payload["resolved_count"])
        self.assertTrue(provenance["openalex_identity_conflict"])
        self.assertFalse(provenance["doi_resolution"]["matches_arxiv_id"])
        self.assertEqual("redirect_mismatch", provenance["doi_resolution"]["status"])
        self.assertEqual("https://example.org/not-the-paper", provenance["doi_resolution"]["resolved_url"])

    def test_openalex_enrich_records_doi_resolution_network_error_without_failing_command(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            sidecar = self.write_arxiv_source_for_enrichment(
                workspace,
                source_id="paper:2504.19874v1",
                arxiv_id="2504.19874v1",
                title="TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
                authors=["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
                publication_year=2025,
            )
            work = {
                **OPENALEX_WORK,
                "id": "https://openalex.org/W4416915242",
                "doi": "https://doi.org/10.48550/arXiv.2504.19874",
                "display_name": "Formal Verification of TurboQuant: Machine-Checked Proofs and Gap Closures",
                "publication_year": 2025,
                "authorships": [
                    {"author": {"display_name": "Alex Proof"}},
                    {"author": {"display_name": "Taylor Logic"}},
                ],
            }

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return json.dumps(work).encode("utf-8")

            def doi_transport(_url: str, _timeout: float, _headers: dict[str, str]):
                raise TimeoutError("synthetic DOI timeout")

            self.install_openalex_transport(transport)
            self.install_doi_transport(doi_transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "enrich",
                "--source-id",
                "paper:2504.19874v1",
            )
            payload = json.loads(stdout)
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, payload["resolved_count"])
        self.assertTrue(provenance["openalex_identity_conflict"])
        self.assertFalse(provenance["doi_resolution"]["matches_arxiv_id"])
        self.assertEqual("network_error", provenance["doi_resolution"]["status"])
        self.assertIn("synthetic DOI timeout", provenance["doi_resolution"]["error"])

    def test_openalex_enrich_records_unresolved_404_without_failing_command(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            target = workspace / "raw" / "papers" / "2601.00001v1.pdf"
            target.write_bytes(b"%PDF-1.4\nSynthetic PDF bytes\n")
            sidecar = target.with_name("2601.00001v1.pdf.provenance.yml")
            sidecar.write_text(
                yaml.safe_dump(
                    {
                        "origin_url": "https://arxiv.org/abs/2601.00001v1",
                        "license": "unresolved",
                        "retrieved_at": "2026-07-05T12:00:00Z",
                        "retrieved_by": "fetch_sources.py/arxiv",
                        "arxiv_id": "2601.00001v1",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (workspace / "sources" / "manifest.jsonl").write_text(
                json.dumps(
                    {
                        "id": "paper:2601.00001v1",
                        "kind": "pdf",
                        "raw_paths": ["raw/papers/2601.00001v1.pdf"],
                        "status": "pending",
                        "provenance": {
                            "sidecar_path": "raw/papers/2601.00001v1.pdf.provenance.yml",
                            "retrieved_by": "fetch_sources.py/arxiv",
                            "arxiv_id": "2601.00001v1",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                raise HTTPError(url=url, code=404, msg="not found", hdrs=None, fp=None)

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "enrich",
                "--source-id",
                "paper:2601.00001v1",
            )
            payload = json.loads(stdout)
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["network_io_executed"])
        self.assertEqual(0, payload["resolved_count"])
        self.assertEqual(1, payload["unresolved_count"])
        self.assertEqual("unresolved", payload["results"][0]["status"])
        self.assertTrue(payload["results"][0]["network_io_executed"])
        self.assertEqual("unresolved", provenance["openalex_enrichment_status"])
        self.assertEqual("not_found", provenance["openalex_enrichment_error"])

    def test_openalex_enrich_missing_identifier_does_not_report_network_io(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            target = workspace / "raw" / "papers" / "paper-without-id.pdf"
            target.write_bytes(b"%PDF-1.4\nSynthetic PDF bytes\n")
            sidecar = target.with_name("paper-without-id.pdf.provenance.yml")
            sidecar.write_text(
                yaml.safe_dump(
                    {
                        "origin_url": "https://arxiv.org/abs/missing-id",
                        "license": "unresolved",
                        "retrieved_at": "2026-07-05T12:00:00Z",
                        "retrieved_by": "fetch_sources.py/arxiv",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (workspace / "sources" / "manifest.jsonl").write_text(
                json.dumps(
                    {
                        "id": "paper:missing-id",
                        "kind": "pdf",
                        "raw_paths": ["raw/papers/paper-without-id.pdf"],
                        "status": "pending",
                        "provenance": {
                            "sidecar_path": "raw/papers/paper-without-id.pdf.provenance.yml",
                            "retrieved_by": "fetch_sources.py/arxiv",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                raise AssertionError(f"OpenAlex transport should not be called for local preflight miss: {url}")

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "enrich",
                "--source-id",
                "paper:missing-id",
            )
            payload = json.loads(stdout)
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertFalse(payload["network_io_executed"])
        self.assertEqual(0, payload["resolved_count"])
        self.assertEqual(1, payload["unresolved_count"])
        self.assertEqual("unresolved", payload["results"][0]["status"])
        self.assertEqual("missing_arxiv_id_or_doi", payload["results"][0]["reason"])
        self.assertFalse(payload["results"][0]["network_io_executed"])
        self.assertEqual("unresolved", provenance["openalex_enrichment_status"])
        self.assertEqual("missing_arxiv_id_or_doi", provenance["openalex_enrichment_error"])

    def test_openalex_get_output_writes_metadata_snapshot_with_explicit_null_license(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                return json.dumps(
                    {
                        **OPENALEX_CLOSED_WORK,
                        "primary_location": {
                            **OPENALEX_CLOSED_WORK["primary_location"],
                            "landing_page_url": "https://publisher.example/closed",
                            "source": {"display_name": "Closed Synthetic Journal"},
                        },
                    }
                ).encode("utf-8")

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "get",
                "--id-or-doi",
                "W260100003",
                "--output",
                "raw/papers/openalex-W260100003-metadata.json",
                "--request-id",
                "req-openalex-metadata",
            )
            target = workspace / "raw" / "papers" / "openalex-W260100003-metadata.json"
            sidecar = workspace / "raw" / "papers" / "openalex-W260100003-metadata.json.provenance.yml"
            snapshot = json.loads(target.read_text(encoding="utf-8"))
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

            inventory = load_script_module("fetch_sources_inventory_openalex_get", INVENTORY_PATH)
            with patched_argv("--project-root", str(workspace), "--report"):
                with contextlib.redirect_stdout(io.StringIO()):
                    inventory_code = inventory.main()
            records = [
                json.loads(line)
                for line in (workspace / "sources" / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        self.assertEqual(0, code, stderr)
        payload = json.loads(stdout)
        self.assertEqual("raw/papers/openalex-W260100003-metadata.json", payload["target_path"])
        self.assertEqual("raw/papers/openalex-W260100003-metadata.json.provenance.yml", payload["sidecar_path"])
        self.assertEqual("https://openalex.org/W260100003", snapshot["id"])
        self.assertIsNone(payload["license"])
        self.assertIsNone(provenance["license"])
        self.assertEqual("fetch_sources.py/openalex", provenance["retrieved_by"])
        self.assertEqual("openalex", provenance["academic_provider"])
        self.assertEqual("metadata_only", provenance["academic_source_type"])
        self.assertEqual("Closed Synthetic Journal", provenance["venue"])
        self.assertEqual(2026, provenance["publication_year"])
        self.assertEqual("closed", provenance["oa_status"])
        self.assertEqual("publisher_indexed", provenance["peer_review_status"])
        self.assertEqual("W260100003", provenance["openalex_work_id"])
        self.assertEqual("10.5555/openalex", provenance["doi"])
        self.assertEqual("req-openalex-metadata", provenance["request_id"])
        self.assertEqual(0, inventory_code)
        record = next(
            item for item in records if item["raw_paths"] == ["raw/papers/openalex-W260100003-metadata.json"]
        )
        self.assertEqual("unknown", record["kind"])
        self.assertIsNone(record["provenance"]["license"])
        self.assertEqual("metadata_only", record["provenance"]["academic_source_type"])

    def test_openalex_download_pdf_refuses_output_outside_target_root(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))

            def transport(_url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                raise AssertionError("unsafe output must be rejected before network")

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "download-pdf",
                "--work-id",
                "W260100001",
                "--output",
                "raw/other/openalex.pdf",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("CONFIG_INVALID", envelope["error_code"])
        self.assertIn("target_root", envelope["message"])

    def test_openalex_auth_and_rate_limit_errors_return_guidance(self):
        cases = [
            (401, "OPENALEX_AUTH_REQUIRED", "OPENALEX_API_KEY"),
            (403, "OPENALEX_AUTH_REQUIRED", "OPENALEX_API_KEY"),
            (429, "OPENALEX_RATE_LIMITED", "Retry later"),
        ]
        for status, error_code, guidance in cases:
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
                    workspace = self.build_openalex_workspace(Path(tmpdir))

                    def transport(_url: str, _timeout: float, _headers: dict[str, str], status=status) -> bytes:
                        raise HTTPError(
                            url="https://api.openalex.org/works/W260100001",
                            code=status,
                            msg="error",
                            hdrs=None,
                            fp=None,
                        )

                    self.install_openalex_transport(transport)

                    code, stdout, stderr = self.run_fetch(
                        "--project-root",
                        str(workspace),
                        "--format",
                        "json",
                        "openalex",
                        "get",
                        "--id-or-doi",
                        "W260100001",
                    )

                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                envelope = json.loads(stderr)
                self.assertEqual(error_code, envelope["error_code"])
                self.assertIn(guidance, envelope["remediation"])

    def test_urllib_openalex_transport_requests_dns_and_expected_media_types(self):
        # OpenAlex work metadata legitimately points at arbitrary open-access
        # publisher hosts, so both API JSON and publisher PDF routes must use
        # strict DNS validation without relying on a static domain allowlist.
        calls = []

        def spy_bounded_fetch_bytes(url, **kwargs):
            calls.append((url, kwargs))
            return b"{}"

        original = self.fetch.bounded_fetch_bytes
        self.fetch.bounded_fetch_bytes = spy_bounded_fetch_bytes
        try:
            payload = self.fetch.urllib_openalex_transport(
                "https://api.openalex.org/works/W1",
                5.0,
                {"Accept": "application/json"},
            )
            self.fetch.urllib_openalex_transport(
                "https://publisher.example/paper.pdf",
                5.0,
                {"Accept": "application/pdf"},
            )
        finally:
            self.fetch.bounded_fetch_bytes = original

        self.assertEqual(b"{}", payload)
        self.assertEqual(2, len(calls))
        url, kwargs = calls[0]
        self.assertEqual("https://api.openalex.org/works/W1", url)
        self.assertTrue(all(call_kwargs["resolve_hostnames"] for _url, call_kwargs in calls))
        self.assertEqual(self.fetch.OPENALEX_JSON_CONTENT_TYPES, kwargs["expected_content_types"])
        self.assertEqual(self.fetch.OPENALEX_PDF_CONTENT_TYPES, calls[1][1]["expected_content_types"])

    def test_openalex_download_pdf_refuses_non_open_access_work(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_openalex_workspace(Path(tmpdir))
            calls = []

            def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
                calls.append(url)
                return json.dumps(OPENALEX_CLOSED_WORK).encode("utf-8")

            self.install_openalex_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "openalex",
                "download-pdf",
                "--work-id",
                "W260100003",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("OPENALEX_PDF_UNAVAILABLE", envelope["error_code"])
        self.assertIn("open-access", envelope["message"])
        self.assertEqual(["https://api.openalex.org/works/W260100003"], [url.split("?", 1)[0] for url in calls])

    def test_web_get_writes_bounded_snapshot_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_web_workspace(Path(tmpdir))
            calls = []

            def transport(
                url: str,
                timeout: float,
                headers: dict[str, str],
                allowed_domains: list[str],
                max_bytes: int,
            ):
                calls.append((url, timeout, headers, allowed_domains, max_bytes))
                return self.fetch.result_from_bytes(
                    b"<html><main>Official guidance.</main></html>",
                    url=url,
                    content_type="text/html",
                    http_status=200,
                )

            self.install_web_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "web",
                "get",
                "--url",
                "https://official.example/legal/tax.html",
                "--request-id",
                "req-web-123",
                "--candidate-id",
                "cand-web-123",
                "--source-type",
                "official_web",
                "--publisher",
                "Official Example",
                "--jurisdiction",
                "EX",
                "--evidence-area",
                "current_legal_figure",
                "--terms-url",
                "https://official.example/terms",
                "--publication-date",
                "2026-05-06",
                "--effective-date",
                "2026-05-01",
                "--validity-period",
                "2026-01-01/2026-12-31",
                "--date-note",
                "Page states no separate expiration date.",
                "--valid-for-year",
                "2026",
            )

            target = workspace / "raw" / "web" / "official-example-legal-tax.html"
            sidecar = workspace / "raw" / "web" / "official-example-legal-tax.html.provenance.yml"
            target_bytes = target.read_bytes()
            provenance = yaml.safe_load(sidecar.read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        self.assertEqual("web", payload["provider"])
        self.assertEqual("get", payload["command"])
        self.assertEqual("raw/web/official-example-legal-tax.html", payload["target_path"])
        self.assertEqual("raw/web/official-example-legal-tax.html.provenance.yml", payload["sidecar_path"])
        self.assertEqual(b"<html><main>Official guidance.</main></html>", target_bytes)
        self.assertEqual("https://official.example/legal/tax.html", provenance["origin_url"])
        self.assertEqual("https://official.example/legal/tax.html", provenance["url"])
        self.assertEqual("fetch_sources.py/web", provenance["retrieved_by"])
        self.assertEqual("official_web", provenance["source_type"])
        self.assertEqual("cand-web-123", provenance["candidate_id"])
        self.assertEqual("req-web-123", provenance["request_id"])
        self.assertEqual("Official Example", provenance["publisher"])
        self.assertEqual("EX", provenance["jurisdiction"])
        self.assertEqual(["current_legal_figure"], provenance["supported_evidence_areas"])
        self.assertEqual("https://official.example/terms", provenance["terms_url"])
        self.assertEqual("2026-05-06", provenance["publication_date"])
        self.assertEqual("2026-05-01", provenance["effective_date"])
        self.assertEqual("2026-01-01/2026-12-31", provenance["validity_period"])
        self.assertEqual("Page states no separate expiration date.", provenance["date_not_available"])
        self.assertEqual({"valid_for_year": 2026, "note": "Page states no separate expiration date."}, provenance["date_metadata"])
        self.assertEqual(len(target_bytes), provenance["byte_count"])
        self.assertEqual("text/html", provenance["content_type"])
        self.assertEqual(200, provenance["http_status"])
        self.assertTrue(provenance["tls_verified"])
        self.assertRegex(provenance["checksum"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(["official.example"], calls[0][3])
        self.assertEqual(128, calls[0][4])

    def test_web_get_writes_standards_metadata_sidecar_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_web_workspace(Path(tmpdir), allowed_domains=["www.iso.org"])
            metadata_path = workspace / "sources" / "discovery" / "iso-19131-standards.json"
            metadata_path.parent.mkdir(parents=True)
            metadata_path.write_text(
                json.dumps(
                    {
                        "registry_provider": "iso-open-data",
                        "standards_body": "ISO",
                        "designation": "ISO 19131:2022",
                        "title": "Geographic information - Data product specifications",
                        "edition": 2,
                        "publication_date": "2022-11-01",
                        "status": "published",
                        "registry_url": "https://www.iso.org/standard/77442.html",
                        "dataset_license": "ODC-BY-1.0",
                        "attribution_required": True,
                    }
                ),
                encoding="utf-8",
            )

            def transport(url, timeout, headers, allowed_domains, max_bytes):
                return self.fetch.result_from_bytes(
                    b"<html><main>ISO 19131:2022 metadata.</main></html>",
                    url=url,
                    content_type="text/html",
                    http_status=200,
                )

            self.install_web_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "web",
                "get",
                "--url",
                "https://www.iso.org/standard/77442.html",
                "--candidate-id",
                "cand-iso-19131",
                "--source-type",
                "standards_registry_entry",
                "--terms-url",
                "https://www.iso.org/open-data.html",
                "--evidence-area",
                "standards_registry_reference",
                "--standards-metadata",
                "sources/discovery/iso-19131-standards.json",
            )

            payload = json.loads(stdout)
            provenance = yaml.safe_load((workspace / payload["sidecar_path"]).read_text(encoding="utf-8"))

        self.assertEqual(0, code, stderr)
        self.assertEqual("standards_registry_entry", provenance["source_type"])
        self.assertEqual(["standards_registry_reference"], provenance["supported_evidence_areas"])
        self.assertEqual("ISO 19131:2022", provenance["standards"]["designation"])
        self.assertEqual("published", provenance["standards"]["status"])
        self.assertEqual("ODC-BY-1.0", provenance["standards"]["dataset_license"])
        self.assertIsNone(provenance["license"])

    def test_web_get_rejects_invalid_date_metadata_before_transport(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_web_workspace(Path(tmpdir))
            calls = []

            def transport(*_args):
                calls.append("called")
                return b"must not run"

            self.install_web_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "web",
                "get",
                "--url",
                "https://official.example/legal/tax.html",
                "--publication-date",
                "May 6, 2026",
            )

            target = workspace / "raw" / "web" / "official-example-legal-tax.html"
            sidecar = workspace / "raw" / "web" / "official-example-legal-tax.html.provenance.yml"

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual([], calls)
        self.assertFalse(target.exists())
        self.assertFalse(sidecar.exists())
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_DATE_METADATA_INVALID", envelope["error_code"])
        self.assertIn("--publication-date", envelope["message"])

    def test_web_get_requires_allowlisted_domain_before_transport(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_web_workspace(Path(tmpdir), allowed_domains=["official.example"])
            calls = []

            def transport(*_args):
                calls.append("called")
                return b"must not run"

            self.install_web_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "web",
                "get",
                "--url",
                "https://mirror.example/legal/tax.html",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual([], calls)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_DOMAIN_NOT_ALLOWED", envelope["error_code"])

    def test_web_get_refuses_removed_tls_override_without_transport_or_promotion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_web_workspace(Path(tmpdir))
            calls = []

            def transport(*_args):
                calls.append("called")
                raise AssertionError("removed TLS override must refuse before transport")

            self.install_web_transport(transport)

            code, stdout, stderr = self.run_fetch(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "web",
                "get",
                "--url",
                "https://official.example/legal/tls.html",
                "--insecure-tls-documented",
                "internal test proxy certificate",
            )
            target = workspace / "raw" / "web" / "official-example-legal-tls.html"
            sidecar = target.with_name(f"{target.name}.provenance.yml")

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual([], calls)
        self.assertFalse(target.exists())
        self.assertFalse(sidecar.exists())
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_TLS_FAILED", envelope["error_code"])
        self.assertNotIn("internal test proxy certificate", stderr)

    def test_web_get_rejects_unverified_or_wrong_mime_result_before_promotion(self):
        cases = (
            {"tls_verified": False, "content_type": "text/html", "status": 200, "code": "ACQUISITION_TLS_FAILED"},
            {"tls_verified": True, "content_type": "application/json", "status": 200, "code": "ACQUISITION_MIME_UNEXPECTED"},
            {"tls_verified": True, "content_type": "text/html", "status": 503, "code": "ACQUISITION_STATUS_UNEXPECTED"},
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.build_web_workspace(Path(tmpdir))
                url = f"https://official.example/legal/refused-{index}.html"

                def transport(*_args, value=case, target_url=url):
                    return self.fetch.result_from_bytes(
                        b"<html>must not be promoted</html>",
                        url=target_url,
                        content_type=value["content_type"],
                        http_status=value["status"],
                        tls_verified=value["tls_verified"],
                    )

                self.install_web_transport(transport)
                code, stdout, stderr = self.run_fetch(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "web",
                    "get",
                    "--url",
                    url,
                )
                target = workspace / "raw" / "web" / f"official-example-legal-refused-{index}.html"

                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertEqual(case["code"], json.loads(stderr)["error_code"])
                self.assertFalse(target.exists())
                self.assertFalse(target.with_name(f"{target.name}.provenance.yml").exists())


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.fetch = load_script_module("research_fetch_sources_registry", FETCH_PATH)

    def test_every_registry_entry_shares_metadata_shape(self):
        required = {"provider_id", "terms_urls", "supported_commands", "license_inference"}
        for provider, entry in self.fetch.PROVIDER_REGISTRY.items():
            with self.subTest(provider=provider):
                self.assertEqual(provider, entry["provider_id"])
                self.assertLessEqual(required, set(entry))
                self.assertIsInstance(entry["terms_urls"], list)
                self.assertTrue(entry["terms_urls"], "every provider must document terms URLs")
                self.assertIsInstance(entry["supported_commands"], list)

    def test_github_registry_entry_documents_terms_and_license_inference(self):
        registry = self.fetch.PROVIDER_REGISTRY
        self.assertIn("github", registry)
        entry = registry["github"]
        self.assertEqual("github", entry["provider_id"])
        for url in entry["terms_urls"]:
            self.assertTrue(url.startswith("https://docs.github.com/"), url)
        # E32-T03 registers bounded acquisition commands for selected repos.
        self.assertEqual(
            ["repo-metadata", "release-metadata", "download-archive"],
            entry["supported_commands"],
        )
        self.assertEqual("partial", entry["license_inference"])

    def test_workspace_can_allow_list_github(self):
        providers = self.fetch.validate_provider_list(
            ["github"],
            "integrations.acquisition.providers",
            require_non_empty=True,
        )
        self.assertEqual(["github"], providers)

    def test_unknown_provider_is_still_rejected_and_lists_github(self):
        with self.assertRaises(SystemExit) as ctx:
            self.fetch.validate_provider_list(
                ["gitlab"],
                "integrations.acquisition.providers",
                require_non_empty=True,
            )
        message = str(ctx.exception)
        self.assertIn("unknown provider", message)
        self.assertIn("gitlab", message)
        self.assertIn("github", message)

    def test_init_and_smoke_allow_lists_match_provider_registry(self):
        init = load_script_module("research_init_workspace_registry", INIT_PATH)
        smoke = load_script_module("research_smoke_validate_registry", SMOKE_PATH)
        registry = set(self.fetch.PROVIDER_REGISTRY)
        self.assertEqual(registry, set(init.ACQUISITION_ALLOWED_PROVIDERS))
        self.assertEqual(registry, set(smoke.ACQUISITION_ALLOWED_PROVIDERS))


def github_repo_metadata(
    full_name: str,
    *,
    license_key: str | None = "MIT",
    size: int = 8,
    default_branch: str = "main",
    archived: bool = False,
    fork: bool = False,
) -> bytes:
    license_obj = {"spdx_id": license_key, "key": (license_key or "").lower()} if license_key is not None else None
    return json.dumps(
        {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "description": "Selected repository under test",
            "default_branch": default_branch,
            "size": size,
            "stargazers_count": 1200,
            "forks_count": 80,
            "archived": archived,
            "fork": fork,
            "pushed_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
            "license": license_obj,
        }
    ).encode("utf-8")


def github_commit_payload(sha: str = "a" * 40) -> bytes:
    return json.dumps({"sha": sha, "commit": {"message": "head"}}).encode("utf-8")


def github_release_payload(tag: str = "v1.0.0", *, assets: list[dict] | None = None) -> bytes:
    return json.dumps(
        {
            "tag_name": tag,
            "name": f"Release {tag}",
            "html_url": f"https://github.com/acme/tool/releases/tag/{tag}",
            "published_at": "2026-01-03T00:00:00Z",
            "draft": False,
            "prerelease": False,
            "tarball_url": f"https://api.github.com/repos/acme/tool/tarball/{tag}",
            "zipball_url": f"https://api.github.com/repos/acme/tool/zipball/{tag}",
            "assets": assets
            if assets is not None
            else [
                {
                    "name": "tool-1.0.0.whl",
                    "content_type": "application/octet-stream",
                    "size": 4096,
                    "browser_download_url": "https://github.com/acme/tool/releases/download/v1.0.0/tool-1.0.0.whl",
                    "updated_at": "2026-01-03T00:00:00Z",
                }
            ],
        }
    ).encode("utf-8")


class GithubAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.fetch = load_script_module("research_fetch_sources_github", FETCH_PATH)
        self._saved_token = os.environ.pop("GITHUB_TOKEN", None)
        self.addCleanup(self._restore_token)
        self.addCleanup(self._reset_transport)

    def _restore_token(self):
        if self._saved_token is not None:
            os.environ["GITHUB_TOKEN"] = self._saved_token
        else:
            os.environ.pop("GITHUB_TOKEN", None)

    def _reset_transport(self):
        self.fetch.GITHUB_TRANSPORT = None
        self.fetch.GITHUB_LAST_REQUEST_AT = None

    def build_github_workspace(self, root: Path, github: dict | None = None) -> Path:
        workspace = root / "workspace"
        for relative in ("raw/papers", "raw/code", "sources", "wiki"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        acquisition = {
            "enabled": True,
            "providers": ["github"],
            "target_root": "raw/papers",
            "max_downloads_per_run": 10,
            "require_license_check": True,
        }
        if github is not None:
            acquisition["github"] = github
        config = {
            "project": {"name": "github-acquisition-test"},
            "raw": {"source_roots": ["raw/papers", "raw/code"]},
            "sources": {"manifest_path": "sources/manifest.jsonl"},
            "wiki": {"root": "wiki", "required_dirs": []},
            "integrations": {"acquisition": acquisition},
        }
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return workspace

    def install_github_transport(self, routes) -> list:
        """Install a URL-routed transport. routes is an ordered list of
        (fragment, response-bytes-or-Exception); the first matching fragment wins,
        so list specific paths before the bare /repos/ entry."""
        calls: list = []

        def transport(url, timeout, headers):
            calls.append((url, headers))
            for fragment, response in routes:
                if fragment in url:
                    if isinstance(response, Exception):
                        raise response
                    return response
            raise AssertionError(f"unexpected GitHub URL: {url}")

        self.fetch.GITHUB_TRANSPORT = transport
        self.fetch.GITHUB_CLOCK = lambda: 0.0
        self.fetch.GITHUB_SLEEP = lambda _seconds: None
        self.fetch.GITHUB_LAST_REQUEST_AT = None
        return calls

    def run_fetch(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.fetch.main(list(args))
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    # --- repository metadata snapshot -----------------------------------

    def test_repo_metadata_writes_snapshot_and_provenance(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            self.install_github_transport([("/repos/", github_repo_metadata("acme/tool", license_key="MIT"))])
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--repo", "acme/tool",
            )
            target = workspace / "raw" / "code" / "github-acme-tool-metadata.json"
            snapshot = json.loads(target.read_text(encoding="utf-8"))
            provenance = yaml.safe_load(
                (workspace / "raw" / "code" / "github-acme-tool-metadata.json.provenance.yml").read_text()
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        report = json.loads(stdout)
        self.assertEqual("github", report["provider"])
        self.assertEqual("repo-metadata", report["command"])
        self.assertEqual("raw/code/github-acme-tool-metadata.json", report["target_path"])
        self.assertEqual("MIT", report["license"])
        self.assertFalse(report["token_used"])
        self.assertEqual("acme/tool", snapshot["full_name"])
        self.assertEqual("MIT", snapshot["license"])
        self.assertEqual("https://github.com/acme/tool", provenance["origin_url"])
        self.assertEqual("fetch_sources.py/github", provenance["retrieved_by"])
        self.assertEqual("acme", provenance["repository_owner"])
        self.assertEqual("tool", provenance["repository_name"])
        self.assertEqual("acme/tool", provenance["repository_full_name"])
        self.assertEqual("repository_metadata", provenance["repository_artifact_kind"])
        self.assertEqual("main", provenance["repository_ref"])
        self.assertEqual("MIT", provenance["license"])
        self.assertRegex(provenance["checksum"], r"^sha256:[0-9a-f]{64}$")

    def test_repo_metadata_accepts_url_and_records_license_uncertainty(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            self.install_github_transport([("/repos/", github_repo_metadata("acme/tool", license_key=None))])
            code, stdout, _ = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--url", "https://github.com/acme/tool",
            )
            provenance = yaml.safe_load(
                (workspace / "raw" / "code" / "github-acme-tool-metadata.json.provenance.yml").read_text()
            )
        self.assertEqual(0, code)
        self.assertIsNone(json.loads(stdout)["license"])
        self.assertIsNone(provenance["license"])
        self.assertIn("License not detected", provenance["notes"])

    # --- release asset metadata -----------------------------------------

    def test_release_metadata_latest_writes_snapshot(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            self.install_github_transport([("/releases/latest", github_release_payload("v1.0.0"))])
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "release-metadata", "--repo", "acme/tool",
            )
            snapshot = json.loads(
                (workspace / "raw" / "code" / "github-acme-tool-release-latest.json").read_text(encoding="utf-8")
            )
            provenance = yaml.safe_load(
                (workspace / "raw" / "code" / "github-acme-tool-release-latest.json.provenance.yml").read_text()
            )
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("release-metadata", report["command"])
        self.assertEqual("v1.0.0", report["tag"])
        self.assertEqual(1, report["asset_count"])
        self.assertEqual("tool-1.0.0.whl", snapshot["assets"][0]["name"])
        self.assertEqual("acme", provenance["repository_owner"])
        self.assertEqual("tool", provenance["repository_name"])
        self.assertEqual("acme/tool", provenance["repository_full_name"])
        self.assertEqual("release_metadata", provenance["repository_artifact_kind"])
        self.assertEqual("v1.0.0", provenance["repository_ref"])
        # Release metadata is a snapshot only; no asset bytes are downloaded.
        self.assertEqual(4096, snapshot["assets"][0]["size"])

    def test_release_metadata_missing_release_returns_envelope(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            self.install_github_transport(
                [("/releases/latest", HTTPError(url="x", code=404, msg="not found", hdrs=None, fp=None))]
            )
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "release-metadata", "--repo", "acme/tool",
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("GITHUB_RELEASE_UNAVAILABLE", json.loads(stderr)["error_code"])

    # --- source archive download ----------------------------------------

    def test_download_archive_writes_file_provenance_without_extraction(self):
        archive = tar_gz_bytes({"tool/main.py": b"print('hi')\n", "tool/README.md": b"# tool\n"})
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            calls = self.install_github_transport(
                [
                    ("/tarball/", archive),
                    ("/commits/", github_commit_payload("c" * 40)),
                    ("/repos/", github_repo_metadata("acme/tool", license_key="Apache-2.0", size=2)),
                ]
            )
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "download-archive", "--repo", "acme/tool", "--ref", "main",
                "--request-id", "req-1a2b3c4d5e",
            )
            target = workspace / "raw" / "code" / "github-acme-tool-main.tar.gz"
            target_bytes = target.read_bytes()
            provenance = yaml.safe_load(
                (workspace / "raw" / "code" / "github-acme-tool-main.tar.gz.provenance.yml").read_text()
            )
            # No directory was extracted next to the archive: the repo code never
            # lands on disk as runnable files, so nothing can be executed.
            code_dir = workspace / "raw" / "code"
            extracted = [p for p in code_dir.iterdir() if p.is_dir()]

        self.assertEqual(0, code, stderr)
        self.assertEqual(archive, target_bytes)
        self.assertEqual([], extracted, "archive must be stored as a file, never extracted")
        report = json.loads(stdout)
        self.assertEqual("download-archive", report["command"])
        self.assertEqual("main", report["ref"])
        self.assertEqual("c" * 40, report["commit_sha"])
        self.assertEqual("Apache-2.0", report["license"])
        self.assertEqual(len(archive), report["archive_bytes"])
        self.assertEqual("https://api.github.com/repos/acme/tool/tarball/main", provenance["downloaded_archive_url"])
        self.assertEqual("acme", provenance["repository_owner"])
        self.assertEqual("tool", provenance["repository_name"])
        self.assertEqual("acme/tool", provenance["repository_full_name"])
        self.assertEqual("source_archive", provenance["repository_artifact_kind"])
        self.assertEqual("main", provenance["repository_ref"])
        self.assertEqual("c" * 40, provenance["commit_sha"])
        self.assertEqual("Apache-2.0", provenance["license"])
        self.assertEqual("req-1a2b3c4d5e", provenance["request_id"])
        self.assertRegex(provenance["checksum"], r"^sha256:[0-9a-f]{64}$")
        # tarball endpoint was used; no /contents/ or /git/ file reads.
        urls = [url for url, _ in calls]
        self.assertTrue(any("/tarball/main" in url for url in urls))
        for url in urls:
            for fragment in ("/contents", "/git/", "/zipball"):
                self.assertNotIn(fragment, url)

    def test_download_archive_refuses_oversize_repo_metadata_before_download(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir), github={"max_archive_bytes": 1024})
            calls = self.install_github_transport(
                [
                    ("/tarball/", AssertionError("must not download archive after size refusal")),
                    ("/commits/", github_commit_payload()),
                    ("/repos/", github_repo_metadata("acme/tool", size=5000)),  # 5000 KB > 1024 bytes
                ]
            )
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "download-archive", "--repo", "acme/tool", "--ref", "main",
            )
            target = workspace / "raw" / "code" / "github-acme-tool-main.tar.gz"
            wrote_file = target.exists()

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("GITHUB_ARCHIVE_TOO_LARGE", json.loads(stderr)["error_code"])
        self.assertFalse(wrote_file, "no archive should be written when size metadata exceeds the limit")
        self.assertFalse(any("/tarball/" in url for url, _ in calls))

    def test_download_archive_refuses_oversize_downloaded_bytes(self):
        big_archive = tar_gz_bytes({"tool/data.bin": b"x" * 4096})
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir), github={"max_archive_bytes": 32})
            self.install_github_transport(
                [
                    ("/tarball/", big_archive),
                    ("/commits/", github_commit_payload()),
                    ("/repos/", github_repo_metadata("acme/tool", size=0)),  # passes the pre-check
                ]
            )
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "download-archive", "--repo", "acme/tool", "--ref", "main",
            )
            target = workspace / "raw" / "code" / "github-acme-tool-main.tar.gz"
            wrote_file = target.exists()

        self.assertEqual(2, code)
        self.assertEqual("GITHUB_ARCHIVE_TOO_LARGE", json.loads(stderr)["error_code"])
        self.assertFalse(wrote_file, "an oversized downloaded archive must not be written to disk")

    # --- explicit selection / validation --------------------------------

    def test_requires_exactly_one_of_repo_or_url(self):
        for selector in ([], ["--repo", "acme/tool", "--url", "https://github.com/acme/tool"]):
            with self.subTest(selector=selector):
                with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
                    workspace = self.build_github_workspace(Path(tmpdir))
                    self.install_github_transport([("/repos/", github_repo_metadata("acme/tool"))])
                    code, stdout, stderr = self.run_fetch(
                        "--project-root", str(workspace), "--format", "json",
                        "github", "repo-metadata", *selector,
                    )
                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertEqual("GITHUB_REPO_INVALID", json.loads(stderr)["error_code"])

    def test_invalid_ref_is_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            self.install_github_transport([("/repos/", github_repo_metadata("acme/tool"))])
            code, _, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "download-archive", "--repo", "acme/tool", "--ref", "../etc/passwd",
            )
        self.assertEqual(2, code)
        self.assertEqual("GITHUB_REPO_INVALID", json.loads(stderr)["error_code"])

    # --- token handling --------------------------------------------------

    def test_token_authenticates_without_leaking_value(self):
        secret = "ghp_supersecrettokenvalue1234567890"
        os.environ["GITHUB_TOKEN"] = secret
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            seen = {}

            def transport(url, timeout, headers):
                seen.update(headers)
                return github_repo_metadata("acme/tool")

            self.fetch.GITHUB_TRANSPORT = transport
            self.fetch.GITHUB_CLOCK = lambda: 0.0
            self.fetch.GITHUB_SLEEP = lambda _s: None
            self.fetch.GITHUB_LAST_REQUEST_AT = None
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--repo", "acme/tool",
            )
            sidecar_text = (
                workspace / "raw" / "code" / "github-acme-tool-metadata.json.provenance.yml"
            ).read_text(encoding="utf-8")
            snapshot_text = (workspace / "raw" / "code" / "github-acme-tool-metadata.json").read_text()

        self.assertEqual(0, code, stderr)
        self.assertEqual(f"Bearer {secret}", seen["Authorization"])
        self.assertTrue(json.loads(stdout)["token_used"])
        for blob in (stdout, stderr, sidecar_text, snapshot_text):
            self.assertNotIn(secret, blob)

    def test_token_is_redacted_when_provider_exception_echoes_it(self):
        secret = "github-network-error-canary"
        old_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GITHUB_TOKEN"] = secret
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.build_github_workspace(Path(tmpdir))

                def transport(_url, _timeout, _headers):
                    raise URLError(
                        f"provider echoed {secret} at https://api.github.com/repos/acme/tool?access_token={secret}"
                    )

                self.fetch.GITHUB_TRANSPORT = transport
                self.fetch.GITHUB_CLOCK = lambda: 0.0
                self.fetch.GITHUB_SLEEP = lambda _seconds: None
                self.fetch.GITHUB_LAST_REQUEST_AT = None
                code, stdout, stderr = self.run_fetch(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "github",
                    "repo-metadata",
                    "--repo",
                    "acme/tool",
                )
        finally:
            if old_token is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = old_token

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("ACQUISITION_NETWORK_ERROR", json.loads(stderr)["error_code"])
        self.assertNotIn(secret, stderr)
        self.assertIn("access_token=%5BREDACTED%5D", stderr)

    def test_unauthenticated_sends_no_authorization_header(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            calls = self.install_github_transport([("/repos/", github_repo_metadata("acme/tool"))])
            code, stdout, _ = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--repo", "acme/tool",
            )
        self.assertEqual(0, code)
        self.assertNotIn("Authorization", calls[0][1])
        self.assertFalse(json.loads(stdout)["token_used"])

    # --- auth / rate limit ----------------------------------------------

    def test_auth_failure_returns_envelope(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            self.install_github_transport(
                [("/repos/", HTTPError(url="x", code=401, msg="bad creds", hdrs=None, fp=None))]
            )
            code, _, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--repo", "acme/tool",
            )
        self.assertEqual(2, code)
        envelope = json.loads(stderr)
        self.assertEqual("GITHUB_AUTH_REQUIRED", envelope["error_code"])
        self.assertIn("GITHUB_TOKEN", envelope["remediation"])

    def test_rate_limit_returns_envelope(self):
        for status in (403, 429):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
                    workspace = self.build_github_workspace(Path(tmpdir))
                    self.install_github_transport(
                        [("/repos/", HTTPError(url="x", code=status, msg="limited", hdrs=None, fp=None))]
                    )
                    code, _, stderr = self.run_fetch(
                        "--project-root", str(workspace), "--format", "json",
                        "github", "repo-metadata", "--repo", "acme/tool",
                    )
                self.assertEqual(2, code)
                self.assertEqual("GITHUB_RATE_LIMITED", json.loads(stderr)["error_code"])

    # --- acquisition gate ------------------------------------------------

    def test_disabled_acquisition_does_not_call_transport(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            (workspace / "research.yml").write_text(
                yaml.safe_dump(
                    {
                        "project": {"name": "x"},
                        "raw": {"source_roots": ["raw/code"]},
                        "sources": {"manifest_path": "sources/manifest.jsonl"},
                        "wiki": {"root": "wiki", "required_dirs": []},
                        "integrations": {"acquisition": {"enabled": False, "providers": []}},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            called = []
            self.fetch.GITHUB_TRANSPORT = lambda *a: called.append(a) or b"{}"
            self.fetch.GITHUB_LAST_REQUEST_AT = None
            code, stdout, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--repo", "acme/tool",
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual([], called)
        self.assertEqual("ACQUISITION_DISABLED", json.loads(stderr)["error_code"])

    def test_provider_not_allow_listed_is_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            (workspace / "research.yml").write_text(
                yaml.safe_dump(
                    {
                        "project": {"name": "x"},
                        "raw": {"source_roots": ["raw/code"]},
                        "sources": {"manifest_path": "sources/manifest.jsonl"},
                        "wiki": {"root": "wiki", "required_dirs": []},
                        "integrations": {"acquisition": {"enabled": True, "providers": ["arxiv"], "target_root": "raw/papers"}},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            self.install_github_transport([("/repos/", github_repo_metadata("acme/tool"))])
            code, _, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--repo", "acme/tool",
            )
        self.assertEqual(2, code)
        self.assertEqual("ACQUISITION_PROVIDER_DISABLED", json.loads(stderr)["error_code"])

    def test_existing_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            existing = workspace / "raw" / "code" / "github-acme-tool-metadata.json"
            existing.parent.mkdir(parents=True, exist_ok=True)
            existing.write_text("{}\n", encoding="utf-8")
            self.install_github_transport([("/repos/", github_repo_metadata("acme/tool"))])
            code, _, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "repo-metadata", "--repo", "acme/tool",
            )
        self.assertEqual(2, code)
        self.assertEqual("ACQUISITION_TARGET_EXISTS", json.loads(stderr)["error_code"])

    # --- inventory fixture: code artifact becomes a manifest record ------

    def test_downloaded_archive_is_inventoried_with_provenance_and_no_execution(self):
        archive = tar_gz_bytes({"tool/main.py": b"print('hi')\n"})
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_github_workspace(Path(tmpdir))
            self.install_github_transport(
                [
                    ("/tarball/", archive),
                    ("/commits/", github_commit_payload("d" * 40)),
                    ("/repos/", github_repo_metadata("acme/tool", license_key="MIT", size=1)),
                ]
            )
            code, _, stderr = self.run_fetch(
                "--project-root", str(workspace), "--format", "json",
                "github", "download-archive", "--repo", "acme/tool", "--ref", "main",
            )
            self.assertEqual(0, code, stderr)

            inventory = load_script_module("github_acquisition_inventory", INVENTORY_PATH)
            inv_stderr = io.StringIO()
            with patched_argv("--project-root", str(workspace), "--report"):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(inv_stderr):
                    inventory_code = inventory.main()
            manifest = workspace / "sources" / "manifest.jsonl"
            records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
            warnings_text = inv_stderr.getvalue()

        self.assertEqual(0, inventory_code)
        archive_records = [r for r in records if "github-acme-tool-main.tar.gz" in str(r.get("raw_paths"))]
        self.assertEqual(1, len(archive_records), records)
        record = archive_records[0]
        self.assertEqual("code_archive", record["kind"])
        self.assertEqual("MIT", record["provenance"]["license"])
        self.assertEqual("acme", record["provenance"]["repository_owner"])
        self.assertEqual("tool", record["provenance"]["repository_name"])
        self.assertEqual("acme/tool", record["provenance"]["repository_full_name"])
        self.assertEqual("source_archive", record["provenance"]["repository_artifact_kind"])
        self.assertEqual("main", record["provenance"]["repository_ref"])
        self.assertEqual("d" * 40, record["provenance"]["commit_sha"])
        self.assertTrue(record["provenance"]["checksum_verified"])
        # The new provenance fields are recognized, not ignored as unknown.
        self.assertNotIn("unknown provenance field", warnings_text)


if __name__ == "__main__":
    unittest.main()
