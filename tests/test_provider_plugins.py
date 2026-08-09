"""Unit tests for entry-point provider registration (CR-5 registry loader).

Registration is packaging metadata, so these tests build **real** path-based
distributions — a temp directory holding ``<name>-<version>.dist-info`` next to the
provider module, appended to ``sys.path`` — and let ``importlib.metadata`` discover
them. Patching the enumeration seam everywhere would prove the validator and nothing
about the discovery path an operator's ``pip install`` actually exercises.

The fixture providers never import ``evidence_wiki``. That is the premise the loader
rests on: validation is structural, so a provider authored where the package is not
installed is valid when it matches the shape, and a provider that inherits the
package's own base classes is still invalid when it breaks a value rule.

Every test restores ``sys.path``, ``sys.modules``, the import caches, and the loader's
process cache, because this file runs inside a suite that would otherwise inherit a
half-installed fixture distribution.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import re
import sys
import tempfile
import unittest
from collections import namedtuple
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


PLUGINS = load_script_module("provider_plugins_under_test", "_provider_plugins.py")
REGISTRY = load_script_module("provider_registry_under_test", "_provider_registry.py")

ACQUISITION_GROUP = PLUGINS.ENTRY_POINT_GROUPS["acquisition"]
DISCOVERY_GROUP = PLUGINS.ENTRY_POINT_GROUPS["discovery"]

ALL_PROVIDER_METHODS = ("validate_request", "plan_fetch", "interpret", "plan_search", "interpret_candidates")

DEFAULT_CAPABILITIES = {
    "allowed_domains": '("api.keepa.com", "data.keepa.com")',
    "terms_urls": '("https://keepa.com/terms",)',
    "license_inference": '"partial"',
    "captures_raw": "True",
    "quarantine_on_incomplete": "True",
    "rate_limit": 'RateLimit(60, "minute")',
    "credentials": '("KEEPA_API_KEY",)',
    "request_kinds": '("pack:market-data/price_history",)',
}

PROVIDER_PREAMBLE = '''"""Fixture provider that imports nothing at all: no package, no stdlib."""


class RateLimit:
    def __init__(self, requests, per):
        self.requests = requests
        self.per = per


class Capabilities:
    def __init__(self, **fields):
        for name, value in fields.items():
            setattr(self, name, value)
'''

# A declaration is plugin code, so reading one can raise. The loader must survive it.
EXPLODING_DECLARATION_SOURCE = '''"""Fixture whose declaration raises while it is read."""


class FixtureProvider:
    id = "keepa"
    provider_api_version = 1

    @property
    def capabilities(self):
        raise RuntimeError("declaration exploded")

    def validate_request(self, *args, **kwargs):
        return ()

    def plan_fetch(self, *args, **kwargs):
        return ()

    def interpret(self, *args, **kwargs):
        return ()
'''

InstalledFixture = namedtuple("InstalledFixture", "root module_name distribution")

_MODULE_SEQUENCE = [0]


def unique_suffix() -> str:
    _MODULE_SEQUENCE[0] += 1
    return str(_MODULE_SEQUENCE[0])


def capabilities_expression(overrides: dict[str, str] | None = None, *, drop: tuple[str, ...] = ()) -> str:
    fields = dict(DEFAULT_CAPABILITIES)
    fields.update(overrides or {})
    for name in drop:
        fields.pop(name, None)
    arguments = ", ".join(f"{name}={value}" for name, value in fields.items())
    return f"Capabilities({arguments})"


def provider_source(
    *,
    provider_id: str = '"keepa"',
    api_version: str = "1",
    capabilities: str | None = None,
    methods: tuple[str, ...] = ALL_PROVIDER_METHODS,
    non_callable: tuple[str, ...] = (),
    constructor: str = "",
    module_footer: str = "",
) -> str:
    """Render a fixture provider module from literal source fragments.

    Fragments rather than values, so a test can declare a capability field as any
    Python expression at all — ``None``, a bare string, an object carrying the wrong
    attributes — which is exactly the space a duck-typed loader has to survive.
    """
    lines = [PROVIDER_PREAMBLE, "", "class FixtureProvider:"]
    lines.append(f"    id = {provider_id}")
    lines.append(f"    provider_api_version = {api_version}")
    lines.append(f"    capabilities = {capabilities if capabilities is not None else capabilities_expression()}")
    for name in non_callable:
        lines.append(f'    {name} = "not callable"')
    if constructor:
        lines.append(constructor)
    for name in methods:
        if name in non_callable:
            continue
        lines.append(f"    def {name}(self, *args, **kwargs):")
        lines.append("        return ()")
    lines.append("")
    lines.append(module_footer)
    return "\n".join(lines)


class FixtureDistributionCase(unittest.TestCase):
    """Base case that installs path-based distributions and removes every trace."""

    def setUp(self):
        self.installed: list[InstalledFixture] = []
        PLUGINS.clear_cache()

    def tearDown(self):
        for fixture in list(self.installed):
            self.uninstall(fixture)
        importlib.invalidate_caches()
        PLUGINS.clear_cache()

    def install_distribution(
        self,
        *,
        source: str,
        group: str = ACQUISITION_GROUP,
        entry_point_name: str = "keepa",
        attribute: str = "FixtureProvider",
        dist_name: str = "keepa-fixture",
        version: str = "0.1.0",
        clear_cache: bool = True,
    ) -> InstalledFixture:
        """Write a real .dist-info tree onto sys.path so importlib.metadata finds it."""
        temporary = tempfile.TemporaryDirectory(prefix="evidence-wiki-provider-fixture-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)

        module_name = f"{dist_name.replace('-', '_')}_{unique_suffix()}"
        (root / f"{module_name}.py").write_text(source, encoding="utf-8")

        dist_info = root / f"{dist_name.replace('-', '_')}-{version}.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: {version}\n",
            encoding="utf-8",
        )
        (dist_info / "entry_points.txt").write_text(
            f"[{group}]\n{entry_point_name} = {module_name}:{attribute}\n",
            encoding="utf-8",
        )

        sys.path.append(str(root))
        # A .dist-info written after interpreter start stays invisible until the path
        # finders drop their caches; the loader does the same before it enumerates.
        importlib.invalidate_caches()
        if clear_cache:
            PLUGINS.clear_cache()
        fixture = InstalledFixture(root=str(root), module_name=module_name, distribution=dist_name)
        self.installed.append(fixture)
        return fixture

    def uninstall(self, fixture: InstalledFixture) -> None:
        while fixture.root in sys.path:
            sys.path.remove(fixture.root)
        sys.modules.pop(fixture.module_name, None)
        if fixture in self.installed:
            self.installed.remove(fixture)
        importlib.invalidate_caches()
        PLUGINS.clear_cache()

    @contextlib.contextmanager
    def temporarily_installed(self, **kwargs):
        fixture = self.install_distribution(**kwargs)
        try:
            yield fixture
        finally:
            self.uninstall(fixture)

    def only_invalid(self, phase: str = "acquisition"):
        valid, invalid = PLUGINS.load_registrations(phase)
        self.assertEqual({}, valid)
        self.assertEqual(1, len(invalid), f"expected exactly one invalid registration, got {invalid}")
        return invalid[0]


class BackwardCompatibilityTests(FixtureDistributionCase):
    """With nothing installed, the built-in lists must remain the whole universe."""

    def test_no_installed_distribution_registers_no_provider_in_either_phase(self):
        for phase in ("acquisition", "discovery"):
            with self.subTest(phase=phase):
                valid, invalid = PLUGINS.load_registrations(phase)
                self.assertEqual({}, valid)
                self.assertEqual((), invalid)
                self.assertEqual((), PLUGINS.registered_ids(phase))

    def test_registration_report_is_empty_but_still_names_both_entry_point_groups(self):
        report = PLUGINS.registration_report()

        self.assertEqual(["acquisition", "discovery"], sorted(report))
        self.assertEqual(ACQUISITION_GROUP, report["acquisition"]["entry_point_group"])
        self.assertEqual(DISCOVERY_GROUP, report["discovery"]["entry_point_group"])
        for phase in ("acquisition", "discovery"):
            self.assertEqual([], report[phase]["registered"])
            self.assertEqual([], report[phase]["invalid"])


class ValidRegistrationTests(FixtureDistributionCase):
    def test_acquisition_provider_loads_with_its_packaging_identity_and_declaration(self):
        self.install_distribution(source=provider_source())

        valid, invalid = PLUGINS.load_registrations("acquisition")

        self.assertEqual((), invalid)
        self.assertEqual(["keepa"], list(valid))
        registration = valid["keepa"]
        self.assertEqual("keepa", registration.provider_id)
        self.assertEqual("acquisition", registration.phase)
        self.assertEqual("keepa-fixture", registration.distribution)
        self.assertEqual("0.1.0", registration.version)
        self.assertEqual("keepa", registration.entry_point)
        self.assertEqual(1, registration.provider_api_version)
        self.assertEqual(("api.keepa.com", "data.keepa.com"), registration.capabilities.allowed_domains)
        self.assertEqual({"requests": 60, "per": "minute"}, registration.capabilities.rate_limit)
        self.assertEqual(("KEEPA_API_KEY",), registration.capabilities.credentials)
        self.assertEqual(("keepa",), PLUGINS.registered_ids("acquisition"))

    def test_discovery_provider_loads_from_its_own_group_and_does_not_leak_across_phases(self):
        self.install_distribution(source=provider_source(), group=DISCOVERY_GROUP)

        self.assertEqual(("keepa",), PLUGINS.registered_ids("discovery"))
        self.assertEqual((), PLUGINS.registered_ids("acquisition"))

    def test_an_entry_point_naming_a_ready_made_instance_is_accepted(self):
        self.install_distribution(
            source=provider_source(module_footer="\nINSTANCE = FixtureProvider()\n"),
            attribute="INSTANCE",
        )

        valid, invalid = PLUGINS.load_registrations("acquisition")

        self.assertEqual((), invalid)
        self.assertEqual(["keepa"], list(valid))
        self.assertNotIsInstance(valid["keepa"].provider, type)

    def test_provider_module_that_never_imports_evidence_wiki_is_accepted(self):
        source = provider_source()
        self.assertNotIn("evidence_wiki", source, "the duck-typing premise needs a package-free fixture")

        self.install_distribution(source=source)

        self.assertEqual(("keepa",), PLUGINS.registered_ids("acquisition"))

    def test_the_loader_itself_never_imports_the_package_it_serves(self):
        text = (SCRIPTS / "_provider_plugins.py").read_text(encoding="utf-8")

        imports = re.findall(r"^\s*(?:import|from)\s+(evidence_wiki[\w.]*)", text, flags=re.MULTILINE)

        self.assertEqual([], imports, "workspace scripts must run where only the scripts exist")

    def test_optional_capability_fields_fall_back_to_their_contract_defaults(self):
        self.install_distribution(
            source=provider_source(
                capabilities=capabilities_expression(drop=("rate_limit", "credentials", "request_kinds"))
            )
        )

        valid, invalid = PLUGINS.load_registrations("acquisition")

        self.assertEqual((), invalid)
        capabilities = valid["keepa"].capabilities
        self.assertIsNone(capabilities.rate_limit)
        self.assertEqual((), capabilities.credentials)
        self.assertEqual((), capabilities.request_kinds)
        self.assertTrue(capabilities.captures_raw)
        self.assertTrue(capabilities.quarantine_on_incomplete)

    def test_per_origin_terms_and_hourly_rate_limits_are_accepted(self):
        self.install_distribution(
            source=provider_source(
                capabilities=capabilities_expression(
                    {
                        "terms_urls": '("per-origin",)',
                        "rate_limit": 'RateLimit(5, "hour")',
                        "license_inference": '"none"',
                    }
                )
            )
        )

        valid, _invalid = PLUGINS.load_registrations("acquisition")

        capabilities = valid["keepa"].capabilities
        self.assertEqual(("per-origin",), capabilities.terms_urls)
        self.assertEqual({"requests": 5, "per": "hour"}, capabilities.rate_limit)


class RegistrationSerializationTests(FixtureDistributionCase):
    def test_registration_block_and_capability_summary_have_exact_key_sets(self):
        self.install_distribution(source=provider_source())
        valid, _invalid = PLUGINS.load_registrations("acquisition")
        registration = valid["keepa"]

        block = registration.registration_block()
        summary = registration.capabilities.as_dict()

        self.assertEqual(
            ["distribution", "entry_point", "id", "phase", "provider_api_version", "version"],
            sorted(block),
        )
        self.assertEqual(list(PLUGINS.REGISTRATION_BLOCK_FIELDS), list(block))
        self.assertEqual(list(PLUGINS.CAPABILITY_FIELDS), list(summary))
        self.assertEqual("keepa", block["id"])
        self.assertEqual("acquisition", block["phase"])
        self.assertEqual(["api.keepa.com", "data.keepa.com"], summary["allowed_domains"])
        self.assertEqual({"requests": 60, "per": "minute"}, summary["rate_limit"])
        self.assertEqual(["KEEPA_API_KEY"], summary["credentials"])

    def test_registration_report_carries_credential_names_and_every_invalid_reason(self):
        self.install_distribution(source=provider_source())
        self.install_distribution(
            source=provider_source(provider_id='"Keepa-Broken"'),
            dist_name="broken-fixture",
            entry_point_name="broken",
        )

        report = PLUGINS.registration_report()

        acquisition = report["acquisition"]
        self.assertEqual(["keepa"], [entry["id"] for entry in acquisition["registered"]])
        self.assertEqual(["KEEPA_API_KEY"], acquisition["registered"][0]["capabilities"]["credentials"])
        self.assertEqual(1, len(acquisition["invalid"]))
        invalid = acquisition["invalid"][0]
        self.assertEqual(["distribution", "entry_point", "id", "phase", "reason"], sorted(invalid))
        self.assertEqual("broken-fixture", invalid["distribution"])
        self.assertIn("Keepa-Broken", invalid["reason"])


class InvalidRegistrationTests(FixtureDistributionCase):
    def test_a_module_that_raises_on_import_becomes_an_invalid_registration(self):
        self.install_distribution(source='raise RuntimeError("fixture import exploded")\n')

        invalid = self.only_invalid()

        self.assertIsNone(invalid.provider_id)
        self.assertEqual("keepa-fixture", invalid.distribution)
        self.assertEqual("keepa", invalid.entry_point)
        self.assertIn("failed to load", invalid.reason)
        self.assertIn("fixture import exploded", invalid.reason)

    def test_an_entry_point_naming_a_missing_attribute_becomes_invalid(self):
        self.install_distribution(source=provider_source(), attribute="NoSuchProvider")

        invalid = self.only_invalid()

        self.assertIn("failed to load", invalid.reason)

    def test_a_provider_class_needing_constructor_arguments_is_invalid(self):
        self.install_distribution(
            source=provider_source(constructor="    def __init__(self, api_key):\n        self.api_key = api_key\n")
        )

        invalid = self.only_invalid()

        self.assertEqual("keepa", invalid.provider_id)
        self.assertIn("could not be instantiated", invalid.reason)

    def test_unsupported_and_malformed_provider_api_versions_are_refused(self):
        cases = (
            ("future version", "2", "not supported"),
            ("string version", '"1"', "integer provider_api_version"),
            ("boolean version", "True", "integer provider_api_version"),
        )
        for label, literal, expected in cases:
            with self.subTest(case=label), self.temporarily_installed(
                source=provider_source(api_version=literal)
            ):
                invalid = self.only_invalid()
                self.assertIn(expected, invalid.reason)

    def test_a_missing_required_method_is_refused_per_phase(self):
        cases = (
            ("acquisition", ACQUISITION_GROUP, "plan_fetch"),
            ("acquisition", ACQUISITION_GROUP, "interpret"),
            ("acquisition", ACQUISITION_GROUP, "validate_request"),
            ("discovery", DISCOVERY_GROUP, "plan_search"),
            ("discovery", DISCOVERY_GROUP, "interpret_candidates"),
        )
        for phase, group, missing in cases:
            methods = tuple(name for name in ALL_PROVIDER_METHODS if name != missing)
            with self.subTest(phase=phase, missing=missing), self.temporarily_installed(
                source=provider_source(methods=methods), group=group
            ):
                invalid = self.only_invalid(phase)
                self.assertIn(f"must define {missing}()", invalid.reason)

    def test_a_discovery_provider_needs_no_acquisition_methods(self):
        methods = ("validate_request", "plan_search", "interpret_candidates")
        self.install_distribution(source=provider_source(methods=methods), group=DISCOVERY_GROUP)

        valid, invalid = PLUGINS.load_registrations("discovery")

        self.assertEqual((), invalid)
        self.assertEqual(["keepa"], list(valid))

    def test_a_non_callable_method_attribute_is_refused(self):
        self.install_distribution(source=provider_source(non_callable=("plan_fetch",)))

        invalid = self.only_invalid()

        self.assertIn("plan_fetch must be callable", invalid.reason)

    def test_malformed_provider_ids_are_refused_with_the_pattern_named(self):
        cases = (
            ("uppercase", '"Keepa"'),
            ("leading hyphen", '"-keepa"'),
            ("namespace colon", '"vendor:keepa"'),
            ("surrounding whitespace", '" keepa"'),
            ("empty", '""'),
            ("non-string", "42"),
        )
        for label, literal in cases:
            with self.subTest(case=label), self.temporarily_installed(
                source=provider_source(provider_id=literal)
            ):
                invalid = self.only_invalid()
                self.assertTrue(
                    "must match" in invalid.reason or "non-empty string id" in invalid.reason,
                    invalid.reason,
                )

    def test_reserved_built_in_and_legacy_ids_cannot_be_shadowed(self):
        cases = (
            ("acquisition built-in", "acquisition", ACQUISITION_GROUP, "web"),
            ("discovery built-in", "discovery", DISCOVERY_GROUP, "search"),
            ("standards family", "discovery", DISCOVERY_GROUP, "standards:nist"),
            ("legacy strategy", "discovery", DISCOVERY_GROUP, "legal"),
        )
        for label, phase, group, reserved in cases:
            with self.subTest(case=label), self.temporarily_installed(
                source=provider_source(provider_id=f'"{reserved}"'), group=group
            ):
                invalid = self.only_invalid(phase)
                self.assertIn("is reserved", invalid.reason)
                self.assertIn(reserved, invalid.reason)


class CapabilityRuleTests(FixtureDistributionCase):
    """Every declared-capability value rule, each proved to refuse rather than warn."""

    def test_each_capability_violation_invalidates_the_registration(self):
        cases = (
            ("allowed_domains missing", {}, ("allowed_domains",), "allowed_domains must be a list or tuple"),
            ("allowed_domains empty", {"allowed_domains": "()"}, (), "at least one domain"),
            ("allowed_domains as string", {"allowed_domains": '"api.keepa.com"'}, (), "list or tuple"),
            ("allowed_domains with scheme", {"allowed_domains": '("https://api.keepa.com",)'}, (), "bare lowercase"),
            ("allowed_domains with path", {"allowed_domains": '("api.keepa.com/v1",)'}, (), "bare lowercase"),
            ("allowed_domains uppercase", {"allowed_domains": '("API.keepa.com",)'}, (), "bare lowercase"),
            ("allowed_domains with port", {"allowed_domains": '("api.keepa.com:443",)'}, (), "bare lowercase"),
            ("allowed_domains trailing dot", {"allowed_domains": '("api.keepa.com.",)'}, (), "bare lowercase"),
            ("allowed_domains duplicated", {"allowed_domains": '("a.com", "a.com")'}, (), "duplicate domain"),
            ("allowed_domains non-string", {"allowed_domains": "(1,)"}, (), "list or tuple"),
            ("terms_urls missing", {}, ("terms_urls",), "terms_urls must be a list or tuple"),
            ("terms_urls empty", {"terms_urls": "()"}, (), "at least one terms URL"),
            ("terms_urls http", {"terms_urls": '("http://keepa.com/terms",)'}, (), "https:// URLs"),
            ("terms_urls bare scheme", {"terms_urls": '("https://",)'}, (), "https:// URLs"),
            ("license_inference missing", {}, ("license_inference",), "license_inference must be one of"),
            ("license_inference unknown", {"license_inference": '"maybe"'}, (), "license_inference must be one of"),
            ("captures_raw false", {"captures_raw": "False"}, (), "captures_raw must be True"),
            (
                "quarantine_on_incomplete false",
                {"quarantine_on_incomplete": "False"},
                (),
                "quarantine_on_incomplete must be True",
            ),
            ("rate_limit zero", {"rate_limit": 'RateLimit(0, "minute")'}, (), "at least 1"),
            ("rate_limit non-integer", {"rate_limit": 'RateLimit("60", "minute")'}, (), "at least 1"),
            ("rate_limit boolean", {"rate_limit": 'RateLimit(True, "minute")'}, (), "at least 1"),
            ("rate_limit window", {"rate_limit": 'RateLimit(60, "day")'}, (), "rate_limit.per must be one of"),
            ("rate_limit shapeless", {"rate_limit": "object()"}, (), "rate_limit"),
            ("credentials lowercase", {"credentials": '("keepa_api_key",)'}, (), "environment variable names"),
            ("credentials with dash", {"credentials": '("KEEPA-API-KEY",)'}, (), "environment variable names"),
            ("credentials leading digit", {"credentials": '("1KEEPA",)'}, (), "environment variable names"),
            ("credentials as string", {"credentials": '"KEEPA_API_KEY"'}, (), "list or tuple"),
            ("request_kinds blank", {"request_kinds": '("",)'}, (), "request_kinds entries"),
            ("request_kinds padded", {"request_kinds": '(" paper",)'}, (), "request_kinds entries"),
            ("request_kinds non-string", {"request_kinds": "(7,)"}, (), "list or tuple"),
        )
        for label, overrides, drop, expected in cases:
            with self.subTest(case=label), self.temporarily_installed(
                source=provider_source(capabilities=capabilities_expression(overrides, drop=drop))
            ):
                invalid = self.only_invalid()
                self.assertIn(expected, invalid.reason)

    def test_a_provider_without_any_capability_declaration_is_refused(self):
        self.install_distribution(source=provider_source(capabilities="None"))

        invalid = self.only_invalid()

        self.assertIn("must declare capabilities", invalid.reason)

    def test_a_capability_mapping_is_named_as_the_wrong_shape_rather_than_eight_gaps(self):
        self.install_distribution(source=provider_source(capabilities='{"allowed_domains": ("a.com",)}'))

        invalid = self.only_invalid()

        self.assertIn("not a mapping", invalid.reason)

    def test_a_declaration_that_raises_while_it_is_read_becomes_an_invalid_registration(self):
        self.install_distribution(source=EXPLODING_DECLARATION_SOURCE)

        invalid = self.only_invalid()

        self.assertIn("could not be read", invalid.reason)
        self.assertIn("declaration exploded", invalid.reason)
        self.assertEqual("keepa-fixture", invalid.distribution)
        self.assertEqual("keepa", invalid.entry_point)

    def test_an_empty_capability_object_reports_every_missing_required_field(self):
        self.install_distribution(source=provider_source(capabilities="Capabilities()"))

        invalid = self.only_invalid()

        for field in ("allowed_domains", "terms_urls", "license_inference"):
            self.assertIn(field, invalid.reason)

    def test_a_capability_declaration_never_carries_a_credential_value(self):
        self.install_distribution(
            source=provider_source(capabilities=capabilities_expression({"credentials": '("KEEPA_API_KEY",)'}))
        )

        summary = PLUGINS.require_registration("acquisition", "keepa").capabilities.as_dict()

        self.assertEqual(["KEEPA_API_KEY"], summary["credentials"])
        self.assertEqual(list(PLUGINS.CAPABILITY_FIELDS), list(summary))


class DuplicateRegistrationTests(FixtureDistributionCase):
    def test_two_distributions_claiming_one_id_invalidate_each_other(self):
        self.install_distribution(source=provider_source(), dist_name="keepa-first")
        self.install_distribution(source=provider_source(), dist_name="keepa-second")

        valid, invalid = PLUGINS.load_registrations("acquisition")

        self.assertEqual({}, valid, "a duplicated id must not resolve by installation order")
        self.assertEqual((), PLUGINS.registered_ids("acquisition"))
        self.assertEqual(2, len(invalid))
        reasons = {item.distribution: item.reason for item in invalid}
        self.assertEqual({"keepa-first", "keepa-second"}, set(reasons))
        self.assertIn("keepa-second", reasons["keepa-first"])
        self.assertIn("keepa-first", reasons["keepa-second"])
        for item in invalid:
            self.assertEqual("keepa", item.provider_id)

    def test_a_duplicated_id_refuses_with_registration_invalid_naming_both_distributions(self):
        self.install_distribution(source=provider_source(), dist_name="keepa-first")
        self.install_distribution(source=provider_source(), dist_name="keepa-second")

        with self.assertRaises(PLUGINS.ProviderPluginError) as ctx:
            PLUGINS.require_registration("acquisition", "keepa")

        self.assertEqual("PROVIDER_REGISTRATION_INVALID", ctx.exception.error_code)
        self.assertEqual(["keepa-first", "keepa-second"], ctx.exception.details["distributions"])

    def test_the_same_id_in_two_phases_is_not_a_duplicate(self):
        self.install_distribution(source=provider_source(), dist_name="keepa-acquire")
        self.install_distribution(source=provider_source(), dist_name="keepa-discover", group=DISCOVERY_GROUP)

        self.assertEqual(("keepa",), PLUGINS.registered_ids("acquisition"))
        self.assertEqual(("keepa",), PLUGINS.registered_ids("discovery"))


class RequireRegistrationTests(FixtureDistributionCase):
    def test_an_absent_provider_refuses_with_provider_not_registered(self):
        with self.assertRaises(PLUGINS.ProviderPluginError) as ctx:
            PLUGINS.require_registration("acquisition", "keepa")

        error = ctx.exception
        self.assertEqual("PROVIDER_NOT_REGISTERED", error.error_code)
        self.assertIn("keepa", error.message)
        self.assertIn("Install a distribution", error.remediation)
        self.assertIn(ACQUISITION_GROUP, error.remediation)
        self.assertEqual(ACQUISITION_GROUP, error.details["entry_point_group"])
        self.assertEqual([], error.details["registered"])

    def test_an_installed_but_broken_provider_refuses_with_registration_invalid(self):
        self.install_distribution(source=provider_source(capabilities=capabilities_expression({"terms_urls": "()"})))

        with self.assertRaises(PLUGINS.ProviderPluginError) as ctx:
            PLUGINS.require_registration("acquisition", "keepa")

        error = ctx.exception
        self.assertEqual("PROVIDER_REGISTRATION_INVALID", error.error_code)
        self.assertIn("keepa-fixture", error.message)
        self.assertIn("terms_urls", error.message)
        self.assertIn("Upgrade or fix", error.remediation)
        self.assertNotIn("Install a distribution", error.remediation)
        self.assertEqual(["keepa-fixture"], error.details["distributions"])

    def test_an_entry_point_that_cannot_import_still_reads_as_installed_but_invalid(self):
        self.install_distribution(source='raise RuntimeError("boom")\n')

        with self.assertRaises(PLUGINS.ProviderPluginError) as ctx:
            PLUGINS.require_registration("acquisition", "keepa")

        self.assertEqual("PROVIDER_REGISTRATION_INVALID", ctx.exception.error_code)

    def test_a_broken_registration_in_one_phase_does_not_answer_for_the_other(self):
        self.install_distribution(source='raise RuntimeError("boom")\n')

        with self.assertRaises(PLUGINS.ProviderPluginError) as ctx:
            PLUGINS.require_registration("discovery", "keepa")

        self.assertEqual("PROVIDER_NOT_REGISTERED", ctx.exception.error_code)

    def test_a_valid_registration_is_returned_unchanged(self):
        self.install_distribution(source=provider_source())

        registration = PLUGINS.require_registration("acquisition", "keepa")

        self.assertEqual("keepa", registration.provider_id)
        self.assertTrue(callable(registration.provider.plan_fetch))


class EnumerationRobustnessTests(FixtureDistributionCase):
    def test_a_failing_entry_point_seam_is_reported_rather_than_raised(self):
        original = PLUGINS._entry_points

        def explode(group):
            raise RuntimeError(f"metadata unreadable for {group}")

        PLUGINS._entry_points = explode
        try:
            PLUGINS.clear_cache()
            valid, invalid = PLUGINS.load_registrations("acquisition")
        finally:
            PLUGINS._entry_points = original
            PLUGINS.clear_cache()

        self.assertEqual({}, valid)
        self.assertEqual(1, len(invalid))
        self.assertIn("enumeration failed", invalid[0].reason)

    def test_an_unknown_phase_is_a_programming_error_not_a_refusal(self):
        with self.assertRaises(ValueError):
            PLUGINS.load_registrations("normalization")


class RegistrationCacheTests(FixtureDistributionCase):
    def test_results_are_cached_until_the_cache_is_cleared(self):
        self.install_distribution(source=provider_source())
        self.assertEqual(("keepa",), PLUGINS.registered_ids("acquisition"))

        self.install_distribution(
            source=provider_source(provider_id='"spapi"'),
            dist_name="spapi-fixture",
            entry_point_name="spapi",
            version="2.0.0",
            clear_cache=False,
        )

        self.assertEqual(("keepa",), PLUGINS.registered_ids("acquisition"), "a warm cache must not re-enumerate")

        PLUGINS.clear_cache()

        self.assertEqual(("keepa", "spapi"), PLUGINS.registered_ids("acquisition"))

    def test_a_caller_mutating_its_result_cannot_poison_the_cache(self):
        self.install_distribution(source=provider_source())

        valid, _invalid = PLUGINS.load_registrations("acquisition")
        valid.clear()

        self.assertEqual(("keepa",), PLUGINS.registered_ids("acquisition"))


class ValidatorExtensionTests(unittest.TestCase):
    """``registered=`` widens the accepted set without disturbing today's wording."""

    def test_unknown_acquisition_provider_keeps_todays_exact_message(self):
        with self.assertRaises(REGISTRY.ProviderListError) as ctx:
            REGISTRY.validate_provider_ids(["gitlab"], phase="acquisition")

        self.assertEqual(
            "has unknown provider(s): gitlab. Allowed providers: arxiv, openalex, github, web",
            str(ctx.exception),
        )

    def test_unknown_discovery_provider_keeps_todays_exact_message(self):
        with self.assertRaises(REGISTRY.ProviderListError) as ctx:
            REGISTRY.validate_provider_ids(["keepa"], phase="discovery")

        self.assertEqual(
            "has unknown provider(s): keepa. Allowed providers: " + ", ".join(REGISTRY.DISCOVERY_ACCEPTED_IDS),
            str(ctx.exception),
        )

    def test_the_unknown_provider_error_stays_catchable_as_a_provider_list_error(self):
        with self.assertRaises(REGISTRY.ProviderListError) as ctx:
            REGISTRY.validate_provider_ids(["gitlab", "gitea"], phase="acquisition")

        self.assertIsInstance(ctx.exception, REGISTRY.ProviderNotRegisteredError)
        self.assertEqual(("gitea", "gitlab"), ctx.exception.provider_ids)

    def test_a_registered_id_is_accepted_like_a_built_in(self):
        result = REGISTRY.validate_provider_ids(["web", "keepa"], phase="acquisition", registered=("keepa",))

        self.assertEqual(("web", "keepa"), result.configured)
        self.assertEqual(("web", "keepa"), result.providers)
        self.assertEqual((), result.legacy_strategies)
        self.assertTrue(REGISTRY.provider_is_allowed(result, "keepa"))

    def test_a_registered_discovery_id_satisfies_require_non_empty_beside_legacy_aliases(self):
        result = REGISTRY.validate_provider_ids(
            ["legal", "keepa"],
            phase="discovery",
            require_non_empty=True,
            registered=("keepa",),
        )

        self.assertEqual(("keepa",), result.providers)
        self.assertEqual(("legal",), result.legacy_strategies)

    def test_an_unregistered_id_names_the_registrations_this_environment_supplies(self):
        with self.assertRaises(REGISTRY.ProviderNotRegisteredError) as ctx:
            REGISTRY.validate_provider_ids(["spapi"], phase="acquisition", registered=("keepa",))

        message = str(ctx.exception)
        self.assertIn("has unknown provider(s): spapi", message)
        self.assertIn("Allowed providers: arxiv, openalex, github, web, keepa", message)
        self.assertIn("Registered providers: keepa", message)
        self.assertEqual(("spapi",), ctx.exception.provider_ids)

    def test_a_registered_id_shadowing_a_built_in_never_duplicates_the_allowed_list(self):
        with self.assertRaises(REGISTRY.ProviderNotRegisteredError) as ctx:
            REGISTRY.validate_provider_ids(["spapi"], phase="acquisition", registered=("web", "web"))

        self.assertEqual(
            "has unknown provider(s): spapi. Allowed providers: arxiv, openalex, github, web",
            str(ctx.exception),
        )

    def test_shape_and_duplicate_errors_stay_plain_provider_list_errors(self):
        cases = (
            ("not a list", "web", "must be a list of provider identifiers"),
            ("blank entry", ["  "], "must be a list of non-empty provider identifiers"),
            ("duplicate", ["web", "web"], "has duplicate provider(s): web"),
        )
        for label, value, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(REGISTRY.ProviderListError) as ctx:
                    REGISTRY.validate_provider_ids(value, phase="acquisition", registered=("keepa",))
                self.assertNotIsInstance(ctx.exception, REGISTRY.ProviderNotRegisteredError)
                self.assertIn(expected, str(ctx.exception))

    def test_an_empty_registered_tuple_is_the_universe_the_built_ins_describe(self):
        result = REGISTRY.validate_provider_ids(["arxiv"], phase="acquisition", registered=())

        self.assertEqual(("arxiv",), result.providers)
        with self.assertRaises(REGISTRY.ProviderNotRegisteredError):
            REGISTRY.validate_provider_ids(["keepa"], phase="acquisition", registered=())


if __name__ == "__main__":
    unittest.main()
