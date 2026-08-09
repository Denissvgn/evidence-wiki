"""Registered discovery providers: `discover_sources.py registered search` (CR-5 T7, T3, T6).

Why each group of cases exists, in one sentence each:

- **Registration widens availability, never authorization.** An id that no
  built-in and no installed distribution supplies must refuse with
  ``PROVIDER_NOT_REGISTERED`` instead of the generic unknown-provider crash, and
  an id that *is* installed but is not written into `research.yml` must still be
  refused with the byte-identical ``DISCOVERY_PROVIDER_DISABLED`` envelope a
  built-in gets. Installing a plugin must never be able to enable itself.
- **The declaration is the egress boundary.** A planned request outside the
  provider's declared ``allowed_domains`` is refused by the package before any
  DNS or socket work, with ``ACQUISITION_DOMAIN_NOT_DECLARED``.
- **A registered provider buys reach, never trust.** Its candidates re-enter the
  pipeline through the same shape rules, classification, and trust rejection a
  `search` hit does, so a plugin cannot promote its own results; malformed
  results are refused per result through the warnings path rather than taking
  the command down.
- **Discovery stays read-only.** `registered search` proposes candidates. The
  only files it may create are the candidate store, its lock, and the run-scoped
  provider-call ledger that bounds it — nothing under `raw/`, ever.
- **The academic budget survives its migration (T6).** A ledger written by the
  pre-migration writer must keep its spent slots after the accounting moves onto
  the shared module: an upgrade that hands a mid-run workspace a fresh allowance
  would be a budget that resets, which is not a budget.

Nothing here touches the network or DNS. The fixture distribution declares hosts
under the reserved ``.invalid`` TLD, and every transport call is executed by the
real ``_acquisition_transport`` executor with an injected opener and resolver, so
the declaration check, credential custody, and bounded read all run for real
while the socket does not exist.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._provider_plugin_fixture import (  # noqa: E402
    DISCOVERY_PROVIDER_ID,
    installed_provider_plugins,
)


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("registered_discovery_under_test", "discover_sources.py")
ACCOUNTING = load_script_module("registered_discovery_accounting", "_provider_accounting.py")

CREDENTIAL_ENV_VAR = "KEEPA_FIXTURE_API_KEY"
SECRET_VALUE = "keepa-fixture-live-7c1d-never-log-me"
API_HOST = "api.keepa-fixture.invalid"
CATALOG_HOST = "www.keepa-fixture.invalid"

REGISTERED_LEDGER = "registered-provider-requests.jsonl"
ACADEMIC_LEDGER = "academic-provider-requests.jsonl"


def search_payload(*entries: dict) -> bytes:
    return json.dumps({"results": list(entries)}).encode("utf-8")


def product(asin: str, title: str, **extra) -> dict:
    record = {"asin": asin, "title": title}
    record.update(extra)
    return record


class FakeResponse:
    """The minimum an ``http.client``-shaped response has to be for bounded_download."""

    def __init__(self, body: bytes, *, url: str, status: int = 200, content_type: str = "application/json") -> None:
        self.body = body
        self.url = url
        self.status = status
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self.body)
        chunk = self.body[:size]
        self.body = self.body[size:]
        return chunk

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status


@dataclass
class RecordingOpener:
    """Transport seam: records the request the package actually built, returns bytes."""

    bodies: list[bytes] = field(default_factory=list)
    status: int = 200
    content_type: str = "application/json"
    requests: list[object] = field(default_factory=list)

    def __call__(self, request, timeout):
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.bodies) - 1)
        return FakeResponse(
            self.bodies[index] if self.bodies else b"{}",
            url=request.full_url,
            status=self.status,
            content_type=self.content_type,
        )


@dataclass
class RecordingResolver:
    """DNS seam that records every call, so a refusal can be proved pre-resolution."""

    calls: list = field(default_factory=list)

    def __call__(self, host, port=None):
        self.calls.append((host, port))
        return [(2, 1, 6, "", ("93.184.216.34", 0))]


@dataclass(frozen=True)
class PlannedRequest:
    """Duck-typed stand-in for the plan shape a provider returns.

    Structural, like everything else in this contract: the executor reads
    attributes, so a misbehaving provider is simulated with a plain object rather
    than by reaching into the fixture distribution's own types.
    """

    url: str
    method: str = "GET"
    headers: tuple = ()
    body: bytes | None = None
    timeout_hint: float | None = None


class MisbehavingProvider:
    """A registered provider whose named methods are replaced with bad behaviour.

    Every unpatched call delegates to the real fixture provider, so a test changes
    exactly one thing. The point is to exercise this script's own guards against
    provider output — patching the guard itself would assert nothing.
    """

    def __init__(self, base, **overrides):
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


class RegisteredDiscoveryTestBase(unittest.TestCase):
    """Workspace, transport seams, and CLI plumbing shared by every case below."""

    def setUp(self):
        self.opener = RecordingOpener(bodies=[search_payload()])
        self.resolver = RecordingResolver()
        DISCOVER.REGISTERED_OPENER = self.opener
        DISCOVER.REGISTERED_RESOLVER = self.resolver
        self.addCleanup(self._reset_seams)
        self._restore_env()

    def _reset_seams(self):
        DISCOVER.REGISTERED_OPENER = None
        DISCOVER.REGISTERED_RESOLVER = None

    def _restore_env(self):
        previous = os.environ.get(CREDENTIAL_ENV_VAR)
        os.environ[CREDENTIAL_ENV_VAR] = SECRET_VALUE

        def restore():
            if previous is None:
                os.environ.pop(CREDENTIAL_ENV_VAR, None)
            else:
                os.environ[CREDENTIAL_ENV_VAR] = previous

        self.addCleanup(restore)

    def workspace(self, root: Path, *, providers=(DISCOVERY_PROVIDER_ID,), enabled: bool = True) -> Path:
        space = root / "workspace"
        (space / "sources" / "discovery").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: registered-discovery-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "  source_requests_path: sources/source-requests.jsonl",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
            "    providers:",
        ]
        lines.extend(f"      - {provider}" for provider in providers)
        (space / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return space

    def request_file(self, workspace: Path, document, name: str = "request.json") -> str:
        path = workspace / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def start_run(self, workspace: Path, run_id: str = "run-registered") -> str:
        """A run-controller run, with only the artifacts discovery reads."""
        run_dir = workspace / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "started_at": "2026-08-09T00:00:00Z",
                    "state": {"current": "discovering"},
                    "_pending_event": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return run_id

    def run_cli(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "registered", "search", *args]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def run_registered(self, workspace: Path, document, *extra: str) -> tuple[int, str, str]:
        return self.run_cli(
            workspace,
            "--id",
            DISCOVERY_PROVIDER_ID,
            "--request-file",
            self.request_file(workspace, document),
            *extra,
        )

    def store_records(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "discovery" / "candidates.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def tree(self, root: Path) -> set[str]:
        return {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() or path.is_dir()
        }

    def misbehave(self, **overrides) -> None:
        """Swap the installed provider's named methods for the duration of one test.

        The registration itself — id, distribution, declared capabilities — is the
        real one, so the declaration still bounds egress while the provider's
        behaviour is the thing under test.
        """
        original = DISCOVER.require_registered_discovery_provider

        def wrapped(provider_id: str):
            registration = original(provider_id)
            return dataclasses.replace(
                registration,
                provider=MisbehavingProvider(registration.provider, **overrides),
            )

        DISCOVER.require_registered_discovery_provider = wrapped
        self.addCleanup(setattr, DISCOVER, "require_registered_discovery_provider", original)


class RegisteredSearchEndToEndTests(RegisteredDiscoveryTestBase):
    """The fixture discovery provider, end to end into the candidate store."""

    def test_fixture_provider_search_writes_classified_candidates_to_the_store(self):
        """The whole path: plan, package-executed transport, interpret, hygiene, append."""
        self.opener.bodies = [
            search_payload(
                product("B0ABC12345", "Retrieval Benchmark Kit", snippet="A product listing"),
                product("B0DEF67890", "Vector Index Appliance"),
            )
        ]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("registered", report["command"])
        self.assertEqual("search", report["registered_command"])
        self.assertEqual(DISCOVERY_PROVIDER_ID, report["provider"])
        self.assertEqual(1, report["planned_request_count"])
        self.assertEqual(2, report["count"])
        self.assertEqual(2, report["written"])
        self.assertEqual(2, len(stored))

        # The package executed the plan, not the plugin: exactly one request, to
        # the declared API host, carrying the resolved credential the plugin only
        # ever named.
        self.assertEqual(1, len(self.opener.requests))
        request = self.opener.requests[0]
        self.assertTrue(request.full_url.startswith(f"https://{API_HOST}/search?"))
        self.assertEqual(SECRET_VALUE, request.get_header("X-keepa-fixture-key"))

        for candidate in stored:
            with self.subTest(candidate=candidate["candidate_id"]):
                self.assertEqual(DISCOVERY_PROVIDER_ID, candidate["provider"])
                self.assertTrue(candidate["url"].startswith(f"https://{CATALOG_HOST}/product/"))
                self.assertTrue(candidate["network_io_executed"])
                # Candidate lifecycle fields the store's schema requires.
                self.assertEqual("proposed", candidate["lifecycle_state"])
                self.assertIn(candidate["recommended_action"], ("fetch", "review", "reject"))

    def test_a_candidate_records_the_registration_that_proposed_it(self):
        """Auditability before acquisition: which installed code proposed this, under what limits."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        block = stored[0]["provider_registration"]
        self.assertEqual(DISCOVERY_PROVIDER_ID, block["id"])
        self.assertEqual("discovery", block["phase"])
        self.assertEqual("keepa-fixture", block["distribution"])
        self.assertEqual(1, block["provider_api_version"])
        self.assertEqual(block, report["provider_registration"])

        capabilities = stored[0]["provider_capabilities"]
        self.assertEqual([API_HOST], capabilities["allowed_domains"])
        self.assertEqual({"requests": 30, "per": "minute"}, capabilities["rate_limit"])
        # Credential *names* only. A value must never reach a candidate record.
        self.assertEqual([CREDENTIAL_ENV_VAR], capabilities["credentials"])
        self.assertNotIn(SECRET_VALUE, json.dumps(stored))
        self.assertNotIn(SECRET_VALUE, stdout)

    def test_registered_candidates_get_no_trust_privilege_for_being_registered(self):
        """A plugin's own trust hint is a hint; official_primary is never self-granted."""
        self.opener.bodies = [
            search_payload(
                {
                    "asin": "B0ABC12345",
                    "title": "Vendor Catalogue Listing",
                    "trust_tier": "official_primary",
                    "official": True,
                }
            )
        ]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            code, _stdout, stderr = self.run_registered(workspace, {"query": "vendor catalogue"})
            stored = self.store_records(workspace)

        self.assertEqual(0, code, stderr)
        # The fixture provider pins its own hints (official=False,
        # primary_non_official) before this script ever sees the raw entry, and
        # the classifier then derives the tier from policy signals rather than
        # from any hint. Either way the vendor host is not official_primary.
        self.assertEqual(1, len(stored))
        self.assertNotEqual("official_primary", stored[0]["trust_tier"])
        self.assertIsNot(True, stored[0]["official_source"])
        self.assertIn("recommended_action", stored[0])

    def test_reruns_are_idempotent_through_the_shared_candidate_writer(self):
        """`registered search` is the eighth caller of append_candidates, not a new writer."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            first_code, first_stdout, first_stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            second_code, second_stdout, second_stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(0, first_code, first_stderr)
        self.assertEqual(0, second_code, second_stderr)
        self.assertEqual(1, json.loads(first_stdout)["written"])
        self.assertEqual(0, json.loads(second_stdout)["written"])
        self.assertEqual(1, len(stored))


class RegisteredSearchCandidateHygieneTests(RegisteredDiscoveryTestBase):
    """Untrusted plugin output must not reach the store unvalidated."""

    def test_malformed_results_are_refused_per_result_through_the_warnings_path(self):
        """One unusable result must not lose the usable ones, or take the command down."""
        # The fixture provider drops entries without a valid ASIN itself; these
        # two get past it and are refused by this script's own hygiene instead:
        # a non-object entry, and an object whose URL is unusable.
        self.opener.bodies = [
            search_payload(
                product("B0ABC12345", "Retrieval Benchmark Kit"),
                product("B0DEF67890", "Vector Index Appliance"),
            )
        ]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            self.misbehave(
                interpret_candidates=lambda document, responses: (
                    {"url": f"https://{CATALOG_HOST}/product/B0ABC12345", "title": "Retrieval Benchmark Kit"},
                    {"url": f"https://{CATALOG_HOST}/product/B0DEF67890", "title": "Vector Index Appliance"},
                    "not-an-object",
                    {"title": "No URL At All"},
                    {"url": "ftp://x.invalid/f", "title": "Wrong Scheme"},
                )
            )
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual(2, report["count"], "the two well-formed candidates must survive")
        self.assertEqual(2, len(stored))
        codes = {warning["code"] for warning in report["warnings"]}
        self.assertIn("registered_candidate_refused", codes)
        stored_urls = {record["url"] for record in stored}
        self.assertNotIn("ftp://x.invalid/f", stored_urls)

    def test_an_unmodelled_source_type_is_demoted_to_web_page_and_said_out_loud(self):
        """The deliberate fallback: policy defaults are source_type-keyed, so a silent
        demotion would hand a candidate web_page review policy with no trace of why."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            self.misbehave(
                interpret_candidates=lambda document, responses: (
                    {
                        "url": f"https://{CATALOG_HOST}/product/B0ABC12345",
                        "title": "Retrieval Benchmark Kit",
                        "source_type": "market_listing",
                    },
                )
            )
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        warnings = {warning["code"]: warning["message"] for warning in report["warnings"]}
        self.assertIn("registered_source_type_unmodelled", warnings)
        self.assertIn("market_listing", warnings["registered_source_type_unmodelled"])
        self.assertEqual("web_page", stored[0]["source_type"])
        # The recorded policy matches the recorded type; nothing is left mismatched.
        self.assertEqual(
            DISCOVER.CANDIDATE_POLICY_DEFAULTS["web_page"]["source_policy"],
            stored[0]["source_policy"],
        )

    def test_a_provider_returning_a_non_sequence_refuses_the_command_without_writing(self):
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        # One seam, re-aimed per case: tearing the seams down inside the loop
        # would let a later iteration fall through to real DNS.
        returned: list = ["candidates"]
        self.misbehave(interpret_candidates=lambda document, responses: returned[0])
        with installed_provider_plugins():
            for label, produced in (("string", "candidates"), ("mapping", {"results": []}), ("none", None)):
                with self.subTest(returned=label), tempfile.TemporaryDirectory() as tmpdir:
                    returned[0] = produced
                    workspace = self.workspace(Path(tmpdir))
                    code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
                    stored = self.store_records(workspace)

                    self.assertEqual(2, code)
                    self.assertEqual("", stdout)
                    self.assertEqual("PROVIDER_PLAN_INVALID", json.loads(stderr)["error_code"])
                    self.assertEqual([], stored)

    def test_a_crashing_provider_becomes_a_refusal_not_a_traceback(self):
        """A plugin bug must fail this command, never take the interpreter down."""
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))

            def explode(document):
                raise RuntimeError("plugin exploded while planning")

            self.misbehave(plan_search=explode)
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
        self.assertIn("plugin exploded while planning", envelope["message"])
        self.assertEqual(0, len(self.opener.requests))

    def test_a_plugins_own_error_text_cannot_print_a_secret(self):
        """The provider is the one code path that never holds a credential; keep it that way
        even when it echoes something an operator loaded into the request document."""
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))

            def leaky(request):
                raise ValueError(f"cannot search with key {SECRET_VALUE}")

            self.misbehave(validate_request=leaky)
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_REQUEST_INVALID", envelope["error_code"])
        self.assertNotIn(SECRET_VALUE, stderr)
        self.assertIn("[REDACTED]", envelope["message"])
        self.assertIn("[REDACTED]", envelope["details"]["provider_reason"])

    def test_a_plan_over_the_command_cap_is_refused_before_any_transport(self):
        """A plan is not a crawl: the cap is enforced before the first request leaves."""
        over_cap = tuple(
            PlannedRequest(url=f"https://{API_HOST}/search?term=page{index}")
            for index in range(DISCOVER.REGISTERED_MAX_PLANNED_REQUESTS + 1)
        )
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            self.misbehave(plan_search=lambda document: over_cap)
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
        self.assertEqual(DISCOVER.REGISTERED_MAX_PLANNED_REQUESTS, envelope["details"]["limit"])
        self.assertEqual(0, len(self.opener.requests), "no request may leave for an over-cap plan")
        self.assertEqual([], stored)

    def test_one_malformed_request_refuses_the_whole_plan_before_any_of_it_executes(self):
        """Shape-checking every request up front is what keeps a partial plan off the wire."""
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            self.misbehave(
                plan_search=lambda document: (
                    PlannedRequest(url=f"https://{API_HOST}/search?term=ok"),
                    PlannedRequest(url="http://api.keepa-fixture.invalid/search?term=insecure"),
                )
            )
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
        self.assertEqual(2, envelope["details"]["planned_request_index"])
        self.assertEqual(0, len(self.opener.requests), "the valid first request must not have executed")

    def test_a_credential_placeholder_in_a_planned_url_is_refused(self):
        """A secret in a URL would survive URL redaction and leak into origin_url."""
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            self.misbehave(
                plan_search=lambda document: (
                    PlannedRequest(url=f"https://{API_HOST}/search?key={{{{credential:{CREDENTIAL_ENV_VAR}}}}}"),
                )
            )
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("PROVIDER_PLAN_INVALID", json.loads(stderr)["error_code"])
        self.assertNotIn(SECRET_VALUE, stderr)
        self.assertEqual(0, len(self.opener.requests))

    def test_a_request_document_the_provider_refuses_is_reported_in_its_own_words(self):
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            code, stdout, stderr = self.run_registered(workspace, {"query": "", "unknown_field": 1})
            stored = self.store_records(workspace)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_REQUEST_INVALID", envelope["error_code"])
        self.assertIn("unknown_field", envelope["message"])
        self.assertFalse(envelope["details"]["network_io_executed"])
        self.assertEqual([], stored)
        self.assertEqual(0, len(self.opener.requests))

    def test_a_request_file_that_is_not_one_json_object_is_refused(self):
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            for label, payload in (("array", "[1, 2]"), ("scalar", '"query"'), ("garbage", "{not json")):
                with self.subTest(document=label):
                    path = workspace / f"{label}.json"
                    path.write_text(payload, encoding="utf-8")
                    code, stdout, stderr = self.run_cli(
                        workspace, "--id", DISCOVERY_PROVIDER_ID, "--request-file", str(path)
                    )
                    self.assertEqual(2, code)
                    self.assertEqual("", stdout)
                    self.assertEqual("PROVIDER_REQUEST_INVALID", json.loads(stderr)["error_code"])


class RegisteredSearchDeclaredDomainTests(RegisteredDiscoveryTestBase):
    """The declared domain list is the boundary, enforced by the package."""

    def test_a_planned_url_outside_the_declaration_is_refused_before_any_dns_or_socket(self):
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            self.misbehave(
                plan_search=lambda document: (
                    PlannedRequest(url="https://evil.example.invalid/search?term=x"),
                )
            )
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", envelope["error_code"])
        self.assertIn(API_HOST, envelope["message"])
        self.assertFalse(envelope["details"]["network_io_executed"])
        self.assertEqual([], self.resolver.calls, "the refusal must precede name resolution")
        self.assertEqual([], self.opener.requests)
        self.assertEqual([], stored)

    def test_the_declared_host_itself_is_accepted(self):
        """The negative case is only meaningful beside the positive one."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            code, _stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(0, code, stderr)
        self.assertEqual({API_HOST}, {host for host, _port in self.resolver.calls})
        self.assertTrue(self.resolver.calls, "the declared host is validated, not merely trusted")


class RegisteredSearchAuthorizationTests(RegisteredDiscoveryTestBase):
    """Available is not enabled, and enabled is not available (T3)."""

    def test_an_authorized_but_unregistered_id_refuses_with_provider_not_registered(self):
        """`research.yml` may name an id this environment cannot supply; say which."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=("search", DISCOVERY_PROVIDER_ID))
            # No installed_provider_plugins(): the distribution is absent.
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_NOT_REGISTERED", envelope["error_code"])
        self.assertIn(DISCOVERY_PROVIDER_ID, envelope["details"]["provider_ids"])
        self.assertEqual("evidence_wiki.discovery_providers", envelope["details"]["entry_point_group"])
        self.assertFalse(envelope["details"]["network_io_executed"])

    def test_an_unregistered_id_refuses_every_discovery_route_not_only_the_registered_one(self):
        """The allow-list is one authorization boundary, so drift is fatal everywhere."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=("search", "keepa-absent-fixture"))
            argv = [
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "search",
                "--query",
                "retrieval benchmark",
            ]
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = DISCOVER.main(argv)

        self.assertEqual(2, int(code or 0))
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("PROVIDER_NOT_REGISTERED", json.loads(stderr.getvalue())["error_code"])

    def test_installing_a_provider_does_not_authorize_it(self):
        """The registered id is installed but unlisted: today's disabled envelope, unchanged."""
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=("search",))
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            stored = self.store_records(workspace)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("DISCOVERY_PROVIDER_DISABLED", envelope["error_code"])
        self.assertEqual(
            f"Discovery provider '{DISCOVERY_PROVIDER_ID}' is not listed in integrations.discovery.providers.",
            envelope["message"],
        )
        self.assertEqual([], stored)
        self.assertEqual(0, len(self.opener.requests))

    def test_a_built_in_id_is_not_a_registered_provider(self):
        """`registered search --id search` must not borrow the built-in route's authority."""
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=("search",))
            code, stdout, stderr = self.run_cli(
                workspace,
                "--id",
                "search",
                "--request-file",
                self.request_file(workspace, {"query": "retrieval benchmark"}),
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("PROVIDER_NOT_REGISTERED", json.loads(stderr)["error_code"])

    def test_disabled_discovery_refuses_before_the_registration_is_consulted(self):
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), enabled=False)
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])
        self.assertEqual(0, len(self.opener.requests))

    def test_no_plugins_installed_leaves_the_built_in_allow_list_message_byte_identical(self):
        """Hosts parse this sentence; registration must not change it when nothing is registered."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir), providers=("gitlab",))
            code, _stdout, stderr = self.run_registered(workspace, {"query": "x"})

        self.assertEqual(2, code)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_NOT_REGISTERED", envelope["error_code"])
        self.assertIn(
            "has unknown provider(s): gitlab. Allowed providers: "
            + ", ".join(DISCOVER.DISCOVERY_ACCEPTED_IDS),
            envelope["message"],
        )
        self.assertNotIn("Registered providers:", envelope["message"])


class RegisteredSearchReadOnlyTests(RegisteredDiscoveryTestBase):
    """Discovery proposes; it never acquires."""

    def test_a_successful_search_writes_only_the_candidate_store_and_its_lock(self):
        """The read-only invariant, asserted as a filesystem diff rather than a promise."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            request_path = self.request_file(workspace, {"query": "retrieval benchmark"})
            before = self.tree(workspace)
            code, _stdout, stderr = self.run_cli(
                workspace, "--id", DISCOVERY_PROVIDER_ID, "--request-file", request_path
            )
            after = self.tree(workspace)

        self.assertEqual(0, code, stderr)
        created = after - before
        self.assertEqual(
            {
                "sources/discovery/candidates.jsonl",
                "sources/discovery/.locks",
                "sources/discovery/.locks/candidates.lock",
            },
            created,
        )
        # Nothing resembling evidence: no raw tree at all.
        self.assertFalse((Path(tmpdir) / "workspace" / "raw").exists())

    def test_an_accounted_search_writes_only_the_store_and_the_run_scoped_ledger(self):
        """With a run active the ledger is the *only* extra file; it bounds the route, not evidence."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            run_id = self.start_run(workspace)
            request_path = self.request_file(workspace, {"query": "retrieval benchmark"})
            before = self.tree(workspace)
            code, stdout, stderr = self.run_cli(
                workspace, "--id", DISCOVERY_PROVIDER_ID, "--request-file", request_path
            )
            after = self.tree(workspace)

        self.assertEqual(0, code, stderr)
        self.assertEqual(
            {
                "sources/discovery/candidates.jsonl",
                "sources/discovery/.locks",
                "sources/discovery/.locks/candidates.lock",
                f"runs/{run_id}/{REGISTERED_LEDGER}",
                f"runs/{run_id}/.locks",
                f"runs/{run_id}/.locks/registered-provider-requests.lock",
            },
            after - before,
        )
        report = json.loads(stdout)
        self.assertEqual(run_id, report["accounting"]["run_id"])
        self.assertEqual(1, report["accounting"]["reserved"])

    def test_a_refused_search_leaves_the_workspace_byte_identical(self):
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            request_path = self.request_file(workspace, {"query": "  "})
            before = self.tree(workspace)
            code, _stdout, _stderr = self.run_cli(
                workspace, "--id", DISCOVERY_PROVIDER_ID, "--request-file", request_path
            )
            after = self.tree(workspace)

        self.assertEqual(2, code)
        self.assertEqual(before, after)


class RegisteredSearchAccountingTests(RegisteredDiscoveryTestBase):
    """The declared rate limit is a refusal, not a note in a report (T6)."""

    def test_the_plan_is_reserved_in_the_registered_ledger_before_transport(self):
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            run_id = self.start_run(workspace)
            code, _stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            events = ACCOUNTING.load_events(workspace / "runs" / run_id, ledger_filename=REGISTERED_LEDGER)

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, len(events))
        self.assertEqual(DISCOVERY_PROVIDER_ID, events[0].provider_id)
        self.assertEqual("registered_provider_request", events[0].record["event_type"])
        self.assertEqual("registered", events[0].record["command"])

    def test_the_declared_rate_limit_refuses_the_call_past_the_ceiling(self):
        """The fixture declares 30/minute; the 31st within the window must be refused."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            run_id = self.start_run(workspace)
            run_dir = workspace / "runs" / run_id
            for _ in range(30):
                ACCOUNTING.reserve(
                    run_dir,
                    DISCOVERY_PROVIDER_ID,
                    1,
                    ledger_filename=REGISTERED_LEDGER,
                    lock_filename="registered-provider-requests.lock",
                    schema_version=DISCOVER.SCHEMA_VERSION,
                    extra_fields={"event_type": "registered_provider_request"},
                )
            requests_before = len(self.opener.requests)
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            events = ACCOUNTING.load_events(run_dir, ledger_filename=REGISTERED_LEDGER)
            stored = self.store_records(workspace)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("ACQUISITION_PROVIDER_RATE_LIMITED", envelope["error_code"])
        self.assertEqual("rate_limit", envelope["details"]["ceiling"])
        self.assertEqual(30, envelope["details"]["limit"])
        self.assertFalse(envelope["details"]["network_io_executed"])
        # Refusal is pre-transport and costs nothing: no request, no record.
        self.assertEqual(requests_before, len(self.opener.requests))
        self.assertEqual(30, len(events))
        self.assertEqual([], stored)

    def test_the_registered_ledger_is_separate_from_the_academic_one(self):
        """The academic reader refuses any provider outside {arxiv, openalex}; one file would break it."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            run_id = self.start_run(workspace)
            run_dir = workspace / "runs" / run_id
            (run_dir / ACADEMIC_LEDGER).write_text("", encoding="utf-8")
            code, _stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})
            academic_bytes = (run_dir / ACADEMIC_LEDGER).read_bytes()

        self.assertEqual(0, code, stderr)
        self.assertEqual(b"", academic_bytes, "a registered reservation must never land in the academic ledger")

    def test_a_search_without_an_active_run_proceeds_unaccounted_and_says_so(self):
        """The academic precedent: with no run there is nowhere durable to account."""
        self.opener.bodies = [search_payload(product("B0ABC12345", "Retrieval Benchmark Kit"))]
        with installed_provider_plugins(), tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            code, stdout, stderr = self.run_registered(workspace, {"query": "retrieval benchmark"})

        self.assertEqual(0, code, stderr)
        accounting = json.loads(stdout)["accounting"]
        self.assertIsNone(accounting["run_id"])
        self.assertEqual(0, accounting["reserved"])


class AcademicLedgerMigrationTests(unittest.TestCase):
    """T6: the migrated academic budget must not restart at zero on upgrade.

    These cases write the ledger the way the *pre-migration* writer did — its
    exact key order, its ``academic-call-<hex>`` id prefix — and then drive the
    migrated ``reserve_academic_provider_request`` against those bytes. A
    workspace part-way through a run keeps every slot it already spent.
    """

    LEGACY_CALL_PREFIX = "academic-call-"

    def workspace(self, root: Path, run_id: str = "run-academic") -> Path:
        space = root / "workspace"
        run_dir = space / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / ACADEMIC_LEDGER).write_text("", encoding="utf-8")
        (run_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "started_at": "2026-08-09T00:00:00Z",
                    "state": {"current": "discovering"},
                    "_pending_event": None,
                    "academic_provider_request_accounting": {
                        "schema_version": "1.0",
                        "ledger_path": f"runs/{run_id}/{ACADEMIC_LEDGER}",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return space

    def legacy_record(self, run_id: str, provider: str, *, attempt: int = 1, index: int = 0) -> str:
        """One line exactly as discover_sources.py wrote it before this migration."""
        return json.dumps(
            {
                "schema_version": "1.0",
                "event_type": "academic_provider_request",
                "call_id": f"{self.LEGACY_CALL_PREFIX}{index:032x}",
                "run_id": run_id,
                "command": "academic",
                "scope_id": "req-paper-1234567890",
                "provider": provider,
                "attempt": attempt,
                "reserved_at": "2026-08-09T11:59:00Z",
                "budget_consumed": True,
            },
            separators=(",", ":"),
        )

    def context(self, workspace: Path, run_id: str, limit: int) -> dict:
        return {
            "project_root": workspace,
            "run_id": run_id,
            "limit": limit,
            "command": "academic",
            "scope_id": "req-paper-1234567890",
            "network_io_executed": False,
        }

    def test_an_existing_pre_migration_ledger_keeps_its_spent_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            run_dir = workspace / "runs" / "run-academic"
            (run_dir / ACADEMIC_LEDGER).write_text(
                "".join(f"{self.legacy_record('run-academic', 'arxiv', index=i)}\n" for i in range(3)),
                encoding="utf-8",
            )
            context = self.context(workspace, "run-academic", limit=3)

            with self.assertRaises(DISCOVER.DiscoverSourcesError) as ctx:
                DISCOVER.reserve_academic_provider_request(context, provider="arxiv", attempt=1)

            lines = (run_dir / ACADEMIC_LEDGER).read_text(encoding="utf-8").splitlines()

        self.assertEqual("ACADEMIC_PROVIDER_REQUEST_BUDGET_EXCEEDED", ctx.exception.error_code)
        self.assertEqual(3, ctx.exception.details["used"])
        self.assertEqual(3, ctx.exception.details["limit"])
        self.assertFalse(ctx.exception.details["network_io_executed"])
        self.assertEqual(3, len(lines), "a refused overage must leave the ledger untouched")

    def test_the_migrated_writer_appends_beside_pre_migration_records(self):
        """Both framings coexist in one file and draw down one budget."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            run_dir = workspace / "runs" / "run-academic"
            (run_dir / ACADEMIC_LEDGER).write_text(
                self.legacy_record("run-academic", "openalex", index=1) + "\n",
                encoding="utf-8",
            )
            context = self.context(workspace, "run-academic", limit=4)
            written = DISCOVER.reserve_academic_provider_request(context, provider="arxiv", attempt=2)
            events = DISCOVER.load_academic_provider_request_events(workspace, "run-academic")

        self.assertEqual(2, len(events))
        self.assertEqual(["openalex", "arxiv"], [event["provider"] for event in events])
        self.assertEqual("academic_provider_request", written["event_type"])
        self.assertEqual("academic", written["command"])
        self.assertEqual("req-paper-1234567890", written["scope_id"])
        self.assertEqual(2, written["attempt"])
        self.assertTrue(written["budget_consumed"])
        # Documented, accepted difference: the id prefix now names the provider.
        self.assertTrue(written["call_id"].startswith("arxiv-call-"))
        # Every field the pre-migration record carried is still carried.
        self.assertEqual(set(events[0]), set(events[1]))

    def test_a_damaged_ledger_still_refuses_with_discoverys_own_code(self):
        """The migration must not leak the shared module's vocabulary to hosts."""
        cases = {
            "unparseable": "{not json\n",
            "foreign_provider": json.dumps(
                {
                    "schema_version": "1.0",
                    "event_type": "academic_provider_request",
                    "call_id": "academic-call-01",
                    "run_id": "run-academic",
                    "provider": "keepa-search-fixture",
                    "reserved_at": "2026-08-09T11:59:00Z",
                }
            )
            + "\n",
            "duplicate_call_id": (
                self.legacy_record("run-academic", "arxiv", index=7)
                + "\n"
                + self.legacy_record("run-academic", "arxiv", index=7)
                + "\n"
            ),
        }
        for label, payload in cases.items():
            with self.subTest(damage=label), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.workspace(Path(tmpdir))
                (workspace / "runs" / "run-academic" / ACADEMIC_LEDGER).write_text(payload, encoding="utf-8")
                context = self.context(workspace, "run-academic", limit=25)

                with self.assertRaises(DISCOVER.DiscoverSourcesError) as ctx:
                    DISCOVER.reserve_academic_provider_request(context, provider="arxiv", attempt=1)

                self.assertEqual("ACADEMIC_PROVIDER_REQUEST_LEDGER_INVALID", ctx.exception.error_code)
                self.assertFalse(ctx.exception.recoverable)

    def test_a_missing_accounting_marker_still_refuses_before_any_reservation(self):
        """The shared module cannot notice a deleted ledger; the run-state marker can."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            state_path = workspace / "runs" / "run-academic" / "run-state.json"
            document = json.loads(state_path.read_text(encoding="utf-8"))
            document.pop("academic_provider_request_accounting")
            state_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
            context = self.context(workspace, "run-academic", limit=25)

            with self.assertRaises(DISCOVER.DiscoverSourcesError) as ctx:
                DISCOVER.reserve_academic_provider_request(context, provider="arxiv", attempt=1)

            ledger = (workspace / "runs" / "run-academic" / ACADEMIC_LEDGER).read_text(encoding="utf-8")

        self.assertEqual("ACADEMIC_PROVIDER_ACCOUNTING_UNINITIALIZED", ctx.exception.error_code)
        self.assertEqual("", ledger)

    def test_the_budget_error_still_reports_whether_network_io_already_happened(self):
        """A retry that already spent a request must not claim it made no network call."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(Path(tmpdir))
            (workspace / "runs" / "run-academic" / ACADEMIC_LEDGER).write_text(
                self.legacy_record("run-academic", "openalex", index=2) + "\n",
                encoding="utf-8",
            )
            context = self.context(workspace, "run-academic", limit=1)
            context["network_io_executed"] = True

            with self.assertRaises(DISCOVER.DiscoverSourcesError) as ctx:
                DISCOVER.reserve_academic_provider_request(context, provider="openalex", attempt=2)

        self.assertEqual("ACADEMIC_PROVIDER_REQUEST_BUDGET_EXCEEDED", ctx.exception.error_code)
        self.assertTrue(ctx.exception.details["network_io_executed"])


if __name__ == "__main__":
    unittest.main()
