"""Tests for GitHub repository candidate discovery (E32-T02).

`discover_sources.py github --query TEXT` searches GitHub repository metadata
through a transport-injected adapter and emits `source_candidate` records to
`sources/discovery/candidates.jsonl` with `network_io_executed: true`. It never
clones a repository, downloads an archive, or reads file contents.

These tests use a mocked transport (no real network) to cover repository search,
license propagation, archived repos, forks, candidate ranking, rate limiting,
auth failure, idempotent appends, and GITHUB_TOKEN handling that never leaks the
token value.
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
from urllib.error import HTTPError, URLError

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


DISCOVER = load_script_module("discover_sources_github_under_test", "discover_sources.py")


def repo(
    full_name: str,
    *,
    description: str = "",
    default_branch: str = "main",
    license_key: str | None = "MIT",
    stars: int = 0,
    forks: int = 0,
    archived: bool = False,
    fork: bool = False,
    pushed_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    owner, name = full_name.split("/", 1)
    license_obj = {"spdx_id": license_key, "key": (license_key or "").lower()} if license_key is not None else None
    return {
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "html_url": f"https://github.com/{full_name}",
        "url": f"https://api.github.com/repos/{full_name}",
        "description": description,
        "default_branch": default_branch,
        "license": license_obj,
        "stargazers_count": stars,
        "forks_count": forks,
        "archived": archived,
        "fork": fork,
        "pushed_at": pushed_at,
    }


def search_payload(*repos: dict) -> bytes:
    return json.dumps(
        {"total_count": len(repos), "incomplete_results": False, "items": list(repos)}
    ).encode("utf-8")


class GithubDiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Always start without a token unless a test sets one, and never leak a
        # real environment token into the assertions.
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

    def write_workspace(self, root: Path, *, enabled: bool = True) -> Path:
        target = root / "workspace"
        target.mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: github-discovery-fixture",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
            "    providers:",
            "      - github",
        ]
        (target / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    def install_transport(self, transport) -> None:
        DISCOVER.GITHUB_TRANSPORT = transport
        DISCOVER.GITHUB_CLOCK = lambda: 0.0
        DISCOVER.GITHUB_SLEEP = lambda _seconds: None
        DISCOVER.GITHUB_LAST_REQUEST_AT = None

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
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

    # --- repository search ----------------------------------------------

    def test_repository_search_emits_candidates_and_writes_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            calls = []

            def transport(url, timeout, headers):
                calls.append((url, timeout, headers))
                return search_payload(
                    repo(
                        "acme/rag-toolkit",
                        description="Retrieval augmented generation toolkit",
                        license_key="MIT",
                        stars=1200,
                        forks=80,
                    )
                )

            self.install_transport(transport)
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github",
                 "--query", "retrieval augmented generation", "--max-results", "5"]
            )
            # Read the durable store before the temp workspace is torn down.
            stored_ids = [record["candidate_id"] for record in self.store_records(workspace)]

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        report = json.loads(stdout)
        self.assertEqual("github", report["provider"])
        self.assertEqual(1, report["count"])
        self.assertEqual(1, report["written"])
        self.assertTrue(report["network_io_executed"])
        self.assertEqual("sources/discovery/candidates.jsonl", report["candidates_path"])

        candidate = report["candidates"][0]
        self.assertEqual("code_repository", candidate["source_type"])
        self.assertEqual("https://github.com/acme/rag-toolkit", candidate["url"])
        self.assertEqual("acme/rag-toolkit", candidate["github"]["full_name"])
        self.assertEqual("acme", candidate["github"]["owner"])
        self.assertEqual("main", candidate["github"]["default_branch"])
        self.assertEqual("Retrieval augmented generation toolkit", candidate["github"]["description"])
        self.assertEqual(1200, candidate["github"]["stars"])
        self.assertEqual(80, candidate["github"]["forks"])
        self.assertEqual(
            "https://github.com/acme/rag-toolkit/releases/latest",
            candidate["github"]["latest_release_url"],
        )
        # Discovery never confirms canonical ownership -> review, not fetch.
        self.assertIsNone(candidate["request_id"])
        self.assertIsNone(candidate["seed_source_id"])
        self.assertRegex(candidate["discovery_run_id"], r"^disc-[0-9a-f]{10}$")
        self.assertIsNone(candidate["official_source"])
        self.assertEqual("review", candidate["recommended_action"])
        self.assertTrue(candidate["network_io_executed"])

        # Bounded single search request hit the repositories endpoint.
        self.assertEqual(1, len(calls))
        self.assertIn("/search/repositories", calls[0][0])
        self.assertIn("per_page=5", calls[0][0])

        # The candidate is durably stored under sources/discovery/.
        self.assertEqual([candidate["candidate_id"]], stored_ids)

    def test_search_does_not_fetch_repository_contents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            urls = []

            def transport(url, timeout, headers):
                urls.append(url)
                return search_payload(repo("acme/tool"))

            self.install_transport(transport)
            self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )

        # Exactly one request, and only to the search endpoint: no archive,
        # tarball, contents, or git tree endpoints are ever touched.
        self.assertEqual(1, len(urls))
        for fragment in ("/tarball", "/zipball", "/contents", "/archive/", "/git/"):
            self.assertNotIn(fragment, urls[0])

    # --- license propagation --------------------------------------------

    def test_license_propagates_and_absence_flags_uncertainty(self):
        cases = [
            ("MIT", "MIT", False),
            (None, None, True),
            ("NOASSERTION", None, True),  # GitHub's "detected but unknown" sentinel
        ]
        for raw, expected, uncertain in cases:
            with self.subTest(license=raw):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace = self.write_workspace(Path(tmpdir))
                    self.install_transport(lambda *_, raw=raw: search_payload(repo("acme/tool", license_key=raw)))
                    code, stdout, _ = self.run_cli(
                        ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
                    )
                self.assertEqual(0, code)
                candidate = json.loads(stdout)["candidates"][0]
                self.assertEqual(expected, candidate["license"])
                self.assertEqual(expected, candidate["github"]["license_key"])
                self.assertEqual(uncertain, "license_uncertain" in candidate["reasoning"]["risk_flags"])

    # --- archived repos --------------------------------------------------

    def test_archived_repo_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.install_transport(lambda *_: search_payload(repo("acme/tool", archived=True)))
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertTrue(candidate["github"]["archived"])
        self.assertIn("archived", candidate["reasoning"]["risk_flags"])
        self.assertIn("archived", candidate["reasoning"]["freshness_reason"].lower())

    # --- forks -----------------------------------------------------------

    def test_fork_is_secondary_unknown_possible_mirror(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.install_transport(lambda *_: search_payload(repo("someone/tool", fork=True)))
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertTrue(candidate["github"]["is_fork"])
        self.assertEqual("secondary_unknown", candidate["trust_tier"])
        self.assertIn("possible_mirror", candidate["reasoning"]["risk_flags"])

    # --- candidate ranking ----------------------------------------------

    def test_candidates_rank_by_trust_tier_then_scores(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            # A fork (secondary_unknown) is returned first by the provider, but a
            # well-licensed non-fork must outrank it; tier beats provider order.
            self.install_transport(
                lambda *_: search_payload(
                    repo("someone/agent-fork", description="agent", fork=True, license_key=None),
                    repo("acme/agent", description="agent framework", license_key="Apache-2.0", stars=900),
                )
            )
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "agent"]
            )
        self.assertEqual(0, code)
        candidates = json.loads(stdout)["candidates"]
        self.assertEqual("acme/agent", candidates[0]["github"]["full_name"])
        self.assertEqual("primary_non_official", candidates[0]["trust_tier"])
        self.assertEqual("secondary_unknown", candidates[1]["trust_tier"])

    def test_exact_owner_repo_match_outranks_fuzzy_within_tier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.install_transport(
                lambda *_: search_payload(
                    repo("other/loosely-related-llm", description="llm tools"),
                    repo("acme/llm", description="the llm"),
                )
            )
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "acme/llm"]
            )
        self.assertEqual(0, code)
        candidates = json.loads(stdout)["candidates"]
        winner = candidates[0]
        self.assertEqual("acme/llm", winner["github"]["full_name"])
        self.assertGreaterEqual(winner["relevance_score"], 0.95)
        self.assertIn("acme/llm", winner["reasoning"]["matched_query_terms"])

    def test_max_results_caps_emitted_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            many = [repo(f"acme/tool-{index}", description="tool") for index in range(5)]
            self.install_transport(lambda *_: search_payload(*many))
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github",
                 "--query", "tool", "--max-results", "2"]
            )
        self.assertEqual(0, code)
        self.assertEqual(2, json.loads(stdout)["count"])

    # --- rate limit / auth ----------------------------------------------

    def test_rate_limit_returns_envelope_with_token_guidance(self):
        for status in (403, 429):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace = self.write_workspace(Path(tmpdir))

                    def transport(_url, _timeout, _headers, status=status):
                        raise HTTPError(
                            url="https://api.github.com/search/repositories?q=tool",
                            code=status, msg="rate limited", hdrs=None, fp=None,
                        )

                    self.install_transport(transport)
                    code, stdout, stderr = self.run_cli(
                        ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
                    )
                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                envelope = json.loads(stderr)
                self.assertEqual("GITHUB_RATE_LIMITED", envelope["error_code"])
                self.assertIn("GITHUB_TOKEN", envelope["remediation"])
                self.assertTrue(envelope["details"]["network_io_executed"])

    def test_auth_failure_returns_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))

            def transport(_url, _timeout, _headers):
                raise HTTPError(url="https://api.github.com/search/repositories", code=401,
                                msg="bad creds", hdrs=None, fp=None)

            self.install_transport(transport)
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
        self.assertEqual(2, code)
        envelope = json.loads(stderr)
        self.assertEqual("GITHUB_AUTH_REQUIRED", envelope["error_code"])
        self.assertIn("GITHUB_TOKEN", envelope["remediation"])

    def test_network_error_is_retried_then_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            attempts = []

            def transport(_url, _timeout, _headers):
                attempts.append(1)
                raise URLError("connection refused")

            self.install_transport(transport)
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
        self.assertEqual(2, code)
        self.assertEqual(DISCOVER.GITHUB_MAX_ATTEMPTS, len(attempts))
        self.assertEqual("DISCOVERY_NETWORK_ERROR", json.loads(stderr)["error_code"])

    def test_missing_items_list_is_response_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.install_transport(lambda *_: json.dumps({"total_count": 0}).encode("utf-8"))
            code, _, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_RESPONSE_INVALID", json.loads(stderr)["error_code"])

    # --- token handling --------------------------------------------------

    def test_token_used_flag_and_authorization_header_without_leaking_token(self):
        secret = "ghp_supersecrettokenvalue1234567890"
        os.environ["GITHUB_TOKEN"] = secret
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            seen_headers = {}

            def transport(_url, _timeout, headers):
                seen_headers.update(headers)
                return search_payload(repo("acme/tool"))

            self.install_transport(transport)
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
            store_text = (workspace / "sources" / "discovery" / "candidates.jsonl").read_text(encoding="utf-8")

        self.assertEqual(0, code)
        # The token authenticates the request...
        self.assertEqual(f"Bearer {secret}", seen_headers["Authorization"])
        report = json.loads(stdout)
        self.assertTrue(report["token_used"])
        # ...but the secret value never appears in stdout, stderr, or the store.
        self.assertNotIn(secret, stdout)
        self.assertNotIn(secret, stderr)
        self.assertNotIn(secret, store_text)

    def test_unauthenticated_discovery_sends_no_authorization_header(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            seen_headers = {}

            def transport(_url, _timeout, headers):
                seen_headers.update(headers)
                return search_payload(repo("acme/tool"))

            self.install_transport(transport)
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
        self.assertEqual(0, code)
        self.assertNotIn("Authorization", seen_headers)
        self.assertFalse(json.loads(stdout)["token_used"])

    # --- request linkage + idempotency ----------------------------------

    def test_request_id_links_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.install_transport(lambda *_: search_payload(repo("acme/tool")))
            code, stdout, _ = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github",
                 "--query", "tool", "--request-id", "req-1a2b3c4d5e"]
            )
        self.assertEqual(0, code)
        candidate = json.loads(stdout)["candidates"][0]
        self.assertEqual("req-1a2b3c4d5e", candidate["request_id"])
        self.assertIsNone(candidate["seed_source_id"])
        self.assertIsNone(candidate["discovery_run_id"])

    def test_blank_optional_request_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.install_transport(lambda *_: search_payload(repo("acme/tool")))
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github",
                 "--query", "tool", "--request-id", "   "]
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("VALUE_INVALID", envelope["error_code"])

    def test_rerunning_same_query_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.install_transport(lambda *_: search_payload(repo("acme/tool"), repo("acme/other")))

            argv = ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            first_code, first_out, _ = self.run_cli(argv)
            self.install_transport(lambda *_: search_payload(repo("acme/tool"), repo("acme/other")))
            second_code, second_out, _ = self.run_cli(argv)
            stored_count = len(self.store_records(workspace))

        self.assertEqual(0, first_code)
        self.assertEqual(0, second_code)
        self.assertEqual(2, json.loads(first_out)["written"])
        # The second run proposes the same candidates but writes nothing new.
        self.assertEqual(2, json.loads(second_out)["count"])
        self.assertEqual(0, json.loads(second_out)["written"])
        self.assertEqual(2, stored_count)

    # --- disabled gate does not touch the network -----------------------

    def test_disabled_discovery_never_calls_transport(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), enabled=False)
            called = []
            self.install_transport(lambda *args: called.append(args) or b"{}")
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github", "--query", "tool"]
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual([], called, "disabled discovery must not call the GitHub transport")
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])


if __name__ == "__main__":
    unittest.main()
