"""CR-5 end to end: a pip-installed provider becomes first-class evidence, or is refused.

Every other CR-5 suite tests one leg against a hand-built workspace. This file is the
acceptance gate: one named test per acceptance criterion in the change request, each run
against a **real** workspace that `init_research_workspace.py` produced, driving the real
commands an operator or host would drive.

What the change request asks for, and what each test therefore has to show:

1. A pip-installed provider that `research.yml` authorizes by id is *usable* — and usable
   means the artifact it delivers is ordinary evidence, so the chain continues past
   `registered get` into `source_inventory.py` and `normalize_sources.py`. Stopping at
   "a file appeared" would prove a download, not an integration.
2. Without the `research.yml` entry the same command is refused exactly as today, with an
   envelope indistinguishable from a disabled **built-in** provider's. Installing a
   distribution must never be able to enable itself.
3. A plan that targets a host outside the provider's declared `allowed_domains` is blocked
   *by the package*, before any socket or DNS work — and leaves nothing behind. The
   leftover check compares a full recursive snapshot of the workspace tree, because a
   half-written artifact, an orphan sidecar, or a stale `.acquisition-incomplete` marker
   are each a different way for a refusal to become durable state.
4. The sidecar carries the declared capability summary, credential **names** only, even
   when the credential's value is in this process's environment.
5. With no entry points installed the built-in lists are the universe, byte for byte. The
   validation sentences hosts parse are pinned as literal strings.
6. `doctor` lists registered providers with their declared capabilities, and separates
   *available* (installed here) from *enabled* (authorized in `research.yml`).
7. An authorization the environment cannot satisfy is loud: smoke fails, and the
   orchestration controller refuses to start a session over that workspace.
8. Registered **discovery** proposes candidates through the same hygiene every other
   candidate goes through, and writes nothing outside the candidate store.

No test here touches the network or DNS. The fixture distribution declares hosts under the
reserved `.invalid` TLD, and transport is stubbed at the seam the implementation exposes
(`execute_planned_request` for the acquisition flow, `REGISTERED_OPENER`/
`REGISTERED_RESOLVER` when the package's own domain check is the subject). A test that
escaped both stubs would fail on resolution rather than reach a live service.

Two hazards this file works around deliberately. Registration lookups are cached per
process and `fetch_sources.py` loads the loader through its own module loader, so its copy
of the cache is cleared alongside the shared one. And criterion 5 asserts the *absence* of
registration state, which a sibling test could have armed process-globally, so it runs in
a subprocess with an environment this file controls.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
PROFILE_FIXTURE_PATH = FIXTURES / "workspace-init-profile.yml"
STUB_ADAPTER = FIXTURES / "normalizer-adapter" / "stub_adapter.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._provider_plugin_fixture import (  # noqa: E402
    ACQUISITION_PROVIDER_ID,
    DISCOVERY_PROVIDER_ID,
    FIXTURE_ROOT,
    installed_provider_plugins,
    refresh_provider_plugin_caches,
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


# Names are prefixed per file: these scripts are reachable through more than one loader,
# and two copies under one name would share -- or clobber -- each other's module state.
INIT = load_script_module("cr5_e2e_init", "init_research_workspace.py")
FETCH = load_script_module("cr5_e2e_fetch_sources", "fetch_sources.py")
DISCOVER = load_script_module("cr5_e2e_discover_sources", "discover_sources.py")
INVENTORY = load_script_module("cr5_e2e_source_inventory", "source_inventory.py")
NORMALIZE = load_script_module("cr5_e2e_normalize_sources", "normalize_sources.py")
DOCTOR = load_script_module("cr5_e2e_doctor", "doctor.py")
SMOKE = load_script_module("cr5_e2e_smoke", "smoke_validate_workspace.py")
CONTROLLER = load_script_module("cr5_e2e_orchestration_controller", "orchestration_controller.py")

CREDENTIAL_ENV_VAR = "KEEPA_FIXTURE_API_KEY"
#: Never a real key. Distinctive enough that finding it anywhere is unambiguous.
CREDENTIAL_VALUE = "keepa-fixture-live-2f9a41-never-log-me"
API_HOST = "api.keepa-fixture.invalid"
ASSET_HOST = "assets.keepa-fixture.invalid"
CATALOG_HOST = "www.keepa-fixture.invalid"
UNDECLARED_HOST = "exfiltrate.example.invalid"

REQUEST_ASIN = "B0FIXTURE1"
ARTIFACT_FILENAME = "keepa-fixture-b0fixture1.json"
ARTIFACT_RELATIVE_PATH = f"raw/data/{ARTIFACT_FILENAME}"
REQUEST_RELATIVE_PATH = "requests/keepa.json"

PRODUCT_RESPONSE = json.dumps({"title": "Fixture Widget", "currency": "USD"}).encode("utf-8")
HISTORY_RESPONSE = b"date,price\n2026-01-01,19.99\n2026-01-02,18.49\n"
PLANNED_RESPONSES = (PRODUCT_RESPONSE, HISTORY_RESPONSE)
SEARCH_RESPONSE = json.dumps(
    {"results": [{"asin": "B0ABC12345", "title": "Retrieval Benchmark Kit", "snippet": "A vendor listing"}]}
).encode("utf-8")

ADAPTER_NAME = "stub-normalize"
ADAPTER_VERSION = "1.0.0"

#: The two blocks the change request's third criterion names, as exact key sets.
REGISTRATION_BLOCK_KEYS = {"id", "phase", "distribution", "version", "entry_point", "provider_api_version"}
CAPABILITY_BLOCK_KEYS = {
    "allowed_domains",
    "terms_urls",
    "license_inference",
    "captures_raw",
    "quarantine_on_incomplete",
    "rate_limit",
    "credentials",
    "request_kinds",
}

# Pre-CR-5 wording, pinned verbatim: hosts parse these sentences, and the backwards
# compatibility criterion is precisely that registration did not disturb them.
BUILT_IN_ACQUISITION_SENTENCE = (
    "research.yml integrations.acquisition.providers has unknown provider(s): gitlab. "
    "Allowed providers: arxiv, openalex, github, web"
)
BUILT_IN_DISCOVERY_SENTENCE = (
    "research.yml integrations.discovery.providers has unknown provider(s): crossref. "
    "Allowed providers: arxiv, openalex, github, search, standards, standards:iso-open-data, "
    "standards:eu-product-requirements, standards:uk-geospatial-register, standards:nist, "
    "legal, authors, companions"
)


def tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under ``root`` with its bytes; directories map to ``None``.

    Bytes rather than names: a refusal that rewrote a file in place would be invisible to
    a name-only comparison, and "leaves nothing behind" has to mean the tree, not the
    target path.
    """
    entries: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[f"{relative}/"] = None
        else:
            entries[relative] = path.read_bytes()
    return entries


class RecordingTransport:
    """Stands in for ``execute_planned_request``, returning canned payloads in plan order."""

    def __init__(self, payloads=PLANNED_RESPONSES):
        self.payloads = list(payloads)
        self.calls: list[dict[str, object]] = []

    def __call__(self, planned, **kwargs):
        index = len(self.calls)
        self.calls.append({"url": planned.url, "method": planned.method, "headers": tuple(planned.headers)})
        if index >= len(self.payloads):
            raise AssertionError(f"transport called {index + 1} time(s) for {len(self.payloads)} payload(s)")
        payload = self.payloads[index]
        return FETCH.DownloadResult(
            content=payload,
            final_url=planned.url,
            byte_count=len(payload),
            checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            http_status=200,
            content_type="application/json",
            redirect_chain=[],
            tls_verified=True,
        )


class FakeResponse:
    """The minimum an ``http.client``-shaped response has to be for a bounded read."""

    def __init__(self, body: bytes, url: str, content_type: str = "application/json"):
        self.body = body
        self.url = url
        self.status = 200
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
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


class RecordingOpener:
    """Transport seam that records the request the package actually built.

    Recording rather than raising, everywhere: a stub that raises to signal "you must not
    reach me" hands its verdict to whatever ``except`` clause happens to be in the way. A
    recorder makes the assertion the test's, not the implementation's.
    """

    def __init__(self, body: bytes = b"{}"):
        self.body = body
        self.requests: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return FakeResponse(self.body, request.full_url)

    @property
    def urls(self) -> list[str]:
        return [request.full_url for request in self.requests]


class RecordingResolver:
    """DNS seam that records every lookup, so a refusal can be proved pre-resolution."""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, host, port=None):
        self.calls.append((host, port))
        return [(2, 1, 6, "", ("93.184.216.34", 0))]


def public_resolver(host, port=None):
    """A DNS seam that answers with a public address, so the pinning check is exercised."""
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


class RegisteredProviderWorkspace:
    """Workspace scaffolding and command drivers.

    Deliberately not a ``TestCase``: subclassing one that carries tests would re-run the
    whole parent suite under every child class.
    """

    def setUp(self):
        # Registration lookups are cached per process, in two copies (below). A test that
        # asserts something about *no* registrations would otherwise be at the mercy of
        # whichever sibling ran before it.
        super().setUp()
        self.clear_registration_caches()
        self.addCleanup(self.clear_registration_caches)
        # The redaction registry is process-global too, and each script holds its own copy
        # of the transport module. Ask each script for its copy rather than loading one:
        # an independently loaded module would have a different registry.
        self.addCleanup(FETCH._acquisition_transport.reset_registered_secrets)
        self.addCleanup(DISCOVER.TRANSPORT.reset_registered_secrets)

    # -- construction ------------------------------------------------------------

    def init_workspace(self, root: Path) -> Path:
        target = root / "registered provider workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def configure(
        self,
        workspace: Path,
        *,
        acquisition_providers: list[str] | None = None,
        discovery_providers: list[str] | None = None,
        acquisition_enabled: bool = True,
        normalizer_adapter: bool = False,
    ) -> None:
        """What an operator writes by hand: the raw root, the authorized ids, the adapter."""
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["raw"]["source_roots"] = sorted({*config["raw"]["source_roots"], "raw/data"})
        config["integrations"]["acquisition"] = {
            "enabled": acquisition_enabled,
            "providers": list(acquisition_providers or []),
            "target_root": "raw/papers",
            "max_downloads_per_run": 10,
            "require_license_check": True,
        }
        config["integrations"]["discovery"] = {
            "enabled": bool(discovery_providers),
            "providers": list(discovery_providers or []),
            "candidate_store_path": "sources/discovery/candidates.jsonl",
        }
        if normalizer_adapter:
            config["normalization"] = {
                "adapters": [
                    {
                        "kinds": ["structured_data"],
                        "provider": "command",
                        "command": [sys.executable, str(STUB_ADAPTER)],
                        "name": ADAPTER_NAME,
                        "version": ADAPTER_VERSION,
                    }
                ]
            }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def write_request(self, workspace: Path, document: object, relative: str = REQUEST_RELATIVE_PATH) -> Path:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def make_workspace(self, root: Path, **kwargs) -> Path:
        workspace = self.init_workspace(root)
        self.configure(workspace, **kwargs)
        return workspace

    # -- registration state ------------------------------------------------------

    def clear_registration_caches(self) -> None:
        """Drop every copy of the loader's per-process cache.

        There are two. Most scripts import ``_provider_plugins`` by name, so they share
        one module the fixture helper's sweep can find; ``fetch_sources.py`` reaches it
        through its own module loader, which keeps that copy out of ``sys.modules`` on
        purpose -- and only the second call below reaches it.
        """
        refresh_provider_plugin_caches()
        FETCH._provider_plugins.clear_cache()

    @contextlib.contextmanager
    def installed_plugins(self, *variants: str, base: bool = True):
        self.clear_registration_caches()
        try:
            with installed_provider_plugins(*variants, base=base) as handle:
                self.clear_registration_caches()
                yield handle
        finally:
            self.clear_registration_caches()

    @contextlib.contextmanager
    def credential_in_the_environment(self):
        with mock.patch.dict(os.environ, {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
            yield

    # -- drivers -----------------------------------------------------------------

    def run_module(self, module, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def registered_get(self, workspace: Path, *extra: str, provider_id: str = ACQUISITION_PROVIDER_ID):
        return self.run_module(
            FETCH,
            [
                "--project-root", str(workspace),
                "--format", "json",
                "registered", "get",
                "--id", provider_id,
                "--request-file", REQUEST_RELATIVE_PATH,
                *extra,
            ],
        )

    def registered_search(self, workspace: Path, request_path: Path, *extra: str):
        return self.run_module(
            DISCOVER,
            [
                "--project-root", str(workspace),
                "--format", "json",
                "registered", "search",
                "--id", DISCOVERY_PROVIDER_ID,
                "--request-file", str(request_path),
                *extra,
            ],
        )

    def refusal_envelope(self, code: int, stdout: str, stderr: str) -> dict:
        self.assertEqual(2, code, stderr or stdout)
        self.assertEqual("", stdout, "a refusal must leave stdout empty")
        return json.loads(stderr)

    def success_report(self, code: int, stdout: str, stderr: str) -> dict:
        self.assertEqual(0, code, stderr or stdout)
        return json.loads(stdout)

    # -- reading durable state ---------------------------------------------------

    def sidecar_document(self, workspace: Path, relative: str = ARTIFACT_RELATIVE_PATH) -> dict:
        path = workspace / f"{relative}.provenance.yml"
        self.assertTrue(path.is_file(), f"no provenance sidecar at {path}")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def manifest_records(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "manifest.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def candidate_records(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "discovery" / "candidates.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def doctor_check(self, report: dict, check_id: str) -> dict:
        for check in report["checks"]:
            if check["id"] == check_id:
                return check
        raise AssertionError(f"doctor reported no {check_id!r} check")


class PipInstalledProviderIsUsableTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 1: installed + authorized -> usable, and its evidence is first-class."""

    def test_authorized_registered_provider_delivers_evidence_that_inventories_and_normalizes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(
                Path(tmpdir),
                acquisition_providers=[ACQUISITION_PROVIDER_ID],
                normalizer_adapter=True,
            )
            self.write_request(workspace, {"asin": REQUEST_ASIN, "history_days": 30})
            transport = RecordingTransport()

            with self.installed_plugins(), self.credential_in_the_environment(), mock.patch.object(
                FETCH, "execute_planned_request", transport
            ):
                report = self.success_report(*self.registered_get(workspace))

            artifact = workspace / ARTIFACT_RELATIVE_PATH
            with self.subTest("the package wrote the plugin's interpretation, and nothing partial"):
                self.assertTrue(artifact.is_file())
                self.assertFalse(FETCH.acquisition_marker_path(artifact).exists())
                interpreted = json.loads(artifact.read_text(encoding="utf-8"))
                self.assertEqual(REQUEST_ASIN, interpreted["asin"])
                self.assertEqual("Fixture Widget", interpreted["title"])
                self.assertEqual(["2026-01-01,19.99", "2026-01-02,18.49"], interpreted["price_history_rows"])
                self.assertEqual(ARTIFACT_RELATIVE_PATH, report["target_path"])

            document = self.sidecar_document(workspace)
            with self.subTest("the sidecar carries both blocks the change request names"):
                self.assertEqual(ACQUISITION_PROVIDER_ID, document["provider_registration"]["id"])
                self.assertEqual("keepa-fixture", document["provider_registration"]["distribution"])
                self.assertEqual(
                    [API_HOST, ASSET_HOST], document["provider_capabilities"]["allowed_domains"]
                )
                self.assertEqual(
                    f"fetch_sources.py/registered:{ACQUISITION_PROVIDER_ID}", document["retrieved_by"]
                )
                self.assertEqual(len(artifact.read_bytes()), document["byte_count"])

            with self.subTest("the plugin planned; the package fetched, in plan order"):
                self.assertEqual(
                    [f"https://{API_HOST}/product?asin={REQUEST_ASIN}&days=30",
                     f"https://{ASSET_HOST}/product/{REQUEST_ASIN}/history.csv"],
                    [call["url"] for call in transport.calls],
                )

            # The chain continues: acquisition is only an integration if the rest of the
            # workspace treats what it produced as ordinary evidence.
            inventory_code, _stdout, inventory_stderr = self.run_module(
                INVENTORY, ["--project-root", str(workspace), "--report", "--format", "json"]
            )
            self.assertEqual(0, inventory_code, inventory_stderr)
            records = [
                record
                for record in self.manifest_records(workspace)
                if ARTIFACT_RELATIVE_PATH in record.get("raw_paths", [])
            ]
            # Asserted outside the subTests below, which read records[0] and would raise
            # an IndexError rather than a diagnosis if the record were missing.
            self.assertEqual(1, len(records), self.manifest_records(workspace))
            with self.subTest("source_inventory sees it"):
                self.assertEqual("structured_data", records[0]["kind"])

            normalize_code, _stdout, normalize_stderr = self.run_module(
                NORMALIZE, ["--project-root", str(workspace), "--all", "--format", "json"]
            )
            self.assertEqual(0, normalize_code, normalize_stderr)
            normalized = sorted((workspace / "sources" / "normalized").glob("*.md"))
            self.assertEqual(1, len(normalized), normalized)
            with self.subTest("and it normalizes into a quotable record"):
                text = normalized[0].read_text(encoding="utf-8")
                self.assertIn(ADAPTER_NAME, text)
                self.assertIn(records[0]["id"], text)
                # The bytes the provider produced are what a reader can now quote.
                self.assertIn("Fixture Widget", text)


class UnauthorizedRegisteredProviderTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 2: installing a distribution can never enable it."""

    def test_registered_provider_absent_from_research_yml_is_refused_exactly_like_a_built_in(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=["openalex"])
            self.write_request(workspace, {"asin": REQUEST_ASIN})
            before = tree_snapshot(workspace)
            opener = RecordingOpener()

            with self.installed_plugins(), mock.patch.object(FETCH, "REGISTERED_OPENER", opener):
                registered = self.refusal_envelope(*self.registered_get(workspace))
                # The same workspace, the same question, asked of a built-in it also
                # did not authorize.
                built_in = self.refusal_envelope(
                    *self.run_module(
                        FETCH,
                        ["--project-root", str(workspace), "--format", "json", "web", "get",
                         "--url", "https://example.org/whatever"],
                    )
                )

            self.assertEqual("ACQUISITION_PROVIDER_DISABLED", registered["error_code"])
            with self.subTest("the two envelopes differ only in the id they name"):
                normalized = copy.deepcopy(registered)
                normalized["message"] = normalized["message"].replace(f"'{ACQUISITION_PROVIDER_ID}'", "'web'")
                self.assertEqual(built_in, normalized)
            with self.subTest("a refusal about authorization leaves no durable state"):
                self.assertEqual([], opener.urls)
                self.assertEqual(before, tree_snapshot(workspace))

    def test_the_refusal_does_not_leak_whether_the_distribution_is_installed(self):
        """An unauthorized id must answer the same question whether or not it exists here."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=["openalex"])
            self.write_request(workspace, {"asin": REQUEST_ASIN})
            with self.installed_plugins():
                installed = self.refusal_envelope(*self.registered_get(workspace))
            self.clear_registration_caches()
            uninstalled = self.refusal_envelope(*self.registered_get(workspace))

        self.assertEqual("ACQUISITION_PROVIDER_DISABLED", installed["error_code"])
        self.assertEqual(installed, uninstalled)


class OutOfDeclarationFetchTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 3: the declaration is the egress boundary, enforced by the package."""

    def planned_requests(self, keepa_fixture, *urls):
        return tuple(keepa_fixture.PlannedRequest(url=url) for url in urls)

    def test_a_plan_targeting_an_undeclared_host_is_blocked_and_leaves_nothing_behind(self):
        cases = {
            "an unrelated host": f"https://{UNDECLARED_HOST}/product",
            "a declared host used as a suffix": f"https://not-{API_HOST}/product",
        }
        for label, url in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.make_workspace(
                    Path(tmpdir), acquisition_providers=[ACQUISITION_PROVIDER_ID]
                )
                self.write_request(workspace, {"asin": REQUEST_ASIN})
                before = tree_snapshot(workspace)
                opener = RecordingOpener(PRODUCT_RESPONSE)

                with self.installed_plugins(), self.credential_in_the_environment():
                    import keepa_fixture

                    # The declared first request stays in the plan: the refusal must
                    # abandon the whole action, not merely skip the offending leg.
                    plan = self.planned_requests(
                        keepa_fixture, f"https://{API_HOST}/product?asin={REQUEST_ASIN}", url
                    )
                    with mock.patch.object(
                        keepa_fixture.KeepaFixtureAcquisitionProvider,
                        "plan_fetch",
                        lambda self, request, _plan=plan: _plan,
                    ), mock.patch.object(FETCH, "REGISTERED_OPENER", opener):
                        envelope = self.refusal_envelope(*self.registered_get(workspace))

                self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", envelope["error_code"])
                self.assertIn(ACQUISITION_PROVIDER_ID, envelope["message"])
                with self.subTest("blocked by the package: not one request left the process"):
                    self.assertEqual([], opener.urls)
                with self.subTest("no artifact, no sidecar, no incomplete marker, nothing at all"):
                    after = tree_snapshot(workspace)
                    self.assertEqual(
                        [],
                        sorted(set(after) - set(before)),
                        "a blocked fetch created a path in the workspace",
                    )
                    self.assertEqual(before, after, "a blocked fetch changed the workspace tree")

    def test_the_declaration_is_checked_before_any_name_resolution(self):
        """Asked of the executor the script itself holds, with resolution left switched on.

        The command-level test above injects an opener, which turns hostname pinning off by
        construction -- so it can prove no socket opened but not that no name was looked
        up. This asks the same question of ``fetch_sources.py``'s own transport with
        ``resolve_hostnames=True`` and a recording resolver in place of DNS.
        """
        with self.installed_plugins():
            import keepa_fixture

            resolver = RecordingResolver()
            capabilities = keepa_fixture.KeepaFixtureAcquisitionProvider.capabilities
            with self.assertRaises(FETCH.AcquisitionTransportError) as caught:
                FETCH.execute_planned_request(
                    keepa_fixture.PlannedRequest(url=f"https://{UNDECLARED_HOST}/product"),
                    allowed_domains=capabilities.allowed_domains,
                    credentials=capabilities.credentials,
                    env={},
                    timeout=5.0,
                    max_bytes=1024,
                    resolve_hostnames=True,
                    resolver=resolver,
                )

        self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", caught.exception.error_code)
        self.assertEqual([], resolver.calls, "an undeclared host must not even be resolved")

    def test_a_redirect_out_of_the_declaration_is_refused(self):
        """The declaration must bound where a request *lands*, not only where it is aimed.

        A plan can be entirely inside the declaration and still leave it, because the
        server chooses the redirect. That check lives on a different code path from the
        planned-URL one -- the final-URL branch of ``bounded_download``, reached through
        the opener rather than through ``enforce_declared_domain`` -- and carries its own
        error code, so it needs its own test. Without this, dropping the final-URL branch
        or passing ``allowed_domains=None`` into the download would let a registered
        provider be redirected anywhere and break nothing.
        """

        class RedirectingOpener(RecordingOpener):
            def __call__(self, request, timeout=None):
                self.requests.append(request)
                return FakeResponse(self.body, f"https://{UNDECLARED_HOST}/product")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=[ACQUISITION_PROVIDER_ID])
            self.write_request(workspace, {"asin": REQUEST_ASIN})
            opener = RedirectingOpener(PRODUCT_RESPONSE)
            before = tree_snapshot(workspace)

            with self.installed_plugins(), self.credential_in_the_environment():
                with mock.patch.object(FETCH, "REGISTERED_OPENER", opener):
                    code, stdout, stderr = self.registered_get(workspace)

            self.assertEqual(2, code, stdout or stderr)
            self.assertEqual("", stdout, "a refusal must leave stdout empty")
            envelope = json.loads(stderr)
            self.assertEqual("ACQUISITION_REDIRECT_UNSAFE", envelope["error_code"])
            # The request was aimed inside the declaration, so it was correctly attempted.
            self.assertTrue(opener.urls, "the in-declaration request should have been sent")
            self.assertNotIn(UNDECLARED_HOST, opener.urls[0])
            self.assertEqual(before, tree_snapshot(workspace), "a blocked redirect wrote to the workspace")

    def test_the_same_plan_inside_the_declaration_is_executed(self):
        """The control: without it, an implementation that refused everything would pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=[ACQUISITION_PROVIDER_ID])
            self.write_request(workspace, {"asin": REQUEST_ASIN})
            opener = RecordingOpener(PRODUCT_RESPONSE)

            with self.installed_plugins(), self.credential_in_the_environment():
                import keepa_fixture

                # A subdomain of a declared host is inside the declaration, by the same
                # matching rule the built-in providers use.
                plan = self.planned_requests(keepa_fixture, f"https://eu.{API_HOST}/product")
                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider,
                    "plan_fetch",
                    lambda self, request, _plan=plan: _plan,
                ), mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider,
                    "interpret",
                    lambda self, request, responses: keepa_fixture.SourceArtifact(
                        filename="in-declaration.json", source_type="dataset", content=b'{"ok": true}'
                    ),
                ), mock.patch.object(FETCH, "REGISTERED_OPENER", opener):
                    code, stdout, stderr = self.registered_get(workspace)

            self.success_report(code, stdout, stderr)
            self.assertEqual([f"https://eu.{API_HOST}/product"], opener.urls)
            self.assertTrue((workspace / "raw/data" / "in-declaration.json").is_file())


class SidecarCapabilitySummaryTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 4: the declaration is recorded, credential names only."""

    def test_the_sidecar_records_the_exact_declared_summary_and_never_a_credential_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=[ACQUISITION_PROVIDER_ID])
            self.write_request(workspace, {"asin": REQUEST_ASIN})

            with self.installed_plugins(), self.credential_in_the_environment(), mock.patch.object(
                FETCH, "execute_planned_request", RecordingTransport()
            ):
                report = self.success_report(*self.registered_get(workspace))

            document = self.sidecar_document(workspace)
            registration = document["provider_registration"]
            capabilities = document["provider_capabilities"]

            with self.subTest("the registration block is exactly the packaging identity"):
                self.assertEqual(REGISTRATION_BLOCK_KEYS, set(registration))
                self.assertEqual(
                    {
                        "id": ACQUISITION_PROVIDER_ID,
                        "phase": "acquisition",
                        "distribution": "keepa-fixture",
                        "version": "0.1.0",
                        "entry_point": ACQUISITION_PROVIDER_ID,
                        "provider_api_version": 1,
                    },
                    registration,
                )

            with self.subTest("the capability block is exactly the declaration"):
                self.assertEqual(CAPABILITY_BLOCK_KEYS, set(capabilities))
                self.assertEqual([API_HOST, ASSET_HOST], capabilities["allowed_domains"])
                self.assertEqual({"requests": 60, "per": "minute"}, capabilities["rate_limit"])
                self.assertEqual("partial", capabilities["license_inference"])
                self.assertEqual(["market-data/price_history"], capabilities["request_kinds"])
                self.assertTrue(capabilities["captures_raw"])
                self.assertTrue(capabilities["quarantine_on_incomplete"])

            with self.subTest("credential names are recorded; the value in the environment is not"):
                self.assertEqual([CREDENTIAL_ENV_VAR], capabilities["credentials"])
                sidecar_text = (workspace / f"{ARTIFACT_RELATIVE_PATH}.provenance.yml").read_text(
                    encoding="utf-8"
                )
                self.assertIn(CREDENTIAL_ENV_VAR, sidecar_text)
                self.assertNotIn(CREDENTIAL_VALUE, sidecar_text)
                self.assertNotIn(CREDENTIAL_VALUE, json.dumps(report))
                # Not just the sidecar: nothing the command wrote anywhere may carry it.
                for path, payload in tree_snapshot(workspace).items():
                    if payload is not None:
                        self.assertNotIn(CREDENTIAL_VALUE.encode("utf-8"), payload, path)

            with self.subTest("plugin-supplied metadata stays nested, never merged into the root"):
                self.assertEqual(REQUEST_ASIN, document["provider_metadata"]["asin"])
                self.assertEqual(API_HOST, document["provider_metadata"]["api_host"])


class NoEntryPointsInstalledTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 5: with nothing installed, the built-in lists are the whole universe.

    Run in a subprocess on purpose. This is the one criterion that asserts an *absence* of
    registration state, and registration lookups are cached process-globally: a sibling
    test that installed the fixture distribution and left a warm cache behind would make
    an in-process version of this test pass or fail for reasons that have nothing to do
    with the code under test.
    """

    def run_in_subprocess(self, script: str, argv: list[str], *, plugins_installed: bool) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        if plugins_installed:
            environment["PYTHONPATH"] = str(FIXTURE_ROOT)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *argv],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    def smoke_messages(self, workspace: Path, *, plugins_installed: bool) -> list[str]:
        result = self.run_in_subprocess(
            "smoke_validate_workspace.py",
            ["--project-root", str(workspace), "--format", "json"],
            plugins_installed=plugins_installed,
        )
        report = json.loads(result.stdout or result.stderr)
        return [issue["message"] for issue in report.get("issues", [])]

    def test_built_in_only_validation_messages_are_byte_identical_to_their_pre_cr5_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(
                Path(tmpdir),
                acquisition_providers=["gitlab"],
                discovery_providers=["crossref"],
            )
            messages = self.smoke_messages(workspace, plugins_installed=False)

        self.assertIn(BUILT_IN_ACQUISITION_SENTENCE, messages)
        self.assertIn(BUILT_IN_DISCOVERY_SENTENCE, messages)
        self.assertEqual(
            [],
            [message for message in messages if "Registered providers" in message],
            "with nothing installed, no message may mention registration at all",
        )

    def test_installing_a_distribution_is_what_changes_those_messages(self):
        """The control for the test above: the environment difference must be observable.

        Without this, "byte-identical with nothing installed" would also pass if the
        subprocess never saw the fixture distribution in either arm.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(
                Path(tmpdir),
                acquisition_providers=["gitlab"],
                discovery_providers=["crossref"],
            )
            uninstalled = self.smoke_messages(workspace, plugins_installed=False)
            installed = self.smoke_messages(workspace, plugins_installed=True)

        self.assertNotEqual(uninstalled, installed)
        self.assertTrue(
            any(f"Registered providers: {ACQUISITION_PROVIDER_ID}" in message for message in installed),
            installed,
        )


class DoctorListsRegisteredProvidersTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 6: an auditor can see what this workspace could reach, and what it enabled."""

    def test_doctor_names_every_registration_and_separates_available_from_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Both fixture providers are installed; only the acquisition one is authorized,
            # which is precisely the available-vs-enabled distinction the CR asks for.
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=[ACQUISITION_PROVIDER_ID])
            with self.installed_plugins():
                report = self.success_report(
                    *self.run_module(DOCTOR, ["--project-root", str(workspace), "--format", "json"])
                )

        check = self.doctor_check(report, "registered_providers")
        details = check["details"]
        self.assertEqual({"registered": 2, "enabled": 1, "available": 1, "invalid": 0}, details["counts"])

        by_id = {entry["id"]: entry for entry in details["registered"]}
        with self.subTest("the authorized provider is enabled"):
            acquisition = by_id[ACQUISITION_PROVIDER_ID]
            self.assertEqual("enabled", acquisition["state"])
            self.assertTrue(acquisition["authorized"])
            self.assertEqual("keepa-fixture", acquisition["distribution"])
            self.assertEqual("0.1.0", acquisition["version"])
            self.assertEqual("evidence_wiki.acquisition_providers", acquisition["entry_point_group"])
        with self.subTest("the installed-but-unauthorized provider is available, not enabled"):
            discovery = by_id[DISCOVERY_PROVIDER_ID]
            self.assertEqual("available", discovery["state"])
            self.assertFalse(discovery["authorized"])
        with self.subTest("each registration is listed with its declared capabilities"):
            self.assertEqual(CAPABILITY_BLOCK_KEYS, set(by_id[ACQUISITION_PROVIDER_ID]["capabilities"]))
            self.assertEqual(
                [API_HOST, ASSET_HOST], by_id[ACQUISITION_PROVIDER_ID]["capabilities"]["allowed_domains"]
            )
            self.assertEqual(
                [CREDENTIAL_ENV_VAR], by_id[ACQUISITION_PROVIDER_ID]["capabilities"]["credentials"]
            )
        with self.subTest("declared credential *names* only -- doctor has no values to leak"):
            self.assertNotIn(CREDENTIAL_VALUE, json.dumps(report))

    def test_a_workspace_with_nothing_installed_says_so_rather_than_staying_silent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=["openalex"])
            self.clear_registration_caches()
            report = self.success_report(
                *self.run_module(DOCTOR, ["--project-root", str(workspace), "--format", "json"])
            )

        check = self.doctor_check(report, "registered_providers")
        self.assertEqual("ok", check["status"])
        self.assertEqual({"registered": 0, "enabled": 0, "available": 0, "invalid": 0}, check["details"]["counts"])
        self.assertIn("No third-party providers are registered", check["message"])


class AuthorizedButUninstalledTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 7: an authorization the environment cannot satisfy stops the workspace.

    The backlog (§2.7) puts enforcement on smoke and explanation on doctor. The
    orchestration controller refuses too -- with ``CONFIG_INVALID``, deliberately: a
    missing distribution and a typo'd id are the same observation to the controller, so
    ``tests/test_orchestration_controller_registered_providers.py`` pins that a new code
    was *not* introduced for one of them. This test asserts the refusal actually happens
    and names the id; the sibling suite owns the argument for which code it carries.
    """

    def test_smoke_fails_and_the_controller_refuses_to_start_over_the_same_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=[ACQUISITION_PROVIDER_ID])
            self.clear_registration_caches()

            smoke_code, smoke_stdout, smoke_stderr = self.run_module(
                SMOKE, ["--project-root", str(workspace), "--format", "json"]
            )
            smoke_report = json.loads(smoke_stdout or smoke_stderr)
            controller_code, controller_stdout, controller_stderr = self.run_module(
                CONTROLLER,
                ["--project-root", str(workspace), "start", "--orchestration-id", "orch-cr5-e2e",
                 "--agent-id", "pm-agent", "--format", "json"],
            )

            with self.subTest("smoke refuses the workspace"):
                self.assertNotEqual(0, smoke_code)
                findings = [
                    issue
                    for issue in smoke_report["issues"]
                    if issue.get("error_code") == "PROVIDER_NOT_REGISTERED"
                ]
                self.assertEqual(1, len(findings), smoke_report["issues"])
                self.assertEqual("HIGH", findings[0]["severity"])
                self.assertEqual([ACQUISITION_PROVIDER_ID], findings[0]["actual"])
                self.assertIn("evidence_wiki.acquisition_providers", findings[0]["recommendation"])

            with self.subTest("and the controller will not open a session over it"):
                envelope = self.refusal_envelope(controller_code, controller_stdout, controller_stderr)
                self.assertEqual("CONFIG_INVALID", envelope["error_code"])
                self.assertEqual(
                    [ACQUISITION_PROVIDER_ID], envelope["details"]["unresolved_provider_ids"]
                )
                self.assertIn("entry-point group", envelope["remediation"])
                self.assertFalse(
                    (workspace / "runs" / "orchestrations" / "orch-cr5-e2e" / "session.json").exists(),
                    "a refused start must not leave a session behind",
                )

    def test_installing_the_distribution_is_the_whole_repair(self):
        """The same workspace, the same commands, one environment change."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), acquisition_providers=[ACQUISITION_PROVIDER_ID])
            with self.installed_plugins():
                smoke_code, smoke_stdout, smoke_stderr = self.run_module(
                    SMOKE, ["--project-root", str(workspace), "--format", "json"]
                )
                controller_code, controller_stdout, controller_stderr = self.run_module(
                    CONTROLLER,
                    ["--project-root", str(workspace), "start", "--orchestration-id", "orch-cr5-e2e-ok",
                     "--agent-id", "pm-agent", "--format", "json"],
                )

            smoke_report = json.loads(smoke_stdout or smoke_stderr)
            self.assertEqual(
                [],
                [issue for issue in smoke_report["issues"] if issue.get("error_code") == "PROVIDER_NOT_REGISTERED"],
            )
            session = self.success_report(controller_code, controller_stdout, controller_stderr)
            self.assertEqual([ACQUISITION_PROVIDER_ID], session["provider_policy"]["acquisition"]["providers"])


class RegisteredDiscoveryProposesCandidatesTests(RegisteredProviderWorkspace, unittest.TestCase):
    """Criterion 8: registered discovery proposes, through the normal hygiene, and nothing more."""

    def test_registered_search_writes_classified_candidates_and_touches_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(
                Path(tmpdir), acquisition_providers=[], discovery_providers=[DISCOVERY_PROVIDER_ID]
            )
            request_path = self.write_request(
                workspace, {"query": "retrieval benchmark"}, "requests/search.json"
            )
            before = tree_snapshot(workspace)
            opener = RecordingOpener(SEARCH_RESPONSE)

            with self.installed_plugins(), self.credential_in_the_environment(), mock.patch.object(
                DISCOVER, "REGISTERED_OPENER", opener
            ), mock.patch.object(DISCOVER, "REGISTERED_RESOLVER", public_resolver):
                report = self.success_report(*self.registered_search(workspace, request_path))

            candidates = self.candidate_records(workspace)
            self.assertEqual(1, len(candidates), candidates)
            candidate = candidates[0]

            with self.subTest("the candidate reached the store through the shared writer"):
                self.assertEqual(1, report["written"])
                self.assertEqual(DISCOVERY_PROVIDER_ID, candidate["provider"])
                self.assertEqual(f"https://{CATALOG_HOST}/product/B0ABC12345", candidate["url"])

            with self.subTest("it went through the normal hygiene, not around it"):
                # Classification and trust rejection are what every other candidate gets;
                # a registered provider buys reach, never a trust promotion.
                self.assertIn(candidate["recommended_action"], {"fetch", "review", "reject"})
                self.assertNotEqual("official_primary", candidate["trust_tier"])
                self.assertIsNot(True, candidate["official_source"])
                self.assertEqual(DISCOVERY_PROVIDER_ID, candidate["provider_registration"]["id"])
                self.assertEqual("discovery", candidate["provider_registration"]["phase"])

            with self.subTest("discovery never acquires: only the candidate store is written"):
                after = tree_snapshot(workspace)
                created = sorted(set(after) - set(before))
                # A subset rather than an equality: whether the store's directory already
                # existed is the initializer's business, but nothing outside these four
                # paths may appear -- above all nothing under raw/.
                self.assertLessEqual(
                    set(created),
                    {
                        "sources/discovery/",
                        "sources/discovery/.locks/",
                        "sources/discovery/.locks/candidates.lock",
                        "sources/discovery/candidates.jsonl",
                    },
                    created,
                )
                self.assertIn("sources/discovery/candidates.jsonl", created)
                changed = [path for path, payload in after.items() if path in before and before[path] != payload]
                self.assertEqual([], changed, "an existing file changed during a read-only command")

            with self.subTest("no credential value reaches the store or the report"):
                self.assertEqual(CREDENTIAL_VALUE, opener.requests[0].get_header("X-keepa-fixture-key"))
                self.assertNotIn(CREDENTIAL_VALUE, json.dumps(report))
                self.assertNotIn(CREDENTIAL_VALUE, json.dumps(candidates))

    def test_an_unauthorized_registered_discovery_id_is_refused_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(
                Path(tmpdir), acquisition_providers=[], discovery_providers=["openalex"]
            )
            request_path = self.write_request(
                workspace, {"query": "retrieval benchmark"}, "requests/search.json"
            )
            before = tree_snapshot(workspace)
            opener = RecordingOpener(SEARCH_RESPONSE)

            with self.installed_plugins(), mock.patch.object(DISCOVER, "REGISTERED_OPENER", opener):
                envelope = self.refusal_envelope(*self.registered_search(workspace, request_path))

            self.assertEqual("DISCOVERY_PROVIDER_DISABLED", envelope["error_code"])
            self.assertEqual([], opener.urls)
            self.assertEqual(before, tree_snapshot(workspace))


if __name__ == "__main__":
    unittest.main()
