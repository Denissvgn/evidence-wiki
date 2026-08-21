"""Why: CR-5 T5 is where every other piece of provider registration is finally spent.

`fetch_sources.py registered get` takes a pip-installed plugin that `research.yml`
authorized by id, asks it to *plan* HTTPS requests, executes those requests through the
package's own pinned transport against the plugin's *declared* `allowed_domains`, lets the
plugin interpret the responses, and writes the result as provenance-stamped evidence. The
plugin never opens a socket and never touches the workspace.

That makes this file the place where the CR's three non-negotiable properties become
observable, so these tests pin them rather than the implementation:

- **registration makes a provider available, never enabled** — an id the workspace did not
  authorize is refused with the same `ACQUISITION_PROVIDER_DISABLED` a built-in gets, and
  an authorized id nothing supplies is refused with `PROVIDER_NOT_REGISTERED` whose
  remediation distinguishes "not installed" from "installed but invalid";
- **the package enforces what it can** — the declaration bounds egress, the declared rate
  limit is spent durably *before* transport, and the plugin's return values are treated as
  untrusted input at every step (filename, source type, byte count, metadata);
- **the declaration is recorded for what it cannot** — the sidecar carries
  `provider_registration` and `provider_capabilities`, credential *names* only.

Plus the property that makes all of it safe to retry: **every refusal leaves zero new
files**. The refusal tests assert the workspace tree is byte-identical to what it was
before the command ran, not merely that the artifact is absent.

No test here touches the network or DNS. The transport is stubbed either at
`execute_planned_request` (when the subject is the flow) or at the `REGISTERED_OPENER`
seam (when the subject is the transport's own domain enforcement). The fixture provider
declares hosts under the reserved `.invalid` TLD, so a test that escaped both stubs would
fail on resolution rather than reach a live service.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests._provider_plugin_fixture import ACQUISITION_PROVIDER_ID, installed_provider_plugins
from tests._script_loader import load_module as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FETCH_PATH = SCRIPTS / "fetch_sources.py"
ACCOUNTING_PATH = SCRIPTS / "_provider_accounting.py"

FETCH_MODULE_NAME = "cr5_u7_registered_fetch_sources"
ACCOUNTING_MODULE_NAME = "cr5_u7_registered_provider_accounting"
LOCKS_MODULE_NAME = "cr5_u7_registered_workspace_locks"

#: The declaration the fixture ships, restated here so a silent change to it fails loudly.
CREDENTIAL_ENV_VAR = "KEEPA_FIXTURE_API_KEY"
CREDENTIAL_VALUE = "keepa-fixture-live-4f21c8-never-log-me"
API_HOST = "api.keepa-fixture.invalid"
ASSET_HOST = "assets.keepa-fixture.invalid"
DECLARED_DOMAINS = (API_HOST, ASSET_HOST)
BROKEN_DECLARATION_ID = "Keepa_Broken_Fixture"

REQUEST_ASIN = "B0FIXTURE1"
ARTIFACT_FILENAME = "keepa-fixture-b0fixture1.json"

PRODUCT_RESPONSE = json.dumps({"title": "Fixture Widget", "currency": "USD"}).encode("utf-8")
HISTORY_RESPONSE = b"date,price\n2026-01-01,19.99\n2026-01-02,18.49\n"
PLANNED_RESPONSES = (PRODUCT_RESPONSE, HISTORY_RESPONSE)


def tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Return every path under ``root`` with its bytes; directories map to ``None``."""
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

    def __init__(self, fetch, payloads=PLANNED_RESPONSES, content_type: str = "application/json"):
        self.fetch = fetch
        self.payloads = list(payloads)
        self.content_type = content_type
        self.calls: list[dict[str, object]] = []

    def __call__(self, planned, **kwargs):
        index = len(self.calls)
        self.calls.append({"url": planned.url, "method": planned.method, "headers": tuple(planned.headers), **kwargs})
        if index >= len(self.payloads):
            raise AssertionError(f"transport called {index + 1} time(s) but only {len(self.payloads)} payload(s) exist")
        payload = self.payloads[index]
        return self.fetch.DownloadResult(
            content=payload,
            final_url=planned.url,
            byte_count=len(payload),
            checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            http_status=200,
            content_type=self.content_type,
            redirect_chain=[],
            tls_verified=True,
        )


class RefusingTransport:
    """A transport that fails the test if it is ever reached."""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, planned, **kwargs):
        self.calls.append(getattr(planned, "url", "<unreadable>"))
        raise AssertionError("transport was executed on a path that must refuse before any network work")


class StubResponse:
    """Minimal ``urlopen`` result for the ``REGISTERED_OPENER`` seam."""

    def __init__(self, url: str, payload: bytes, content_type: str = "application/json"):
        self.url = url
        self._payload = payload
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}
        self.status = 200

    def read(self, size: int = -1) -> bytes:
        if not self._payload:
            return b""
        chunk = self._payload if size is None or size < 0 else self._payload[:size]
        self._payload = b"" if size is None or size < 0 else self._payload[size:]
        return chunk

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FetchSourcesRegisteredTests(unittest.TestCase):
    def setUp(self):
        self.fetch = load_script_module(FETCH_MODULE_NAME, FETCH_PATH)
        self.accounting = load_script_module(ACCOUNTING_MODULE_NAME, ACCOUNTING_PATH)
        self.addCleanup(self.clear_registration_cache)
        self.addCleanup(self.fetch._acquisition_transport.reset_registered_secrets)

    # --- fixtures ------------------------------------------------------------

    def clear_registration_cache(self) -> None:
        """Drop the loader cache the fetch script holds.

        ``load_workspace_module`` deliberately keeps sibling modules out of ``sys.modules``,
        so the fixture helper's own cache sweep cannot reach this copy of the loader.
        """
        self.fetch._provider_plugins.clear_cache()

    @contextlib.contextmanager
    def installed_plugins(self, *variants: str, base: bool = True):
        """Install fixture distributions with the fetch script's loader cache cleared too."""
        self.clear_registration_cache()
        try:
            with installed_provider_plugins(*variants, base=base) as handle:
                self.clear_registration_cache()
                yield handle
        finally:
            self.clear_registration_cache()

    def build_workspace(
        self,
        root: Path,
        *,
        providers: list[str] | None = None,
        max_downloads_per_run: int = 10,
        registered: dict | None = None,
    ) -> Path:
        workspace = root / "workspace"
        for relative in ("raw/data", "raw/papers", "sources", "wiki", "requests"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        acquisition: dict = {
            "enabled": True,
            "providers": [ACQUISITION_PROVIDER_ID] if providers is None else providers,
            "target_root": "raw/papers",
            "max_downloads_per_run": max_downloads_per_run,
            "require_license_check": True,
        }
        if registered is not None:
            acquisition["registered"] = registered
        config = {
            "project": {"name": "fetch-sources-registered-test"},
            "raw": {"source_roots": ["raw/data", "raw/papers"]},
            "sources": {"manifest_path": "sources/manifest.jsonl"},
            "wiki": {"root": "wiki", "required_dirs": []},
            "integrations": {"acquisition": acquisition},
        }
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return workspace

    def write_request(self, workspace: Path, document: object = None, name: str = "requests/keepa.json") -> str:
        payload = {"asin": REQUEST_ASIN, "history_days": 30} if document is None else document
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")
        return name

    def write_active_run(self, workspace: Path, run_id: str = "run-registered-budget") -> Path:
        run_dir = workspace / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "run_id": run_id,
                    "started_at": "2000-01-01T00:00:00Z",
                    "state": {"current": "acquiring"},
                }
            ),
            encoding="utf-8",
        )
        return run_dir

    def run_fetch(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.fetch.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def registered_get(self, workspace: Path, *extra: str, provider_id: str = ACQUISITION_PROVIDER_ID) -> list[str]:
        return [
            "--project-root",
            str(workspace),
            "--format",
            "json",
            "registered",
            "get",
            "--id",
            provider_id,
            "--request-file",
            "requests/keepa.json",
            *extra,
        ]

    def refusal_envelope(self, stdout: str, stderr: str, code: int) -> dict:
        self.assertEqual(2, code)
        self.assertEqual("", stdout, "a refusal must leave stdout empty")
        return json.loads(stderr)

    # --- happy path ----------------------------------------------------------

    def test_registered_get_writes_artifact_and_sidecar_carrying_both_provenance_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            transport = RecordingTransport(self.fetch)
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "execute_planned_request", transport
            ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            self.assertEqual(0, code, stderr)
            report = json.loads(stdout)
            artifact = workspace / "raw/data" / ARTIFACT_FILENAME
            sidecar = Path(f"{artifact}.provenance.yml")
            self.assertTrue(artifact.is_file())
            self.assertTrue(sidecar.is_file())
            self.assertFalse(self.fetch.acquisition_marker_path(artifact).exists())

            document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            registration = document["provider_registration"]
            capabilities = document["provider_capabilities"]
            with self.subTest("registration block names the packaging identity"):
                self.assertEqual(ACQUISITION_PROVIDER_ID, registration["id"])
                self.assertEqual("acquisition", registration["phase"])
                self.assertEqual("keepa-fixture", registration["distribution"])
                self.assertEqual("0.1.0", registration["version"])
                self.assertEqual(1, registration["provider_api_version"])
                self.assertEqual(ACQUISITION_PROVIDER_ID, registration["entry_point"])
            with self.subTest("capability block records the declaration, credential names only"):
                self.assertEqual(list(DECLARED_DOMAINS), capabilities["allowed_domains"])
                self.assertEqual([CREDENTIAL_ENV_VAR], capabilities["credentials"])
                self.assertEqual({"requests": 60, "per": "minute"}, capabilities["rate_limit"])
                self.assertEqual("partial", capabilities["license_inference"])
                self.assertTrue(capabilities["captures_raw"])
                self.assertTrue(capabilities["quarantine_on_incomplete"])
            with self.subTest("the artifact is the provider's interpretation, not a raw response"):
                self.assertEqual("dataset", document["source_type"])
                self.assertEqual(len(artifact.read_bytes()), document["byte_count"])
                interpreted = json.loads(artifact.read_text(encoding="utf-8"))
                self.assertEqual(REQUEST_ASIN, interpreted["asin"])
                self.assertEqual("Fixture Widget", interpreted["title"])
            with self.subTest("both planned requests were executed, in plan order"):
                self.assertEqual(2, len(transport.calls))
                self.assertTrue(transport.calls[0]["url"].startswith(f"https://{API_HOST}/product"))
                self.assertTrue(transport.calls[1]["url"].startswith(f"https://{ASSET_HOST}/product/"))
                self.assertEqual(2, report["planned_requests"])
            with self.subTest("the report repeats what the sidecar recorded"):
                self.assertEqual(ACQUISITION_PROVIDER_ID, report["provider_id"])
                self.assertEqual("registered", report["provider"])
                self.assertEqual("get", report["command"])
                self.assertEqual("raw/data/" + ARTIFACT_FILENAME, report["target_path"])
                self.assertEqual(registration, report["provider_registration"])
                self.assertEqual(capabilities, report["provider_capabilities"])

    def test_retrieved_by_keeps_a_registered_artifact_inside_the_cumulative_run_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            run_dir = self.write_active_run(workspace)
            self.write_request(workspace)
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
            ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))
            self.assertEqual(0, code, stderr)

            artifact = workspace / "raw/data" / ARTIFACT_FILENAME
            document = yaml.safe_load(Path(f"{artifact}.provenance.yml").read_text(encoding="utf-8"))
            self.assertEqual(
                f"fetch_sources.py/registered:{ACQUISITION_PROVIDER_ID}",
                document["retrieved_by"],
            )
            self.assertTrue(document["retrieved_by"].startswith("fetch_sources.py/"))
            usage = self.fetch.retained_acquisition_usage(
                workspace,
                {"run_id": run_dir.name, "started_at": self.fetch.parse_utc_timestamp("2000-01-01T00:00:00Z")},
            )
            self.assertEqual(1, usage["downloads"], "the artifact must count against max_downloads_per_run")

            report = json.loads(stdout)
            ledger = run_dir / "provider-requests.jsonl"
            self.assertTrue(ledger.is_file())
            self.assertNotEqual("academic-provider-requests.jsonl", ledger.name)
            self.assertEqual(2, report["provider_requests_reserved"])
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([ACQUISITION_PROVIDER_ID] * 2, [record["provider"] for record in records])

    def test_max_downloads_per_run_still_bounds_registered_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), max_downloads_per_run=1)
            run_dir = self.write_active_run(workspace)
            self.write_request(workspace)
            (workspace / "requests" / "keepa2.json").write_text(json.dumps({"asin": "B0FIXTURE2"}), encoding="utf-8")
            second_argv = self.registered_get(workspace)
            second_argv[second_argv.index("requests/keepa.json")] = "requests/keepa2.json"
            with self.installed_plugins(), mock.patch.dict(
                "os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False
            ):
                # A fresh transport per command: each one must see exactly its own plan.
                with mock.patch.object(self.fetch, "execute_planned_request", RecordingTransport(self.fetch)):
                    first, _stdout, stderr = self.run_fetch(*self.registered_get(workspace))
                self.assertEqual(0, first, stderr)
                exhausted = RefusingTransport()
                with mock.patch.object(self.fetch, "execute_planned_request", exhausted):
                    code, stdout, stderr = self.run_fetch(*second_argv)

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("ACQUISITION_LIMIT_EXCEEDED", envelope["error_code"])
            self.assertEqual([], exhausted.calls, "an exhausted run budget refuses before the network")
            self.assertFalse((workspace / "raw/data" / "keepa-fixture-b0fixture2.json").exists())
            self.assertEqual(
                [],
                sorted(run_dir.parent.parent.glob("raw/data/.*acquisition-incomplete.json")),
                "a budget refusal must not leave a marker behind",
            )

    # --- authorization and registration --------------------------------------

    def test_unauthorized_registered_provider_is_refused_as_acquisition_provider_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), providers=["web"])
            self.write_request(workspace)
            before = tree_snapshot(workspace)
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "execute_planned_request", RefusingTransport()
            ):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("ACQUISITION_PROVIDER_DISABLED", envelope["error_code"])
            self.assertIn("integrations.acquisition.providers", envelope["message"])
            self.assertEqual(before, tree_snapshot(workspace))

    def test_unauthorized_message_is_identical_for_built_in_and_registered_ids(self):
        """The refusal is about authorization, so it must not leak whether an id is installed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), providers=["web"])
            self.write_request(workspace)
            with self.installed_plugins():
                _code, _stdout, installed_stderr = self.run_fetch(*self.registered_get(workspace))
            _code, _stdout, uninstalled_stderr = self.run_fetch(*self.registered_get(workspace))

        installed = json.loads(installed_stderr)
        uninstalled = json.loads(uninstalled_stderr)
        self.assertEqual("ACQUISITION_PROVIDER_DISABLED", installed["error_code"])
        self.assertEqual(installed, uninstalled)

    def test_authorized_but_unregistered_provider_is_refused_as_provider_not_registered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            before = tree_snapshot(workspace)
            self.clear_registration_cache()
            code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_NOT_REGISTERED", envelope["error_code"])
            self.assertIn(ACQUISITION_PROVIDER_ID, envelope["message"])
            with self.subTest("remediation names the entry-point group and the install fix"):
                self.assertIn("evidence_wiki.acquisition_providers", envelope["remediation"])
                self.assertIn("Install a distribution", envelope["remediation"])
            self.assertEqual(before, tree_snapshot(workspace))

    def test_installed_but_invalid_declaration_is_refused_as_provider_registration_invalid(self):
        # The invalid-declaration fixture declares id "Keepa_Broken_Fixture" (one of the
        # rules it breaks), so that is the id an operator would have to authorize -- and
        # the id the loader matches its broken registration against.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), providers=[BROKEN_DECLARATION_ID])
            self.write_request(workspace)
            before = tree_snapshot(workspace)
            with self.installed_plugins("invalid-declaration", base=False):
                code, stdout, stderr = self.run_fetch(
                    *self.registered_get(workspace, provider_id=BROKEN_DECLARATION_ID)
                )

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_REGISTRATION_INVALID", envelope["error_code"])
            with self.subTest("the message names the distribution and its violations"):
                self.assertIn("keepa-broken-fixture", envelope["message"])
                self.assertIn("allowed_domains", envelope["message"])
            with self.subTest("remediation says upgrade, not install"):
                self.assertIn("Upgrade or fix", envelope["remediation"])
                self.assertIn("evidence_wiki.acquisition_providers", envelope["remediation"])
            self.assertEqual(before, tree_snapshot(workspace))

    def test_built_in_provider_ids_are_not_reachable_through_the_registered_subcommand(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), providers=["web", ACQUISITION_PROVIDER_ID])
            self.write_request(workspace)
            with self.installed_plugins():
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace, provider_id="web"))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_NOT_REGISTERED", envelope["error_code"])
            self.assertEqual(
                {"arxiv", "openalex", "github", "web"},
                set(self.fetch.PROVIDER_REGISTRY),
                "registration must never widen the built-in registry",
            )

    def test_registration_does_not_change_the_built_in_unknown_provider_message(self):
        """Hosts parse this sentence; with nothing installed it must stay verbatim."""
        with self.assertRaises(SystemExit) as ctx:
            self.fetch.validate_provider_list(
                ["gitlab"],
                "integrations.acquisition.providers",
                require_non_empty=True,
            )
        message = str(ctx.exception)
        self.assertIn("has unknown provider(s): gitlab", message)
        self.assertIn("Allowed providers: arxiv, openalex, github, web", message)
        self.assertNotIn("Registered providers", message)

    def test_an_authorized_registered_id_passes_provider_list_validation_for_built_ins(self):
        """A workspace that authorizes a registered id must not break `web get`."""
        with self.installed_plugins():
            providers = self.fetch.validate_provider_list(
                ["web", ACQUISITION_PROVIDER_ID],
                "integrations.acquisition.providers",
                require_non_empty=True,
                registered=self.fetch.registered_acquisition_ids(),
            )
        self.assertEqual(["web", ACQUISITION_PROVIDER_ID], providers)

    # --- request document ----------------------------------------------------

    def test_malformed_request_document_is_refused_as_provider_request_invalid(self):
        cases = {
            "not a JSON object": "[1, 2, 3]",
            "not JSON at all": "{not json",
            "refused by the provider": json.dumps({"asin": "too-short"}),
            "unknown provider field": json.dumps({"asin": REQUEST_ASIN, "colour": "blue"}),
        }
        for label, payload in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.build_workspace(Path(tmpdir))
                self.write_request(workspace, payload)
                before = tree_snapshot(workspace)
                transport = RefusingTransport()
                with self.installed_plugins(), mock.patch.object(
                    self.fetch, "execute_planned_request", transport
                ):
                    code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

                envelope = self.refusal_envelope(stdout, stderr, code)
                self.assertEqual("PROVIDER_REQUEST_INVALID", envelope["error_code"])
                self.assertEqual([], transport.calls)
                self.assertEqual(before, tree_snapshot(workspace))

    def test_provider_reason_for_a_refused_request_is_carried_into_the_detail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace, {"asin": REQUEST_ASIN, "history_days": 9999})
            with self.installed_plugins():
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

        envelope = self.refusal_envelope(stdout, stderr, code)
        self.assertEqual("PROVIDER_REQUEST_INVALID", envelope["error_code"])
        self.assertIn("history_days", envelope["message"])
        self.assertIn("ValueError", envelope["message"])

    def test_a_missing_request_file_is_refused_before_any_transport(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            before = tree_snapshot(workspace)
            transport = RefusingTransport()
            with self.installed_plugins(), mock.patch.object(self.fetch, "execute_planned_request", transport):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_REQUEST_INVALID", envelope["error_code"])
            self.assertEqual([], transport.calls)
            self.assertEqual(before, tree_snapshot(workspace))

    # --- plan envelope -------------------------------------------------------

    def test_over_cap_plan_is_refused_as_provider_plan_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            before = tree_snapshot(workspace)
            transport = RefusingTransport()
            with self.installed_plugins():
                import keepa_fixture

                oversized = tuple(
                    keepa_fixture.PlannedRequest(url=f"https://{API_HOST}/page/{index}")
                    for index in range(self.fetch.REGISTERED_MAX_PLANNED_REQUESTS + 1)
                )
                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider,
                    "plan_fetch",
                    lambda self, request: oversized,
                ), mock.patch.object(self.fetch, "execute_planned_request", transport):
                    code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
            self.assertIn(str(self.fetch.REGISTERED_MAX_PLANNED_REQUESTS), envelope["message"])
            self.assertEqual([], transport.calls, "the cap is checked before anything executes")
            self.assertEqual(before, tree_snapshot(workspace))

    def test_a_malformed_planned_request_refuses_the_whole_plan_before_the_first_call(self):
        cases = {
            "non-https scheme": "http://api.keepa-fixture.invalid/product",
            "unparseable URL": "https://api.keepa-fixture.invalid:notaport/product",
            "credential in the URL": "https://api.keepa-fixture.invalid/p?k={{credential:KEEPA_FIXTURE_API_KEY}}",
        }
        for label, bad_url in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.build_workspace(Path(tmpdir))
                self.write_request(workspace)
                before = tree_snapshot(workspace)
                transport = RefusingTransport()
                with self.installed_plugins():
                    import keepa_fixture

                    plan = (
                        keepa_fixture.PlannedRequest(url=f"https://{API_HOST}/product?asin={REQUEST_ASIN}"),
                        keepa_fixture.PlannedRequest(url=bad_url),
                    )
                    with mock.patch.object(
                        keepa_fixture.KeepaFixtureAcquisitionProvider,
                        "plan_fetch",
                        lambda self, request, _plan=plan: _plan,
                    ), mock.patch.object(self.fetch, "execute_planned_request", transport):
                        code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

                envelope = self.refusal_envelope(stdout, stderr, code)
                self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
                self.assertEqual([], transport.calls, "the first request must not run when the second is malformed")
                self.assertEqual(before, tree_snapshot(workspace))

    def test_an_undeclared_credential_placeholder_refuses_the_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            transport = RefusingTransport()
            with self.installed_plugins():
                import keepa_fixture

                plan = (
                    keepa_fixture.PlannedRequest(
                        url=f"https://{API_HOST}/product",
                        headers=(("X-Other", "{{credential:UNDECLARED_TOKEN}}"),),
                    ),
                )
                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider, "plan_fetch", lambda self, request: plan
                ), mock.patch.object(self.fetch, "execute_planned_request", transport):
                    code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
            self.assertIn("UNDECLARED_TOKEN", envelope["message"])
            self.assertEqual([], transport.calls)

    def test_an_unset_declared_credential_refuses_by_name_without_a_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            transport = RefusingTransport()
            environment = dict(os.environ)
            environment.pop(CREDENTIAL_ENV_VAR, None)
            with self.installed_plugins(), mock.patch.dict(
                "os.environ", environment, clear=True
            ), mock.patch.object(self.fetch, "execute_planned_request", transport):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
            self.assertIn(CREDENTIAL_ENV_VAR, envelope["message"])
            self.assertEqual([], transport.calls)

    # --- declared domains ----------------------------------------------------

    def test_a_planned_url_outside_the_declaration_is_refused_as_domain_not_declared(self):
        """Exercised through the real transport executor, with an opener that must never run."""
        opened: list[str] = []

        def opener(request, timeout):
            opened.append(request.full_url)
            return StubResponse(request.full_url, PRODUCT_RESPONSE)

        for label, url in {
            "unrelated host": "https://evil.example.invalid/product",
            "declared host as a suffix of another": f"https://not-{API_HOST}/product",
        }.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.build_workspace(Path(tmpdir))
                self.write_request(workspace)
                before = tree_snapshot(workspace)
                opened.clear()
                with self.installed_plugins():
                    import keepa_fixture

                    plan = (keepa_fixture.PlannedRequest(url=url),)
                    with mock.patch.object(
                        keepa_fixture.KeepaFixtureAcquisitionProvider,
                        "plan_fetch",
                        lambda self, request, _plan=plan: _plan,
                    ), mock.patch.object(self.fetch, "REGISTERED_OPENER", opener):
                        code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

                envelope = self.refusal_envelope(stdout, stderr, code)
                self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", envelope["error_code"])
                self.assertEqual([], opened, "the declaration is checked before any socket work")
                self.assertEqual(before, tree_snapshot(workspace))

    def test_a_subdomain_of_a_declared_host_is_inside_the_declaration(self):
        opened: list[str] = []

        def opener(request, timeout):
            opened.append(request.full_url)
            return StubResponse(request.full_url, PRODUCT_RESPONSE)

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            with self.installed_plugins():
                import keepa_fixture

                plan = (
                    keepa_fixture.PlannedRequest(url=f"https://eu.{API_HOST}/product"),
                    keepa_fixture.PlannedRequest(url=f"https://{ASSET_HOST}/history.csv"),
                )
                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider, "plan_fetch", lambda self, request: plan
                ), mock.patch.object(self.fetch, "REGISTERED_OPENER", opener):
                    code, _stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            self.assertEqual(0, code, stderr)
            self.assertEqual([f"https://eu.{API_HOST}/product", f"https://{ASSET_HOST}/history.csv"], opened)

    # --- rate limiting -------------------------------------------------------

    def test_the_declared_rate_limit_refuses_before_any_transport(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            run_dir = self.write_active_run(workspace)
            self.write_request(workspace)
            # The fixture declares 60 requests per minute and plans two, so 59 already
            # spent in the rolling window leaves room for one.
            self.accounting.reserve(
                run_dir,
                ACQUISITION_PROVIDER_ID,
                59,
                ledger_filename=self.fetch.REGISTERED_LEDGER_FILENAME,
                lock_filename=self.fetch.REGISTERED_LEDGER_LOCK_FILENAME,
                schema_version=self.fetch.ACCOUNTING_SCHEMA_VERSION,
            )
            before = tree_snapshot(workspace)
            transport = RefusingTransport()
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "execute_planned_request", transport
            ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("ACQUISITION_PROVIDER_RATE_LIMITED", envelope["error_code"])
            self.assertIn("60 per minute", envelope["message"])
            self.assertEqual([], transport.calls, "the ceiling is spent before the network, not after")
            with self.subTest("a refused overage records nothing and leaves the tree untouched"):
                self.assertEqual(before, tree_snapshot(workspace))

    def test_ledger_lock_contention_is_reported_as_an_envelope_not_a_traceback(self):
        """The ledger holds its own copy of the lock module, so its error class differs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_active_run(workspace)
            self.write_request(workspace)
            locks = load_script_module(LOCKS_MODULE_NAME, SCRIPTS / "_workspace_locks.py")
            self.assertIsNot(
                locks.LockUnavailableError,
                self.fetch.LockUnavailableError,
                "this test is only meaningful while the two module copies stay distinct",
            )
            contended = locks.LockUnavailableError(
                "another process holds the provider-call budget lock",
                details={"path": "runs/x/.locks/provider-requests.lock"},
            )
            transport = RefusingTransport()
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "reserve_provider_requests", side_effect=contended
            ), mock.patch.object(self.fetch, "execute_planned_request", transport), mock.patch.dict(
                "os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False
            ):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual(contended.error_code, envelope["error_code"])
            self.assertEqual([], transport.calls)

    def test_a_plan_larger_than_the_declared_ceiling_is_refused_without_an_active_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            before = tree_snapshot(workspace)
            transport = RefusingTransport()
            with self.installed_plugins():
                import keepa_fixture

                plan = tuple(
                    keepa_fixture.PlannedRequest(url=f"https://{API_HOST}/page/{index}") for index in range(4)
                )
                tight = keepa_fixture.ProviderCapabilities(
                    allowed_domains=DECLARED_DOMAINS,
                    terms_urls=(f"https://{API_HOST}/terms",),
                    license_inference="partial",
                    rate_limit=keepa_fixture.RateLimit(requests=2, per="minute"),
                    credentials=(CREDENTIAL_ENV_VAR,),
                )
                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider, "plan_fetch", lambda self, request: plan
                ), mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider, "capabilities", tight
                ), mock.patch.object(self.fetch, "execute_planned_request", transport):
                    code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("ACQUISITION_PROVIDER_RATE_LIMITED", envelope["error_code"])
            self.assertIn("2 per minute", envelope["message"])
            self.assertEqual([], transport.calls)
            self.assertEqual(before, tree_snapshot(workspace))

    # --- interpretation and artifact validation ------------------------------

    def test_a_provider_that_raises_inside_interpret_leaves_zero_new_files(self):
        for label, error in {
            "a deliberate refusal": ValueError("keepa-fixture: the product response is not a JSON object"),
            "an outright plugin bug": RuntimeError("boom"),
        }.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.build_workspace(Path(tmpdir))
                self.write_request(workspace)
                before = tree_snapshot(workspace)

                def exploding(self, request, responses, _error=error):
                    raise _error

                with self.installed_plugins():
                    import keepa_fixture

                    with mock.patch.object(
                        keepa_fixture.KeepaFixtureAcquisitionProvider, "interpret", exploding
                    ), mock.patch.object(
                        self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
                    ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                        code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

                envelope = self.refusal_envelope(stdout, stderr, code)
                self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
                with self.subTest("no artifact, no sidecar, no incomplete marker"):
                    self.assertEqual(
                        before,
                        tree_snapshot(workspace),
                        "an interpret-time failure must leave the workspace byte-identical",
                    )

    def test_a_failed_acquisition_still_spends_the_provider_calls_it_already_made(self):
        """Deliberate asymmetry: the ledger is spent before transport, so a later failure
        leaves the calls recorded. Over-counting a failed attempt is a smaller failure than
        letting a retry loop spend an unbounded budget -- but the evidence tree stays clean.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            run_dir = self.write_active_run(workspace)
            self.write_request(workspace)
            raw_before = tree_snapshot(workspace / "raw")
            with self.installed_plugins():
                import keepa_fixture

                def exploding(self, request, responses):
                    raise RuntimeError("interpret blew up after both calls were made")

                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider, "interpret", exploding
                ), mock.patch.object(
                    self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
                ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                    code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("PROVIDER_PLAN_INVALID", envelope["error_code"])
            self.assertEqual(raw_before, tree_snapshot(workspace / "raw"), "no evidence, no sidecar, no marker")
            ledger = run_dir / "provider-requests.jsonl"
            records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(2, len(records), "the two calls that were actually made stay spent")

    def test_a_target_root_outside_the_evidence_tree_is_refused(self):
        for label, target_root in {"outside raw/": "sources", "escaping upward": "raw/../../etc"}.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.build_workspace(Path(tmpdir))
                self.write_request(workspace)
                before = tree_snapshot(workspace)
                transport = RefusingTransport()
                with self.installed_plugins(), mock.patch.object(
                    self.fetch, "execute_planned_request", transport
                ):
                    code, stdout, stderr = self.run_fetch(
                        *self.registered_get(workspace, "--target-root", target_root)
                    )

                envelope = self.refusal_envelope(stdout, stderr, code)
                self.assertEqual("ACQUISITION_PATH_UNSAFE", envelope["error_code"])
                self.assertEqual([], transport.calls)
                self.assertEqual(before, tree_snapshot(workspace))

    def test_an_untrusted_artifact_descriptor_is_refused_field_by_field(self):
        with self.installed_plugins():
            import keepa_fixture

            cases = {
                "a filename escaping the target root": keepa_fixture.SourceArtifact(
                    filename="../../escape.json", source_type="dataset", content=b"{}"
                ),
                "an absolute filename": keepa_fixture.SourceArtifact(
                    filename="/etc/passwd", source_type="dataset", content=b"{}"
                ),
                "a hidden filename": keepa_fixture.SourceArtifact(
                    filename=".hidden.json", source_type="dataset", content=b"{}"
                ),
                "an unknown source type": keepa_fixture.SourceArtifact(
                    filename="ok.json", source_type="totally-made-up", content=b"{}"
                ),
                "content that is not bytes": keepa_fixture.SourceArtifact(
                    filename="ok.json", source_type="dataset", content="a string"
                ),
                "metadata that is not JSON-serializable": keepa_fixture.SourceArtifact(
                    filename="ok.json",
                    source_type="dataset",
                    content=b"{}",
                    provenance_metadata={"handle": object()},
                ),
            }
            for label, artifact in cases.items():
                with self.subTest(label), tempfile.TemporaryDirectory() as tmpdir:
                    workspace = self.build_workspace(Path(tmpdir))
                    self.write_request(workspace)
                    before = tree_snapshot(workspace)
                    with mock.patch.object(
                        keepa_fixture.KeepaFixtureAcquisitionProvider,
                        "interpret",
                        lambda self, request, responses, _artifact=artifact: _artifact,
                    ), mock.patch.object(
                        self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
                    ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                        code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

                    envelope = self.refusal_envelope(stdout, stderr, code)
                    self.assertIn(
                        envelope["error_code"],
                        {"PROVIDER_PLAN_INVALID", "ACQUISITION_PATH_UNSAFE"},
                    )
                    self.assertEqual(before, tree_snapshot(workspace))

    def test_plugin_metadata_cannot_plant_a_policy_field_in_the_sidecar_root(self):
        """Untrusted hints are nested; a forged repository_artifact_kind must not steer budgets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            with self.installed_plugins():
                import keepa_fixture

                forged = keepa_fixture.SourceArtifact(
                    filename="forged.json",
                    source_type="dataset",
                    content=b'{"ok": true}',
                    provenance_metadata={
                        "repository_artifact_kind": "source_archive",
                        "retrieved_by": "someone_else.py/trusted",
                        "byte_count": 1,
                    },
                )
                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider,
                    "interpret",
                    lambda self, request, responses: forged,
                ), mock.patch.object(
                    self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
                ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                    code, _stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            self.assertEqual(0, code, stderr)
            sidecar = workspace / "raw/data" / "forged.json.provenance.yml"
            document = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
            self.assertNotIn("repository_artifact_kind", document)
            self.assertEqual(
                f"fetch_sources.py/registered:{ACQUISITION_PROVIDER_ID}",
                document["retrieved_by"],
            )
            self.assertEqual(len(b'{"ok": true}'), document["byte_count"])
            self.assertEqual("source_archive", document["provider_metadata"]["repository_artifact_kind"])

    # --- credential custody --------------------------------------------------

    def test_no_resolved_credential_value_reaches_any_output(self):
        opened: list[object] = []

        def opener(request, timeout):
            opened.append(dict(request.header_items()))
            raise self.fetch.AcquisitionTransportError(
                "ACQUISITION_HTTP_ERROR",
                f"upstream rejected the key {CREDENTIAL_VALUE}",
                remediation=f"rotate {CREDENTIAL_VALUE}",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "REGISTERED_OPENER", opener
            ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            with self.subTest("the transport received the resolved value"):
                self.assertTrue(any(CREDENTIAL_VALUE in str(headers) for headers in opened))
            with self.subTest("no rendered diagnostic carries it"):
                self.assertNotIn(CREDENTIAL_VALUE, stderr)
                self.assertNotIn(CREDENTIAL_VALUE, json.dumps(envelope))
                self.assertIn("[REDACTED]", envelope["message"])

    def test_metadata_a_provider_echoed_back_is_redacted_before_it_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            with self.installed_plugins():
                import keepa_fixture

                echoed = keepa_fixture.SourceArtifact(
                    filename="echoed.json",
                    source_type="dataset",
                    content=b"{}",
                    provenance_metadata={
                        "upstream_echo": f"the service replied with key={CREDENTIAL_VALUE}",
                        "nested": [{"also": CREDENTIAL_VALUE}],
                    },
                )
                with mock.patch.object(
                    keepa_fixture.KeepaFixtureAcquisitionProvider,
                    "interpret",
                    lambda self, request, responses: echoed,
                ), mock.patch.object(
                    self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
                ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                    code, _stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            self.assertEqual(0, code, stderr)
            sidecar = workspace / "raw/data" / "echoed.json.provenance.yml"
            text = sidecar.read_text(encoding="utf-8")
            self.assertNotIn(CREDENTIAL_VALUE, text)
            self.assertIn("[REDACTED]", text)

    def test_the_sidecar_records_credential_names_and_never_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
            ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            self.assertEqual(0, code, stderr)
            sidecar = workspace / "raw/data" / f"{ARTIFACT_FILENAME}.provenance.yml"
            text = sidecar.read_text(encoding="utf-8")
            self.assertIn(CREDENTIAL_ENV_VAR, text)
            self.assertNotIn(CREDENTIAL_VALUE, text)
            self.assertNotIn(CREDENTIAL_VALUE, stdout)

    # --- machine-output shape ------------------------------------------------

    def test_json_format_emits_exactly_one_document_on_stdout_with_diagnostics_on_stderr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.write_request(workspace)
            with self.installed_plugins(), mock.patch.object(
                self.fetch, "execute_planned_request", RecordingTransport(self.fetch)
            ), mock.patch.dict("os.environ", {CREDENTIAL_ENV_VAR: CREDENTIAL_VALUE}, clear=False):
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            decoder = json.JSONDecoder()
            report, offset = decoder.raw_decode(stdout.strip())
            self.assertEqual(len(stdout.strip()), offset, "stdout must hold exactly one JSON document")
            self.assertEqual("1.0", report["schema_version"])

    def test_acquisition_disabled_still_wins_over_every_registration_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
            config["integrations"]["acquisition"]["enabled"] = False
            (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            self.write_request(workspace)
            before = tree_snapshot(workspace)
            with self.installed_plugins():
                code, stdout, stderr = self.run_fetch(*self.registered_get(workspace))

            envelope = self.refusal_envelope(stdout, stderr, code)
            self.assertEqual("ACQUISITION_DISABLED", envelope["error_code"])
            self.assertEqual(before, tree_snapshot(workspace))


if __name__ == "__main__":
    unittest.main()
