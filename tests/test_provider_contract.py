"""The provider authoring contract has to refuse a bad declaration at construction time.

`src/evidence_wiki/providers.py` is what a third-party distribution imports to declare a
provider.  It is deliberately *not* the authority — `_provider_plugins.py` in the
workspace template re-validates every registration structurally, because a deployed
workspace can never import this package.  That duplication is the point, and it is also
the risk: if these dataclasses accept something the workspace loader will later refuse,
the plugin author finds out in somebody else's workspace instead of in their own test
suite.

So these tests pin the rules rather than the implementation.  Every `__post_init__`
rule gets an accepting case and a refusing one, because a validator that only ever says
"no" passes a rejection-only suite.  Three traps get their own tests:

- `isinstance(True, int)` is True, so `RateLimit(True, per="minute")` would silently
  declare a limit of one request per minute unless booleans are excluded by hand;
- a credential placeholder is legal in a header value and illegal in a URL, because URL
  redaction cannot know that a query parameter holds a secret;
- a bare string is iterable, so `allowed_domains="example.com"` would otherwise freeze
  into eleven single-character hostnames that match nothing.

A fourth pinned property is about the *rejections* rather than the rules: a header value,
a request body, and artifact content can each hold a secret an author hard-coded instead
of declaring, so no rejection message may render them.  These messages travel outward as
`PROVIDER_PLAN_INVALID` detail.

The duck-typing premise gets a test too: a provider class that imports nothing from this
package is still a valid provider, since the shape — not the base class — is the
contract.
"""

import ast
import dataclasses
import unittest
from pathlib import Path
from types import MappingProxyType

from evidence_wiki import providers
from evidence_wiki.providers import (
    ACQUISITION_ENTRY_POINT_GROUP,
    CREDENTIAL_PLACEHOLDER_RE,
    DISCOVERY_ENTRY_POINT_GROUP,
    MAX_PLANNED_REQUESTS,
    PROVIDER_API_VERSION,
    AcquisitionProvider,
    DiscoveryProvider,
    PlannedRequest,
    ProviderCapabilities,
    RateLimit,
    SourceArtifact,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "src" / "evidence_wiki" / "providers.py"

# The contract module is pure declaration: nothing here may reach a file, a socket, the
# environment, or the clock.
ALLOWED_MODULE_IMPORTS = {"__future__", "abc", "collections", "dataclasses", "json", "re", "types", "typing"}


def make_capabilities(**overrides):
    """A minimal valid declaration, with one field overridden per rejection case."""

    kwargs = {
        "allowed_domains": ("api.example.com",),
        "terms_urls": ("https://example.com/terms",),
        "license_inference": "partial",
    }
    kwargs.update(overrides)
    return ProviderCapabilities(**kwargs)


class ProviderModuleIdentityTests(unittest.TestCase):
    """`evidence_wiki` is installed editable against another checkout on this machine.

    Without this assertion a worktree run would import the *installed* package, exercise
    a file nobody in this branch wrote, and pass vacuously.
    """

    def test_the_imported_contract_module_is_the_one_in_this_repository(self):
        self.assertEqual(
            Path(providers.__file__).resolve(),
            MODULE_PATH,
            "tests must run against this checkout: set PYTHONPATH to its src/ directory",
        )

    def test_the_contract_module_imports_nothing_that_can_perform_io(self):
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - ALLOWED_MODULE_IMPORTS, set())

    def test_the_module_publishes_the_frozen_v1_constants(self):
        self.assertEqual(PROVIDER_API_VERSION, 1)
        self.assertEqual(ACQUISITION_ENTRY_POINT_GROUP, "evidence_wiki.acquisition_providers")
        self.assertEqual(DISCOVERY_ENTRY_POINT_GROUP, "evidence_wiki.discovery_providers")
        self.assertEqual(MAX_PLANNED_REQUESTS, 8)


class RateLimitTests(unittest.TestCase):
    def test_a_declared_window_is_accepted_and_frozen(self):
        for requests, per in ((1, "minute"), (60, "minute"), (5000, "hour")):
            with self.subTest(requests=requests, per=per):
                limit = RateLimit(requests, per=per)
                self.assertEqual(limit.requests, requests)
                self.assertEqual(limit.per, per)
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    limit.requests = 2

    def test_a_boolean_request_count_is_refused_even_though_bool_is_an_int(self):
        # isinstance(True, int) is True: without an explicit guard this would declare a
        # ceiling of one request per minute and look like a deliberate choice.
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    RateLimit(value, per="minute")
                self.assertIn("RateLimit.requests", str(caught.exception))
                self.assertIn("must be an int", str(caught.exception))

    def test_a_nonsensical_ceiling_or_window_is_refused(self):
        cases = (
            ({"requests": 0, "per": "minute"}, "must be >= 1"),
            ({"requests": -1, "per": "minute"}, "must be >= 1"),
            ({"requests": 1.5, "per": "minute"}, "must be an int"),
            ({"requests": "60", "per": "minute"}, "must be an int"),
            ({"requests": 60, "per": "day"}, "RateLimit.per"),
            ({"requests": 60, "per": "MINUTE"}, "RateLimit.per"),
            ({"requests": 60, "per": None}, "RateLimit.per"),
        )
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError) as caught:
                    RateLimit(**kwargs)
                self.assertIn(expected, str(caught.exception))


class ProviderCapabilitiesTests(unittest.TestCase):
    def test_a_complete_declaration_is_accepted(self):
        capabilities = ProviderCapabilities(
            allowed_domains=("api.keepa.com",),
            terms_urls=("https://keepa.com/#!api",),
            license_inference="none",
            rate_limit=RateLimit(60, per="minute"),
            credentials=("KEEPA_API_KEY",),
            request_kinds=("market-data/price_history",),
        )
        self.assertEqual(capabilities.allowed_domains, ("api.keepa.com",))
        self.assertTrue(capabilities.captures_raw)
        self.assertTrue(capabilities.quarantine_on_incomplete)
        self.assertEqual(capabilities.rate_limit, RateLimit(60, per="minute"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            capabilities.allowed_domains = ()

    def test_the_web_provider_shape_of_terms_urls_is_accepted(self):
        # The built-in `web` provider declares the literal "per-origin" because its terms
        # differ per host; a registered provider may say the same thing.
        capabilities = make_capabilities(terms_urls=["per-origin"])
        self.assertEqual(capabilities.terms_urls, ("per-origin",))

    def test_lists_are_frozen_into_tuples_so_an_author_can_pass_either(self):
        capabilities = ProviderCapabilities(
            allowed_domains=["api.example.com", "cdn.example.com"],
            terms_urls=["https://example.com/terms"],
            license_inference="yes",
            credentials=["EXAMPLE_TOKEN"],
            request_kinds=["market-data/price_history"],
        )
        for name, expected in (
            ("allowed_domains", ("api.example.com", "cdn.example.com")),
            ("terms_urls", ("https://example.com/terms",)),
            ("credentials", ("EXAMPLE_TOKEN",)),
            ("request_kinds", ("market-data/price_history",)),
        ):
            with self.subTest(field=name):
                value = getattr(capabilities, name)
                self.assertIsInstance(value, tuple)
                self.assertEqual(value, expected)

    def test_domains_are_stripped_and_lowercased_like_the_builtin_domain_validator(self):
        capabilities = make_capabilities(allowed_domains=("  API.Example.COM  ",))
        self.assertEqual(capabilities.allowed_domains, ("api.example.com",))

    def test_domains_that_are_not_bare_hostnames_are_refused(self):
        cases = (
            ("https://api.example.com", "bare hostnames"),
            ("api.example.com/v1", "bare hostnames"),
            ("api.example.com\\v1", "bare hostnames"),
            ("api example com", "whitespace"),
            ("api.example.com\tx", "whitespace"),
        )
        for domain, expected in cases:
            with self.subTest(domain=domain):
                with self.assertRaises(ValueError) as caught:
                    make_capabilities(allowed_domains=(domain,))
                self.assertIn("ProviderCapabilities.allowed_domains", str(caught.exception))
                self.assertIn(expected, str(caught.exception))

    def test_an_empty_or_malformed_domain_declaration_is_refused(self):
        cases = ((), [], "api.example.com", None, ("",), (b"api.example.com",), (None,), 7)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    make_capabilities(allowed_domains=value)
                self.assertIn("ProviderCapabilities.allowed_domains", str(caught.exception))

    def test_terms_urls_must_be_https_or_the_per_origin_literal(self):
        cases = ((), ("http://example.com/terms",), ("example.com/terms",), ("https://",), ("PER-ORIGIN",))
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    make_capabilities(terms_urls=value)
                self.assertIn("ProviderCapabilities.terms_urls", str(caught.exception))

    def test_license_inference_uses_the_builtin_registry_vocabulary(self):
        for value in ("yes", "partial", "none"):
            with self.subTest(value=value):
                self.assertEqual(make_capabilities(license_inference=value).license_inference, value)
        for value in ("maybe", "None", "", None, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    make_capabilities(license_inference=value)
                self.assertIn("ProviderCapabilities.license_inference", str(caught.exception))

    def test_declaring_a_guarantee_the_package_will_not_honour_is_refused(self):
        for field_name in ("captures_raw", "quarantine_on_incomplete"):
            for value in (False, None, 1, "yes"):
                with self.subTest(field=field_name, value=value):
                    with self.assertRaises(ValueError) as caught:
                        make_capabilities(**{field_name: value})
                    message = str(caught.exception)
                    self.assertIn(field_name, message)
                    self.assertIn("must be True", message)
                    self.assertIn("the package will not honour", message)

    def test_credentials_are_environment_variable_names_not_values(self):
        capabilities = make_capabilities(credentials=("KEEPA_API_KEY", "EXAMPLE_TOKEN2"))
        self.assertEqual(capabilities.credentials, ("KEEPA_API_KEY", "EXAMPLE_TOKEN2"))
        self.assertEqual(make_capabilities().credentials, ())
        cases = (
            ("keepa_api_key",),
            ("2FA_TOKEN",),
            ("_TOKEN",),
            ("KEEPA-API-KEY",),
            ("KEEPA API KEY",),
            ("KEEPA_API_KEY=sk-live-1",),
            ("KEEPA_API_KEY", "KEEPA_API_KEY"),
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    make_capabilities(credentials=value)
                self.assertIn("ProviderCapabilities.credentials", str(caught.exception))

    def test_request_kinds_are_shape_validated_only(self):
        capabilities = make_capabilities(request_kinds=("market-data/price_history", "anything-at-all"))
        self.assertEqual(capabilities.request_kinds, ("market-data/price_history", "anything-at-all"))
        for value in (("",), ("   ",), (None,), ("a", "a"), "market-data/price_history"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    make_capabilities(request_kinds=value)
                self.assertIn("ProviderCapabilities.request_kinds", str(caught.exception))

    def test_a_rate_limit_must_be_a_rate_limit_or_absent(self):
        self.assertIsNone(make_capabilities().rate_limit)
        for value in ({"requests": 60, "per": "minute"}, (60, "minute"), 60):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    make_capabilities(rate_limit=value)
                self.assertIn("ProviderCapabilities.rate_limit", str(caught.exception))


class PlannedRequestTests(unittest.TestCase):
    def test_a_plain_https_get_is_accepted_and_frozen(self):
        planned = PlannedRequest(url="https://api.example.com/v1/items?asin=B01")
        self.assertEqual(planned.method, "GET")
        self.assertEqual(planned.headers, ())
        self.assertIsNone(planned.body)
        self.assertIsNone(planned.timeout_hint)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            planned.url = "https://elsewhere.example.com/"

    def test_a_credential_placeholder_is_accepted_in_a_header_value(self):
        planned = PlannedRequest(
            url="https://api.keepa.com/product",
            headers=[["Authorization", "Bearer {{credential:KEEPA_API_KEY}}"]],
        )
        self.assertEqual(planned.headers, (("Authorization", "Bearer {{credential:KEEPA_API_KEY}}"),))
        self.assertIsInstance(planned.headers[0], tuple)

    def test_a_credential_placeholder_in_the_url_is_refused_because_redaction_cannot_see_it(self):
        for url in (
            "https://api.keepa.com/product?key={{credential:KEEPA_API_KEY}}",
            "https://api.keepa.com/{{credential:BROKEN",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    PlannedRequest(url=url)
                message = str(caught.exception)
                self.assertIn("PlannedRequest.url", message)
                self.assertIn("redaction", message)

    def test_a_url_that_is_not_https_with_a_host_is_refused(self):
        cases = (
            "http://api.example.com/v1",
            "ftp://api.example.com/v1",
            "//api.example.com/v1",
            "api.example.com/v1",
            "https://",
            "https:// api.example.com",
            "https://api.example.com/a b",
            "",
            "   ",
            None,
            b"https://api.example.com",
        )
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    PlannedRequest(url=url)
                self.assertIn("PlannedRequest.url", str(caught.exception))

    def test_only_get_and_post_are_planned(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                self.assertEqual(PlannedRequest(url="https://api.example.com/v1", method=method).method, method)
        for method in ("get", "post", "PUT", "DELETE", "HEAD", "", None):
            with self.subTest(method=method):
                with self.assertRaises(ValueError) as caught:
                    PlannedRequest(url="https://api.example.com/v1", method=method)
                self.assertIn("PlannedRequest.method", str(caught.exception))

    def test_a_body_belongs_to_post_only_and_must_be_bytes(self):
        planned = PlannedRequest(url="https://api.example.com/v1", method="POST", body=b'{"q": 1}')
        self.assertEqual(planned.body, b'{"q": 1}')
        cases = (
            ({"method": "GET", "body": b"x"}, 'only allowed when method is "POST"'),
            ({"method": "POST", "body": "x"}, "must be bytes or None"),
            ({"method": "POST", "body": bytearray(b"x")}, "must be bytes or None"),
        )
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError) as caught:
                    PlannedRequest(url="https://api.example.com/v1", **kwargs)
                self.assertIn("PlannedRequest.body", str(caught.exception))
                self.assertIn(expected, str(caught.exception))

    def test_headers_are_frozen_pairs_of_non_empty_strings(self):
        planned = PlannedRequest(
            url="https://api.example.com/v1",
            headers=[("Accept", "application/json"), ["User-Agent", "example/1.0"]],
        )
        self.assertEqual(planned.headers, (("Accept", "application/json"), ("User-Agent", "example/1.0")))
        cases = (
            "Accept: application/json",
            ("Accept",),
            (("Accept",),),
            (("Accept", "application/json", "extra"),),
            (("", "application/json"),),
            (("Accept", ""),),
            ((None, "application/json"),),
            (("Accept", 7),),
            (("Accept\r\nX-Injected", "value"),),
            (("Accept", "application/json\r\nX-Injected: 1"),),
        )
        for headers in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(ValueError) as caught:
                    PlannedRequest(url="https://api.example.com/v1", headers=headers)
                self.assertIn("PlannedRequest.headers", str(caught.exception))

    def test_a_rejected_header_value_or_body_never_reaches_the_error_message(self):
        secret = "sk-live-do-not-print-this"
        with self.assertRaises(ValueError) as caught:
            PlannedRequest(
                url="https://api.example.com/v1",
                headers=[("Authorization", f"Bearer {secret}\r\nX-Injected: 1")],
            )
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("Authorization", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            PlannedRequest(url="https://api.example.com/v1", method="GET", body=secret.encode("utf-8"))
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("25 bytes", str(caught.exception))

    def test_a_timeout_hint_is_a_positive_finite_number_of_seconds(self):
        self.assertEqual(PlannedRequest(url="https://api.example.com/v1", timeout_hint=5).timeout_hint, 5.0)
        self.assertIsInstance(PlannedRequest(url="https://api.example.com/v1", timeout_hint=5).timeout_hint, float)
        for value in (0, -1, True, "30", float("inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    PlannedRequest(url="https://api.example.com/v1", timeout_hint=value)
                self.assertIn("PlannedRequest.timeout_hint", str(caught.exception))


class SourceArtifactTests(unittest.TestCase):
    def test_a_bare_filename_with_bytes_is_accepted_and_frozen(self):
        artifact = SourceArtifact(filename="price-history.json", source_type="dataset", content=b"{}")
        self.assertEqual(artifact.filename, "price-history.json")
        self.assertEqual(artifact.content, b"{}")
        self.assertEqual(dict(artifact.provenance_metadata), {})
        self.assertEqual(artifact.warnings, ())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.filename = "elsewhere.json"

    def test_a_filename_that_could_escape_the_target_root_is_refused(self):
        cases = ("", "   ", "sub/dir.json", "sub\\dir.json", "/abs.json", ".", "..", "..data.json", "C:data.json")
        for filename in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError) as caught:
                    SourceArtifact(filename=filename, source_type="dataset", content=b"{}")
                self.assertIn("SourceArtifact.filename", str(caught.exception))

    def test_a_filename_with_a_control_character_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            SourceArtifact(filename="price\x00history.json", source_type="dataset", content=b"{}")
        self.assertIn("control characters", str(caught.exception))

    def test_content_must_be_the_exact_bytes_the_package_will_write(self):
        for content in ("{}", bytearray(b"{}"), None, 7):
            with self.subTest(content=content):
                with self.assertRaises(ValueError) as caught:
                    SourceArtifact(filename="a.json", source_type="dataset", content=content)
                self.assertIn("SourceArtifact.content", str(caught.exception))

    def test_rejected_content_never_reaches_the_error_message(self):
        payload = "sk-live-do-not-print-this"
        with self.assertRaises(ValueError) as caught:
            SourceArtifact(filename="a.json", source_type="dataset", content=payload)
        self.assertNotIn(payload, str(caught.exception))
        self.assertIn("got str", str(caught.exception))

    def test_source_type_must_be_a_non_empty_string(self):
        for source_type in ("", "   ", None, 7):
            with self.subTest(source_type=source_type):
                with self.assertRaises(ValueError) as caught:
                    SourceArtifact(filename="a.json", source_type=source_type, content=b"{}")
                self.assertIn("SourceArtifact.source_type", str(caught.exception))

    def test_provenance_metadata_is_copied_and_frozen(self):
        original = {"query": {"asin": "B01"}, "pages": 2, "complete": True, "cursor": None}
        artifact = SourceArtifact(
            filename="a.json",
            source_type="dataset",
            content=b"{}",
            provenance_metadata=original,
        )
        self.assertIsInstance(artifact.provenance_metadata, MappingProxyType)
        self.assertEqual(dict(artifact.provenance_metadata), original)
        original["query"] = "mutated after construction"
        self.assertEqual(artifact.provenance_metadata["query"], {"asin": "B01"})

    def test_provenance_metadata_that_is_not_json_safe_is_refused(self):
        cases = (
            {"seen": {"a", "b"}},
            {"fetched_at": object()},
            {"ratio": float("nan")},
            {"ratio": float("inf")},
            {1: "int key does not survive a json round trip"},
            {"nested": {2: "int key"}},
            {"nested": [{"deeper": {3: "int key"}}]},
            {"raw": b"bytes are not json"},
        )
        for metadata in cases:
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError) as caught:
                    SourceArtifact(
                        filename="a.json",
                        source_type="dataset",
                        content=b"{}",
                        provenance_metadata=metadata,
                    )
                self.assertIn("SourceArtifact.provenance_metadata", str(caught.exception))

    def test_provenance_metadata_must_be_a_mapping(self):
        for metadata in ([("a", 1)], "a=1", None, 7):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError) as caught:
                    SourceArtifact(
                        filename="a.json",
                        source_type="dataset",
                        content=b"{}",
                        provenance_metadata=metadata,
                    )
                self.assertIn("must be a mapping", str(caught.exception))

    def test_warnings_accept_a_list_and_freeze_into_a_tuple(self):
        artifact = SourceArtifact(
            filename="a.json",
            source_type="dataset",
            content=b"{}",
            warnings=["truncated at 1000 rows"],
        )
        self.assertEqual(artifact.warnings, ("truncated at 1000 rows",))
        for warnings in (("",), (None,), "truncated"):
            with self.subTest(warnings=warnings):
                with self.assertRaises(ValueError) as caught:
                    SourceArtifact(filename="a.json", source_type="dataset", content=b"{}", warnings=warnings)
                self.assertIn("SourceArtifact.warnings", str(caught.exception))


class CredentialPlaceholderTests(unittest.TestCase):
    def test_the_placeholder_pattern_extracts_the_variable_name_from_a_header_value(self):
        match = CREDENTIAL_PLACEHOLDER_RE.search("Bearer {{credential:KEEPA_API_KEY}}")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "KEEPA_API_KEY")

    def test_every_placeholder_in_a_composite_header_value_is_found(self):
        value = "user={{credential:EXAMPLE_USER}}; token={{credential:EXAMPLE_TOKEN_2}}"
        self.assertEqual(CREDENTIAL_PLACEHOLDER_RE.findall(value), ["EXAMPLE_USER", "EXAMPLE_TOKEN_2"])

    def test_a_placeholder_naming_something_that_is_not_an_env_var_name_does_not_match(self):
        for value in (
            "Bearer {{credential:keepa_api_key}}",
            "Bearer {{credential:2FA}}",
            "Bearer {{credential:_TOKEN}}",
            "Bearer {{credential:KEEPA-API-KEY}}",
            "Bearer {credential:KEEPA_API_KEY}",
            "Bearer sk-live-not-a-placeholder",
        ):
            with self.subTest(value=value):
                self.assertIsNone(CREDENTIAL_PLACEHOLDER_RE.search(value))


class ProviderBaseClassTests(unittest.TestCase):
    def test_the_base_classes_cannot_be_instantiated_without_the_contract_methods(self):
        for base in (AcquisitionProvider, DiscoveryProvider):
            with self.subTest(base=base.__name__):
                with self.assertRaises(TypeError):
                    base()

    def test_a_complete_subclass_is_usable(self):
        class ExampleAcquisition(AcquisitionProvider):
            id = "example"
            provider_api_version = PROVIDER_API_VERSION
            capabilities = make_capabilities()

            def validate_request(self, request):
                return None

            def plan_fetch(self, request):
                return (PlannedRequest(url="https://api.example.com/v1"),)

            def interpret(self, request, responses):
                return SourceArtifact(filename="a.json", source_type="dataset", content=responses[0])

        provider = ExampleAcquisition()
        self.assertIsNone(provider.validate_request({}))
        self.assertEqual(len(provider.plan_fetch({})), 1)
        self.assertEqual(provider.interpret({}, (b"{}",)).content, b"{}")

    def test_a_discovery_subclass_returns_candidate_mappings(self):
        class ExampleDiscovery(DiscoveryProvider):
            id = "example-search"
            provider_api_version = PROVIDER_API_VERSION
            capabilities = make_capabilities()

            def validate_request(self, request):
                return None

            def plan_search(self, request):
                return (PlannedRequest(url="https://api.example.com/search"),)

            def interpret_candidates(self, request, responses):
                return ({"title": "a candidate", "url": "https://example.com/a"},)

        provider = ExampleDiscovery()
        self.assertEqual(provider.interpret_candidates({}, (b"[]",))[0]["title"], "a candidate")

    def test_subclassing_is_a_convenience_and_not_the_contract(self):
        """The workspace loader duck-types, so a provider importing nothing still counts.

        This mirrors what `_provider_plugins.py` checks structurally.  If this module
        ever became load-bearing for registration, a workspace deployed beside a
        differently-versioned package would start refusing valid plugins.
        """

        class UnrelatedProvider:  # deliberately no base class, no dataclasses from here
            id = "unrelated"
            provider_api_version = 1
            capabilities = type(
                "Caps",
                (),
                {
                    "allowed_domains": ("api.example.com",),
                    "terms_urls": ("https://example.com/terms",),
                    "license_inference": "none",
                    "captures_raw": True,
                    "quarantine_on_incomplete": True,
                    "rate_limit": None,
                    "credentials": (),
                    "request_kinds": (),
                },
            )()

            def validate_request(self, request):
                return None

            def plan_fetch(self, request):
                return ()

            def interpret(self, request, responses):
                return None

        provider = UnrelatedProvider()
        self.assertNotIsInstance(provider, AcquisitionProvider)
        for attribute in ("id", "provider_api_version", "capabilities"):
            with self.subTest(attribute=attribute):
                self.assertTrue(hasattr(provider, attribute))
        for method in ("validate_request", "plan_fetch", "interpret"):
            with self.subTest(method=method):
                self.assertTrue(callable(getattr(provider, method, None)))
        self.assertEqual(provider.capabilities.allowed_domains, ("api.example.com",))


if __name__ == "__main__":
    unittest.main()
