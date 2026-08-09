"""The fixture provider distribution and its install helper are themselves under test.

Every other CR-5 unit tests *against* this fixture, so a defect here would be diagnosed
somewhere else: a loader bug and a fixture bug look identical from a loader test. These
tests pin the three properties the rest of the work depends on.

**Discovery is real.** The distribution is found by ordinary ``importlib.metadata`` entry
point enumeration over a real ``.dist-info`` directory — no monkeypatched seam, no pip, no
venv mutation. If the fixture only worked through a stub, it would prove nothing about
registration.

**The plugin is package-free.** ``keepa_fixture`` never imports ``evidence_wiki``. The
loader validates registrations structurally, so a plugin authored without the package
installed must be valid; that claim is only tested if at least one fixture is written that
way. It is asserted twice here — once over the source, once by importing the module with
``evidence_wiki`` blocked at the meta path.

**Nothing leaks.** The helper runs inside a shared-interpreter suite of ~2400 tests, so a
stray ``sys.path`` entry or ``sys.modules`` entry would surface as an unrelated failure far
from here. Install/uninstall is asserted to restore both exactly, including under double
and nested use.
"""

import ast
import contextlib
import importlib
import importlib.metadata
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

from tests._provider_plugin_fixture import (
    ACQUISITION_ENTRY_POINT_GROUP,
    ACQUISITION_PROVIDER_ID,
    BASE_DISTRIBUTION_NAME,
    BASE_DISTRIBUTION_VERSION,
    BASE_MODULE_NAME,
    DISCOVERY_ENTRY_POINT_GROUP,
    DISCOVERY_PROVIDER_ID,
    FIXTURE_ROOT,
    VARIANT_NAMES,
    VARIANT_ROOTS,
    install_provider_plugins,
    installed_provider_plugins,
    uninstall_provider_plugins,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"

FIXTURE_DISTRIBUTION_NAMES = frozenset(
    {
        "keepa-fixture",
        "keepa-rival-fixture",
        "keepa-reserved-fixture",
        "keepa-broken-fixture",
        "keepa-exploding-fixture",
    }
)

LICENSE_INFERENCE_VALUES = frozenset({"yes", "partial", "none"})
RATE_LIMIT_WINDOWS = frozenset({"minute", "hour"})

SAMPLE_ASIN = "B0ABC12345"
SAMPLE_PRODUCT = {"title": "Synthetic widget", "currency": "EUR", "asin": SAMPLE_ASIN}
SAMPLE_HISTORY = "date,price\n2026-08-01,21.40\n2026-08-02,23.99\n"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REGISTRY = load_script_module("provider_plugin_fixture_registry", "_provider_registry.py")
DISCOVER = load_script_module("provider_plugin_fixture_discover", "discover_sources.py")

RESERVED_PROVIDER_IDS = frozenset(
    {
        *REGISTRY.DISCOVERY_PROVIDER_IDS,
        *REGISTRY.ACQUISITION_PROVIDER_IDS,
        *REGISTRY.LEGACY_DISCOVERY_STRATEGY_IDS,
    }
)


class _BlockedRootFinder:
    """A meta-path finder that refuses one top-level package and defers on everything else."""

    def __init__(self, blocked_root: str) -> None:
        self.blocked_root = blocked_root

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.blocked_root or fullname.startswith(f"{self.blocked_root}."):
            raise ImportError(f"{fullname} is blocked for this test")
        return None


def fixture_entry_points(group: str) -> dict[str, list[importlib.metadata.EntryPoint]]:
    """Group the visible entry points of ``group`` by name, fixture distributions only."""

    grouped: dict[str, list[importlib.metadata.EntryPoint]] = {}
    for entry_point in importlib.metadata.entry_points(group=group):
        distribution = getattr(entry_point, "dist", None)
        name = getattr(distribution, "name", None)
        if name in FIXTURE_DISTRIBUTION_NAMES:
            grouped.setdefault(entry_point.name, []).append(entry_point)
    return grouped


class ProviderPluginFixtureDiscoveryTests(unittest.TestCase):
    """Real entry-point discovery over the shipped .dist-info directories."""

    def test_both_entry_point_groups_expose_the_fixture_providers_only_while_installed(self):
        for group in (ACQUISITION_ENTRY_POINT_GROUP, DISCOVERY_ENTRY_POINT_GROUP):
            with self.subTest(group=group, phase="before install"):
                self.assertEqual({}, fixture_entry_points(group))

        with installed_provider_plugins():
            acquisition = fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)
            discovery = fixture_entry_points(DISCOVERY_ENTRY_POINT_GROUP)
            self.assertEqual([ACQUISITION_PROVIDER_ID], sorted(acquisition))
            self.assertEqual([DISCOVERY_PROVIDER_ID], sorted(discovery))

        for group in (ACQUISITION_ENTRY_POINT_GROUP, DISCOVERY_ENTRY_POINT_GROUP):
            with self.subTest(group=group, phase="after uninstall"):
                self.assertEqual({}, fixture_entry_points(group))

    def test_the_entry_point_carries_the_distribution_name_and_version_provenance_records(self):
        with installed_provider_plugins():
            (entry_point,) = fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)[ACQUISITION_PROVIDER_ID]
            self.assertEqual(BASE_DISTRIBUTION_NAME, entry_point.dist.name)
            self.assertEqual(BASE_DISTRIBUTION_VERSION, entry_point.dist.version)
            self.assertEqual(f"{BASE_MODULE_NAME}:KeepaFixtureAcquisitionProvider", entry_point.value)

    def test_the_entry_point_name_matches_the_declared_provider_id_in_both_groups(self):
        with installed_provider_plugins():
            for group, expected_id in (
                (ACQUISITION_ENTRY_POINT_GROUP, ACQUISITION_PROVIDER_ID),
                (DISCOVERY_ENTRY_POINT_GROUP, DISCOVERY_PROVIDER_ID),
            ):
                with self.subTest(group=group):
                    (entry_point,) = fixture_entry_points(group)[expected_id]
                    provider = entry_point.load()()
                    self.assertEqual(entry_point.name, provider.id)

    def test_neither_fixture_id_collides_with_a_reserved_built_in_provider_id(self):
        for provider_id in (ACQUISITION_PROVIDER_ID, DISCOVERY_PROVIDER_ID):
            with self.subTest(provider_id=provider_id):
                self.assertNotIn(provider_id, RESERVED_PROVIDER_IDS)
                self.assertFalse(provider_id.startswith("standards:"))


class ProviderPluginFixtureContractTests(unittest.TestCase):
    """The v1 contract attributes, asserted on the objects the entry points load."""

    def assert_capabilities(self, capabilities, *, expect_credentials: bool) -> None:
        self.assertIsInstance(capabilities.allowed_domains, tuple)
        self.assertTrue(capabilities.allowed_domains)
        for domain in capabilities.allowed_domains:
            with self.subTest(domain=domain):
                self.assertIsInstance(domain, str)
                self.assertEqual(domain, domain.lower())
                self.assertNotIn("://", domain)
                self.assertNotIn("/", domain)

        self.assertIsInstance(capabilities.terms_urls, tuple)
        self.assertTrue(capabilities.terms_urls)
        for terms_url in capabilities.terms_urls:
            with self.subTest(terms_url=terms_url):
                self.assertTrue(terms_url == "per-origin" or terms_url.startswith("https://"))

        self.assertIn(capabilities.license_inference, LICENSE_INFERENCE_VALUES)
        self.assertIs(True, capabilities.captures_raw)
        self.assertIs(True, capabilities.quarantine_on_incomplete)

        self.assertIsNotNone(capabilities.rate_limit)
        self.assertIsInstance(capabilities.rate_limit.requests, int)
        self.assertGreater(capabilities.rate_limit.requests, 0)
        self.assertIn(capabilities.rate_limit.per, RATE_LIMIT_WINDOWS)

        self.assertIsInstance(capabilities.credentials, tuple)
        self.assertEqual(expect_credentials, bool(capabilities.credentials))
        for name in capabilities.credentials:
            with self.subTest(credential=name):
                self.assertRegex(name, r"^[A-Z][A-Z0-9_]*$")

        self.assertIsInstance(capabilities.request_kinds, tuple)
        for kind in capabilities.request_kinds:
            with self.subTest(kind=kind):
                self.assertIsInstance(kind, str)
                self.assertTrue(kind.strip())

    def test_every_loaded_provider_declares_the_v1_capability_shape(self):
        with installed_provider_plugins():
            for group, methods in (
                (ACQUISITION_ENTRY_POINT_GROUP, ("validate_request", "plan_fetch", "interpret")),
                (DISCOVERY_ENTRY_POINT_GROUP, ("validate_request", "plan_search", "interpret_candidates")),
            ):
                for name, entry_points in sorted(fixture_entry_points(group).items()):
                    with self.subTest(group=group, entry_point=name):
                        (entry_point,) = entry_points
                        provider = entry_point.load()()
                        self.assertRegex(provider.id, r"^[a-z0-9][a-z0-9._-]*$")
                        self.assertEqual(1, provider.provider_api_version)
                        self.assert_capabilities(provider.capabilities, expect_credentials=True)
                        for method in methods:
                            self.assertTrue(callable(getattr(provider, method)))

    def test_the_acquisition_provider_declares_two_domains_and_one_credential(self):
        with installed_provider_plugins():
            provider = self.load_acquisition_provider()
            self.assertEqual(2, len(provider.capabilities.allowed_domains))
            self.assertEqual(1, len(provider.capabilities.credentials))
            self.assertEqual(("KEEPA_FIXTURE_API_KEY",), provider.capabilities.credentials)

    def load_acquisition_provider(self):
        (entry_point,) = fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)[ACQUISITION_PROVIDER_ID]
        return entry_point.load()()

    def load_discovery_provider(self):
        (entry_point,) = fixture_entry_points(DISCOVERY_ENTRY_POINT_GROUP)[DISCOVERY_PROVIDER_ID]
        return entry_point.load()()

    def test_the_acquisition_plan_is_two_https_requests_inside_the_declared_domains(self):
        with installed_provider_plugins():
            provider = self.load_acquisition_provider()
            plan = provider.plan_fetch({"asin": SAMPLE_ASIN, "history_days": 30})

            self.assertIsInstance(plan, tuple)
            self.assertEqual(2, len(plan))
            hosts = []
            for planned in plan:
                with self.subTest(url=planned.url):
                    self.assertTrue(planned.url.startswith("https://"))
                    self.assertIn(planned.method, {"GET", "POST"})
                    self.assertIsNone(planned.body)
                    self.assertIsInstance(planned.timeout_hint, float)
                    hosts.append(planned.url.split("/")[2])
            self.assertEqual(sorted(provider.capabilities.allowed_domains), sorted(hosts))

    def test_the_plan_references_its_credential_by_placeholder_and_never_by_value(self):
        with installed_provider_plugins():
            provider = self.load_acquisition_provider()
            plan = provider.plan_fetch({"asin": SAMPLE_ASIN})

            placeholders = [
                value
                for planned in plan
                for _, value in planned.headers
                if "{{credential:" in value
            ]
            self.assertEqual(["{{credential:KEEPA_FIXTURE_API_KEY}}"], placeholders)
            for planned in plan:
                with self.subTest(url=planned.url):
                    # A credential in a URL would defeat URL redaction, so the plan must
                    # never place one there.
                    self.assertNotIn("{{credential:", planned.url)

    def test_a_malformed_acquisition_request_is_refused_with_a_reason_naming_the_field(self):
        cases = {
            "not an object": ["asin"],
            "missing asin": {},
            "lower-case asin": {"asin": "b0abc12345"},
            "short asin": {"asin": "B0ABC"},
            "history_days out of range": {"asin": SAMPLE_ASIN, "history_days": 4000},
            "history_days as bool": {"asin": SAMPLE_ASIN, "history_days": True},
            "unknown field": {"asin": SAMPLE_ASIN, "region": "eu"},
        }
        with installed_provider_plugins():
            provider = self.load_acquisition_provider()
            for label, request in cases.items():
                with self.subTest(case=label):
                    with self.assertRaises(ValueError) as caught:
                        provider.validate_request(request)
                    self.assertIn(ACQUISITION_PROVIDER_ID, str(caught.exception))

    def test_interpret_folds_both_responses_into_one_deterministic_artifact(self):
        responses = (
            json.dumps(SAMPLE_PRODUCT).encode("utf-8"),
            SAMPLE_HISTORY.encode("utf-8"),
        )
        with installed_provider_plugins():
            provider = self.load_acquisition_provider()
            request = {"asin": SAMPLE_ASIN, "history_days": 30}

            first = provider.interpret(request, responses)
            second = provider.interpret(request, responses)

            self.assertEqual(first.content, second.content)
            self.assertEqual(f"keepa-fixture-{SAMPLE_ASIN.lower()}.json", first.filename)
            self.assertNotIn("/", first.filename)
            self.assertNotIn("\\", first.filename)
            self.assertEqual("dataset", first.source_type)
            self.assertIsInstance(first.content, bytes)
            self.assertEqual((), first.warnings)

            document = json.loads(first.content.decode("utf-8"))
            self.assertEqual(SAMPLE_ASIN, document["asin"])
            self.assertEqual(30, document["history_days"])
            self.assertEqual("Synthetic widget", document["title"])
            self.assertEqual(["2026-08-01,21.40", "2026-08-02,23.99"], document["price_history_rows"])

            # Derived from the responses, not invented: changing a byte changes the digest.
            mutated = (responses[0], SAMPLE_HISTORY.replace("23.99", "24.99").encode("utf-8"))
            self.assertNotEqual(first.content, provider.interpret(request, mutated).content)

            self.assertIsInstance(json.dumps(dict(first.provenance_metadata)), str)
            self.assertEqual(SAMPLE_ASIN, first.provenance_metadata["asin"])
            self.assertEqual(2, first.provenance_metadata["observation_count"])

    def test_interpret_warns_rather_than_inventing_a_title_it_was_not_given(self):
        responses = (json.dumps({"currency": "EUR"}).encode("utf-8"), b"date,price\n")
        with installed_provider_plugins():
            artifact = self.load_acquisition_provider().interpret({"asin": SAMPLE_ASIN}, responses)

            self.assertIsNone(json.loads(artifact.content.decode("utf-8"))["title"])
            self.assertEqual(2, len(artifact.warnings))
            for warning in artifact.warnings:
                with self.subTest(warning=warning):
                    self.assertTrue(warning.startswith(f"{ACQUISITION_PROVIDER_ID}:"))

    def test_interpret_refuses_a_response_count_that_does_not_match_its_own_plan(self):
        with installed_provider_plugins():
            provider = self.load_acquisition_provider()
            with self.assertRaises(ValueError):
                provider.interpret({"asin": SAMPLE_ASIN}, (b"{}",))

    def test_the_discovery_plan_is_one_request_inside_the_declared_domain(self):
        with installed_provider_plugins():
            provider = self.load_discovery_provider()
            plan = provider.plan_search({"query": "synthetic widget", "max_results": 3})

            self.assertEqual(1, len(plan))
            self.assertTrue(plan[0].url.startswith(f"https://{provider.capabilities.allowed_domains[0]}/"))

    def test_a_malformed_discovery_request_is_refused_with_a_reason_naming_the_field(self):
        cases = {
            "missing query": {},
            "blank query": {"query": "   "},
            "max_results out of range": {"query": "widget", "max_results": 500},
            "unknown field": {"query": "widget", "cursor": "abc"},
        }
        with installed_provider_plugins():
            provider = self.load_discovery_provider()
            for label, request in cases.items():
                with self.subTest(case=label):
                    with self.assertRaises(ValueError) as caught:
                        provider.validate_request(request)
                    self.assertIn(DISCOVERY_PROVIDER_ID, str(caught.exception))


class ProviderPluginFixtureCandidateHygieneTests(unittest.TestCase):
    """Discovery candidates must survive the existing search-candidate pipeline unchanged."""

    SEARCH_PAYLOAD = {
        "results": [
            {"asin": "B0ABC12345", "title": "Synthetic widget", "snippet": "A widget.", "first_seen": "2026-07-01"},
            {"asin": "B0DEF67890", "title": "Synthetic widget pro"},
            {"asin": "not-an-asin", "title": "Dropped by the provider"},
        ]
    }

    def candidates(self, *, max_results: int = 5):
        with installed_provider_plugins():
            (entry_point,) = fixture_entry_points(DISCOVERY_ENTRY_POINT_GROUP)[DISCOVERY_PROVIDER_ID]
            provider = entry_point.load()()
            request = {"query": "synthetic widget", "max_results": max_results}
            responses = (json.dumps(self.SEARCH_PAYLOAD).encode("utf-8"),)
            return provider.interpret_candidates(request, responses)

    def test_interpret_candidates_drops_records_it_cannot_identify(self):
        records = self.candidates()

        self.assertEqual(2, len(records))
        for record in records:
            with self.subTest(url=record["url"]):
                self.assertIsNotNone(DISCOVER.http_url(record["url"]))
                self.assertTrue(record["title"])
                self.assertIn(record["trust_tier"], DISCOVER.TIER_RANK)

    def test_interpret_candidates_honours_the_requested_result_cap(self):
        self.assertEqual(1, len(self.candidates(max_results=1)))

    def test_the_records_feed_straight_into_the_existing_search_candidate_pipeline(self):
        records = DISCOVER.coerce_search_results(list(self.candidates()))
        self.assertEqual(2, len(records))

        request = {
            "query": "synthetic widget",
            "domain_allowlist": [],
            "domain_blocklist": [],
            "max_results": 10,
            "jurisdiction": None,
        }
        candidates = DISCOVER.normalize_search_candidates(
            records,
            request=request,
            request_id=None,
            discovery_id="disc-provider-plugin-fixture",
            network_io=True,
            discovered_at="2026-08-09T12:00:00Z",
            provider=DISCOVERY_PROVIDER_ID,
            discovered_by="discover_sources.py/registered",
        )

        self.assertEqual(2, len(candidates))
        for candidate in candidates:
            with self.subTest(candidate_id=candidate["candidate_id"]):
                self.assertEqual(DISCOVERY_PROVIDER_ID, candidate["provider"])
                self.assertIn(candidate["trust_tier"], DISCOVER.TIER_RANK)
                self.assertIn(candidate["recommended_action"], {"fetch", "review", "reject"})
                self.assertNotEqual("unsafe_or_unusable", candidate["trust_tier"])
                self.assertTrue(candidate["reasoning"]["authority_reason"])


class ProviderPluginFixtureIndependenceTests(unittest.TestCase):
    """The plugin must be authorable without the package it plugs into."""

    def test_no_shipped_fixture_module_imports_the_package_it_plugs_into(self):
        sources = sorted(FIXTURE_ROOT.rglob("*.py"))
        self.assertTrue(sources)
        for path in sources:
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                imported: list[str] = []
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.Import):
                        imported.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.append(node.module)
                roots = {name.split(".", 1)[0] for name in imported}
                self.assertNotIn("evidence_wiki", roots)

    def test_the_providers_load_with_the_package_blocked_at_the_meta_path(self):
        blocker = _BlockedRootFinder("evidence_wiki")
        saved_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "evidence_wiki" or name.startswith("evidence_wiki.")
        }
        for name in saved_modules:
            del sys.modules[name]
        sys.meta_path.insert(0, blocker)
        try:
            with self.assertRaises(ImportError):
                importlib.import_module("evidence_wiki")
            with installed_provider_plugins():
                module = importlib.import_module(BASE_MODULE_NAME)
                self.assertEqual(1, module.PROVIDER_API_VERSION)
                for group in (ACQUISITION_ENTRY_POINT_GROUP, DISCOVERY_ENTRY_POINT_GROUP):
                    with self.subTest(group=group):
                        for entry_points in fixture_entry_points(group).values():
                            for entry_point in entry_points:
                                provider = entry_point.load()()
                                self.assertTrue(provider.capabilities.allowed_domains)
        finally:
            sys.meta_path.remove(blocker)
            sys.modules.update(saved_modules)


class ProviderPluginFixtureIsolationTests(unittest.TestCase):
    """Install/uninstall must leave the interpreter exactly as it found it."""

    def exercise_every_distribution(self) -> None:
        importlib.import_module(BASE_MODULE_NAME)
        for group in (ACQUISITION_ENTRY_POINT_GROUP, DISCOVERY_ENTRY_POINT_GROUP):
            for entry_points in fixture_entry_points(group).values():
                for entry_point in entry_points:
                    with contextlib.suppress(Exception):
                        entry_point.load()()

    def test_install_and_uninstall_restore_sys_path_and_sys_modules_exactly(self):
        # Warm first: the snapshot below must measure leakage, not the first import of a
        # stdlib module the fixtures happen to pull in.
        with installed_provider_plugins(*VARIANT_NAMES):
            self.exercise_every_distribution()

        expected_path = list(sys.path)
        expected_modules = dict(sys.modules)

        with installed_provider_plugins(*VARIANT_NAMES):
            self.exercise_every_distribution()
            self.assertIn(str(FIXTURE_ROOT), sys.path)
            self.assertIn(BASE_MODULE_NAME, sys.modules)

        self.assertEqual(expected_path, sys.path)
        self.assertEqual(expected_modules, sys.modules)

    def test_a_nested_install_of_the_same_distribution_stays_balanced(self):
        expected_path = list(sys.path)

        with installed_provider_plugins() as outer:
            self.assertEqual((str(FIXTURE_ROOT),), outer.added_paths)
            with installed_provider_plugins() as inner:
                # The inner install found the root already present, so it owns nothing and
                # its exit must not pull the path out from under the outer block.
                self.assertEqual((), inner.added_paths)
                self.assertEqual(1, sys.path.count(str(FIXTURE_ROOT)))
            self.assertIn(str(FIXTURE_ROOT), sys.path)
            self.assertEqual([ACQUISITION_PROVIDER_ID], sorted(fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)))

        self.assertEqual(expected_path, sys.path)
        self.assertEqual({}, fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP))

    def test_uninstalling_the_same_handle_twice_is_a_no_op(self):
        expected_path = list(sys.path)

        handle = install_provider_plugins()
        uninstall_provider_plugins(handle)
        uninstall_provider_plugins(handle)

        self.assertEqual(expected_path, sys.path)

    def test_an_unknown_variant_name_is_refused_before_anything_is_installed(self):
        expected_path = list(sys.path)

        with self.assertRaises(ValueError) as caught:
            install_provider_plugins("no-such-variant")

        self.assertIn("no-such-variant", str(caught.exception))
        for name in VARIANT_NAMES:
            with self.subTest(variant=name):
                self.assertIn(name, str(caught.exception))
        self.assertEqual(expected_path, sys.path)

    def test_the_helper_clears_an_already_imported_loader_cache(self):
        calls: list[str] = []
        stub = types.ModuleType("provider_plugin_fixture_loader_stub")
        stub.__file__ = str(SCRIPTS / "_provider_plugins.py")
        stub.clear_cache = lambda: calls.append("cleared")
        sys.modules[stub.__name__] = stub
        try:
            with installed_provider_plugins():
                self.assertEqual(["cleared"], calls)
            self.assertEqual(["cleared", "cleared"], calls)
        finally:
            del sys.modules[stub.__name__]

    def test_a_loader_without_the_cache_seam_does_not_break_installation(self):
        stub = types.ModuleType("provider_plugin_fixture_seamless_loader_stub")
        stub.__file__ = str(SCRIPTS / "_provider_plugins.py")
        sys.modules[stub.__name__] = stub
        try:
            with installed_provider_plugins():
                self.assertEqual([ACQUISITION_PROVIDER_ID], sorted(fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)))
        finally:
            del sys.modules[stub.__name__]


class ProviderPluginFixtureVariantTests(unittest.TestCase):
    """The variants exist so the loader's fail-closed rules have something to refuse."""

    def test_every_declared_variant_ships_a_dist_info_directory(self):
        for name, root in sorted(VARIANT_ROOTS.items()):
            with self.subTest(variant=name):
                dist_infos = sorted(root.glob("*.dist-info"))
                self.assertEqual(1, len(dist_infos))
                self.assertTrue((dist_infos[0] / "METADATA").is_file())
                self.assertTrue((dist_infos[0] / "entry_points.txt").is_file())

    def test_the_duplicate_id_variant_registers_a_second_claim_on_the_same_id(self):
        with installed_provider_plugins("duplicate-id"):
            claims = fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)[ACQUISITION_PROVIDER_ID]

            self.assertEqual(2, len(claims))
            self.assertEqual(
                {BASE_DISTRIBUTION_NAME, "keepa-rival-fixture"},
                {claim.dist.name for claim in claims},
            )
            for claim in claims:
                with self.subTest(distribution=claim.dist.name):
                    self.assertEqual(ACQUISITION_PROVIDER_ID, claim.load()().id)

    def test_the_reserved_id_variant_claims_a_built_in_acquisition_id(self):
        with installed_provider_plugins("reserved-id", base=False):
            (entry_point,) = fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)["web"]
            provider = entry_point.load()()

            self.assertEqual("web", provider.id)
            self.assertIn(provider.id, REGISTRY.ACQUISITION_PROVIDER_IDS)

    def test_the_invalid_declaration_variant_breaks_every_v1_value_rule_at_once(self):
        with installed_provider_plugins("invalid-declaration", base=False):
            (entry_point,) = fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)["keepa-broken-fixture"]
            provider = entry_point.load()()
            capabilities = provider.capabilities

            self.assertNotRegex(provider.id, r"^[a-z0-9][a-z0-9._-]*$")
            self.assertNotEqual(1, provider.provider_api_version)
            self.assertEqual((), capabilities.allowed_domains)
            self.assertEqual((), capabilities.terms_urls)
            self.assertNotIn(capabilities.license_inference, LICENSE_INFERENCE_VALUES)
            self.assertIs(False, capabilities.captures_raw)
            self.assertIs(False, capabilities.quarantine_on_incomplete)
            self.assertNotIn(capabilities.rate_limit.per, RATE_LIMIT_WINDOWS)
            for name in capabilities.credentials:
                with self.subTest(credential=name):
                    self.assertNotRegex(name, r"^[A-Z][A-Z0-9_]*$")

    def test_the_import_error_variant_is_discoverable_but_fails_on_load(self):
        with installed_provider_plugins("import-error", base=False):
            (entry_point,) = fixture_entry_points(ACQUISITION_ENTRY_POINT_GROUP)["keepa-exploding-fixture"]

            with self.assertRaises(RuntimeError) as caught:
                entry_point.load()

            self.assertIn("synthetic import failure", str(caught.exception))
        self.assertNotIn("keepa_exploding_fixture", sys.modules)


if __name__ == "__main__":
    unittest.main()
