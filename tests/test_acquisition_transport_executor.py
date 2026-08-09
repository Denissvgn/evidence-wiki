"""Why: CR-5 opens provider registration to third-party distributions, and its second
acceptance criterion is that a registered provider reaching outside its declared
`allowed_domains` is blocked *by the package*, not by convention. The planner/executor
split makes that enforceable: the plugin plans an HTTPS request, and
`_acquisition_transport.execute_planned_request` is the only thing that performs it.

These tests pin the properties that make the block real rather than decorative:

- the declaration is the boundary, refused with `ACQUISITION_DOMAIN_NOT_DECLARED`
  *before* any DNS or socket work, with the same subdomain semantics the built-ins use;
- credential custody stays with the package -- a plan carries `{{credential:NAME}}`
  placeholders, never values, and an undeclared or unset variable is a loud refusal
  rather than a silently empty header;
- no resolved secret survives into any rendered error text;
- a plan cannot widen the caller's timeout or escape the byte cap.

Every test injects a fake opener and a fake resolver: this file must never touch the
network or DNS.
"""

import importlib.util
import sys
import traceback
import unittest
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
TRANSPORT_PATH = SCRIPTS / "_acquisition_transport.py"

EXECUTOR_MODULE_NAME = "cr5_executor_acquisition_transport"
DECLARED_CREDENTIAL = "KEEPA_API_KEY"
SECRET_VALUE = "keepa-live-9f3a2b7c-do-not-log"


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


@dataclass(frozen=True)
class PlannedRequest:
    """Duck-typed stand-in for the shape `evidence_wiki.providers` defines."""

    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    timeout_hint: float | None = None


@dataclass
class RecordingResolver:
    """DNS seam that records every call so refusals can be proved pre-resolution."""

    calls: list[tuple[str, object]] = field(default_factory=list)

    def __call__(self, host, port=None):
        self.calls.append((host, port))
        return [(2, 1, 6, "", ("93.184.216.34", 0))]


class FakeResponse:
    def __init__(self, body: bytes, *, url: str, status: int = 200, content_type: str = "application/json") -> None:
        self.body = body
        self.url = url
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.closed = True
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
    """Transport seam that records the request the package actually built."""

    body: bytes = b'{"ok":true}'
    status: int = 200
    content_type: str = "application/json"
    error: BaseException | None = None
    requests: list[object] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return FakeResponse(
            self.body,
            url=request.full_url,
            status=self.status,
            content_type=self.content_type,
        )

    @property
    def request(self):
        if not self.requests:
            raise AssertionError("transport was never invoked")
        return self.requests[-1]


def rendered_error_text(exc: BaseException) -> str:
    """Render an exception the way an operator or a log would actually see it."""
    return "\n".join(
        [
            str(exc),
            repr(exc),
            getattr(exc, "message", "") or "",
            getattr(exc, "remediation", "") or "",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
    )


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        # Re-executing the module gives every test a clean secret-redaction registry.
        self.transport = load_script_module(EXECUTOR_MODULE_NAME, TRANSPORT_PATH)
        self.addCleanup(self.transport.reset_registered_secrets)
        self.resolver = RecordingResolver()
        self.opener = RecordingOpener()

    def execute(self, planned, **overrides):
        kwargs = {
            "allowed_domains": ("api.keepa.com",),
            "credentials": (DECLARED_CREDENTIAL,),
            "env": {DECLARED_CREDENTIAL: SECRET_VALUE},
            "timeout": 30.0,
            "max_bytes": 4096,
            "opener": self.opener,
            "resolver": self.resolver,
        }
        kwargs.update(overrides)
        return self.transport.execute_planned_request(planned, **kwargs)


class DeclaredDomainEnforcementTests(ExecutorTestCase):
    def test_planned_request_on_the_exact_declared_host_is_executed(self):
        result = self.execute(PlannedRequest(url="https://api.keepa.com/product?asin=B01"))

        self.assertEqual(b'{"ok":true}', result.content)
        self.assertEqual("https://api.keepa.com/product?asin=B01", result.final_url)
        self.assertEqual(200, result.http_status)
        self.assertEqual("GET", self.opener.request.get_method())
        self.assertIsNone(self.opener.request.data)

    def test_subdomain_of_a_declared_domain_matches_the_builtin_semantics(self):
        for url, declared, allowed in (
            ("https://api.keepa.com/product", ("keepa.com",), True),
            ("https://deep.api.keepa.com/product", ("keepa.com",), True),
            ("https://keepa.com/product", ("api.keepa.com",), False),
            ("https://evilkeepa.com/product", ("keepa.com",), False),
            ("https://keepa.com.attacker.test/product", ("keepa.com",), False),
        ):
            with self.subTest(url=url, declared=declared):
                self.opener = RecordingOpener()
                host = self.transport.normalize_host(url.split("/")[2])
                same = self.transport.domain_allowed(host, list(declared))
                self.assertEqual(allowed, same, "test expectation must match domain_allowed()")
                if allowed:
                    self.execute(PlannedRequest(url=url), allowed_domains=declared)
                    self.assertEqual(1, len(self.opener.requests))
                else:
                    with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                        self.execute(PlannedRequest(url=url), allowed_domains=declared)
                    self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", ctx.exception.error_code)
                    self.assertEqual([], self.opener.requests)

    def test_unrelated_host_is_refused_before_any_dns_or_socket_work(self):
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(PlannedRequest(url="https://exfiltration.example.com/collect"))

        self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", ctx.exception.error_code)
        self.assertIn("exfiltration.example.com", ctx.exception.message)
        self.assertIn("api.keepa.com", ctx.exception.message)
        self.assertEqual([], self.resolver.calls, "DNS ran before the declaration was checked")
        self.assertEqual([], self.opener.requests, "a socket was opened before the declaration was checked")

    def test_declaring_no_domains_refuses_every_host(self):
        for declaration in ((), None, []):
            with self.subTest(declaration=declaration):
                self.opener = RecordingOpener()
                with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                    self.execute(
                        PlannedRequest(url="https://api.keepa.com/product"),
                        allowed_domains=declaration,
                    )
                self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", ctx.exception.error_code)
                self.assertEqual([], self.resolver.calls)
                self.assertEqual([], self.opener.requests)

    def test_declaration_written_as_a_url_is_refused_rather_than_guessed(self):
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(
                PlannedRequest(url="https://api.keepa.com/product"),
                allowed_domains=("https://api.keepa.com/",),
            )

        self.assertEqual("ACQUISITION_DOMAIN_NOT_DECLARED", ctx.exception.error_code)
        self.assertEqual([], self.opener.requests)

    def test_non_public_host_keeps_the_builtin_url_unsafe_envelope(self):
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(
                PlannedRequest(url="https://127.0.0.1/product"),
                allowed_domains=("127.0.0.1",),
            )

        self.assertEqual("ACQUISITION_URL_UNSAFE", ctx.exception.error_code)
        self.assertEqual([], self.opener.requests)


class CredentialCustodyTests(ExecutorTestCase):
    def test_declared_placeholder_is_resolved_into_a_realistic_auth_header(self):
        planned = PlannedRequest(
            url="https://api.keepa.com/product?asin=B01",
            headers=(
                ("Accept", "application/json"),
                ("Authorization", f"Bearer {{{{credential:{DECLARED_CREDENTIAL}}}}}"),
            ),
        )

        self.execute(planned)

        request = self.opener.request
        self.assertEqual(f"Bearer {SECRET_VALUE}", request.get_header("Authorization"))
        self.assertEqual("application/json", request.get_header("Accept"))
        self.assertNotIn("{{credential:", str(request.headers))

    def test_resolver_returns_secret_values_so_the_caller_can_register_them(self):
        headers = (
            ("Authorization", f"Bearer {{{{credential:{DECLARED_CREDENTIAL}}}}}"),
            ("X-Api-Key", f"{{{{credential:{DECLARED_CREDENTIAL}}}}}"),
        )

        resolved, secrets = self.transport.resolve_credential_placeholders(
            headers,
            capabilities_credentials=(DECLARED_CREDENTIAL,),
            env={DECLARED_CREDENTIAL: SECRET_VALUE},
        )

        self.assertEqual(
            (("Authorization", f"Bearer {SECRET_VALUE}"), ("X-Api-Key", SECRET_VALUE)),
            resolved,
        )
        self.assertEqual((SECRET_VALUE,), secrets)
        self.assertIn(SECRET_VALUE, self.transport.registered_secret_values())

    def test_undeclared_credential_name_is_refused_and_named(self):
        planned = PlannedRequest(
            url="https://api.keepa.com/product",
            headers=(("Authorization", "Bearer {{credential:SPAPI_REFRESH_TOKEN}}"),),
        )

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(planned)

        self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
        self.assertIn("SPAPI_REFRESH_TOKEN", ctx.exception.message)
        self.assertEqual([], self.opener.requests)

    def test_declared_but_unset_variable_refuses_instead_of_sending_an_empty_header(self):
        planned = PlannedRequest(
            url="https://api.keepa.com/product",
            headers=(("Authorization", f"Bearer {{{{credential:{DECLARED_CREDENTIAL}}}}}"),),
        )

        for environment in ({}, {DECLARED_CREDENTIAL: ""}, {DECLARED_CREDENTIAL: "   "}):
            with self.subTest(environment=sorted(environment)):
                self.opener = RecordingOpener()
                with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                    self.execute(planned, env=environment)
                self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
                self.assertIn(DECLARED_CREDENTIAL, ctx.exception.message)
                self.assertEqual([], self.opener.requests, "a header with no credential was sent anyway")

    def test_malformed_placeholder_is_refused_rather_than_sent_literally(self):
        planned = PlannedRequest(
            url="https://api.keepa.com/product",
            headers=(("Authorization", "Bearer {{credential:keepa_api_key}}"),),
        )

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(planned)

        self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
        self.assertEqual([], self.opener.requests)

    def test_credential_placeholder_in_the_url_is_refused(self):
        planned = PlannedRequest(
            url=f"https://api.keepa.com/product?key={{{{credential:{DECLARED_CREDENTIAL}}}}}",
        )

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(planned)

        self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
        self.assertEqual([], self.resolver.calls)
        self.assertEqual([], self.opener.requests)

    def test_declared_credential_names_participate_in_diagnostic_redaction(self):
        planned = PlannedRequest(
            url="https://api.keepa.com/product",
            headers=(("Authorization", f"Bearer {{{{credential:{DECLARED_CREDENTIAL}}}}}"),),
        )

        self.execute(planned)

        self.assertIn(DECLARED_CREDENTIAL, self.transport.registered_secret_env_names())
        self.assertEqual(
            "connection reset while sending [REDACTED]",
            self.transport.redact_diagnostic(f"connection reset while sending {SECRET_VALUE}"),
        )

    def test_redaction_is_unchanged_when_no_provider_credential_was_resolved(self):
        self.assertEqual(
            "nothing to hide here",
            self.transport.redact_diagnostic("nothing to hide here"),
        )
        self.assertEqual((), self.transport.registered_secret_values())
        self.assertEqual((), self.transport.registered_secret_env_names())


class SecretLeakageTests(ExecutorTestCase):
    """Every failure path is checked against the fully rendered error text."""

    def failure_cases(self):
        credential_header = (("Authorization", f"Bearer {{{{credential:{DECLARED_CREDENTIAL}}}}}"),)
        return {
            "transport error quoting the sent header": (
                PlannedRequest(url="https://api.keepa.com/product", headers=credential_header),
                {"opener": RecordingOpener(error=OSError(f"reset while sending Authorization: Bearer {SECRET_VALUE}"))},
            ),
            "oversized response": (
                PlannedRequest(url="https://api.keepa.com/product", headers=credential_header),
                {"opener": RecordingOpener(body=SECRET_VALUE.encode("utf-8") * 8), "max_bytes": 8},
            ),
            "unexpected status": (
                PlannedRequest(url="https://api.keepa.com/product", headers=credential_header),
                {"opener": RecordingOpener(status=503, body=SECRET_VALUE.encode("utf-8"))},
            ),
            "unexpected media type": (
                PlannedRequest(url="https://api.keepa.com/product", headers=credential_header),
                {"opener": RecordingOpener(content_type="image/png")},
            ),
            "second header names an undeclared credential": (
                PlannedRequest(
                    url="https://api.keepa.com/product",
                    headers=credential_header + (("X-Extra", "{{credential:UNDECLARED_TOKEN}}"),),
                ),
                {},
            ),
            "url carries the resolved secret": (
                PlannedRequest(
                    url=f"https://api.keepa.com/product?api_key={SECRET_VALUE}",
                    headers=credential_header,
                ),
                {"opener": RecordingOpener(error=OSError(f"boom {SECRET_VALUE}"))},
            ),
        }

    def test_no_resolved_secret_appears_in_any_rendered_failure(self):
        for label, (planned, overrides) in self.failure_cases().items():
            with self.subTest(failure=label):
                self.transport.reset_registered_secrets()
                self.opener = overrides.pop("opener", RecordingOpener())
                with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                    self.execute(planned, **overrides)
                rendered = rendered_error_text(ctx.exception)
                self.assertNotIn(SECRET_VALUE, rendered)
                self.assertTrue(ctx.exception.error_code)

    def test_a_secret_registered_by_one_request_is_redacted_for_the_whole_command(self):
        planned = PlannedRequest(
            url="https://api.keepa.com/product",
            headers=(("Authorization", f"Bearer {{{{credential:{DECLARED_CREDENTIAL}}}}}"),),
        )
        self.execute(planned)

        self.opener = RecordingOpener(error=OSError(f"later failure carrying {SECRET_VALUE}"))
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(PlannedRequest(url="https://api.keepa.com/other"))

        self.assertNotIn(SECRET_VALUE, rendered_error_text(ctx.exception))


class PlannedRequestShapeTests(ExecutorTestCase):
    def test_post_carries_its_body_and_get_may_not_have_one(self):
        payload = b'{"asin":"B01"}'

        self.execute(PlannedRequest(url="https://api.keepa.com/query", method="POST", body=payload))
        self.assertEqual("POST", self.opener.request.get_method())
        self.assertEqual(payload, self.opener.request.data)

        self.opener = RecordingOpener()
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(PlannedRequest(url="https://api.keepa.com/query", method="GET", body=payload))
        self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
        self.assertEqual([], self.opener.requests)

    def test_unsupported_methods_and_schemes_are_refused_before_the_declaration_check(self):
        for planned in (
            PlannedRequest(url="https://api.keepa.com/product", method="DELETE"),
            PlannedRequest(url="http://api.keepa.com/product"),
            PlannedRequest(url="ftp://api.keepa.com/product"),
            PlannedRequest(url="   "),
            PlannedRequest(url="https://api.keepa.com:notaport/product"),
            PlannedRequest(url="https://api.keepa.com\n.attacker.test/product"),
            PlannedRequest(url="https://api.keepa.com/pro duct"),
        ):
            with self.subTest(url=planned.url, method=planned.method):
                self.opener = RecordingOpener()
                with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                    self.execute(planned)
                self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
                self.assertEqual([], self.resolver.calls)
                self.assertEqual([], self.opener.requests)

    def test_malformed_headers_are_refused(self):
        for label, headers in (
            ("empty value", (("Accept", ""),)),
            ("empty name", (("", "application/json"),)),
            ("non-string value", (("Accept", 7),)),
            ("header injection", (("Accept", "application/json\r\nX-Evil: 1"),)),
            ("invalid token name", (("Accept Encoding", "gzip"),)),
            ("duplicate name", (("Accept", "application/json"), ("accept", "text/plain"))),
            ("transport-owned name", (("Content-Length", "12"),)),
            ("not a pair sequence", ("Accept: application/json",)),
        ):
            with self.subTest(headers=label):
                self.opener = RecordingOpener()
                with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                    self.execute(PlannedRequest(url="https://api.keepa.com/product", headers=headers))
                self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
                self.assertEqual([], self.opener.requests)

    def test_a_plan_object_whose_attribute_raises_is_refused_not_crashed(self):
        class HostilePlan:
            url = "https://api.keepa.com/product"
            method = "GET"
            headers = ()
            body = None

            @property
            def timeout_hint(self):
                raise RuntimeError("plugin bug")

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(HostilePlan())

        self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
        self.assertEqual([], self.opener.requests)

    def test_oversized_response_is_refused_with_the_acquisition_size_bound(self):
        self.opener = RecordingOpener(body=b"x" * 64)

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(PlannedRequest(url="https://api.keepa.com/product"), max_bytes=16)

        self.assertEqual("ACQUISITION_CONTENT_TOO_LARGE", ctx.exception.error_code)

    def test_oversized_planned_body_is_refused_before_it_is_sent(self):
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.execute(
                PlannedRequest(url="https://api.keepa.com/query", method="POST", body=b"x" * 64),
                max_bytes=16,
            )

        self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
        self.assertEqual([], self.opener.requests)


class PlannedTimeoutTests(ExecutorTestCase):
    def test_timeout_hint_can_tighten_but_never_widen_the_callers_timeout(self):
        for hint, timeout, expected in ((5.0, 30.0, 5.0), (120.0, 30.0, 30.0), (None, 30.0, 30.0)):
            with self.subTest(timeout_hint=hint):
                self.opener = RecordingOpener()
                self.execute(
                    PlannedRequest(url="https://api.keepa.com/product", timeout_hint=hint),
                    timeout=timeout,
                )
                self.assertEqual([expected], self.opener.timeouts)

    def test_invalid_timeout_hint_is_refused(self):
        for hint in (0, -1, float("inf"), "5", True):
            with self.subTest(timeout_hint=hint):
                self.opener = RecordingOpener()
                with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                    self.execute(PlannedRequest(url="https://api.keepa.com/product", timeout_hint=hint))
                self.assertEqual("PROVIDER_PLAN_INVALID", ctx.exception.error_code)
                self.assertEqual([], self.opener.requests)


class BuiltinTransportRegressionTests(ExecutorTestCase):
    def test_bounded_download_still_builds_a_plain_get_request(self):
        opener = RecordingOpener(body=b"<html>ok</html>", content_type="text/html")

        result = self.transport.bounded_download(
            "https://example.org/page.html",
            allowed_domains=["example.org"],
            max_bytes=64,
            opener=opener,
            resolver=self.resolver,
            expected_content_types=["text/html"],
        )

        self.assertEqual(b"<html>ok</html>", result.content)
        self.assertEqual("GET", opener.request.get_method())
        self.assertIsNone(opener.request.data)


if __name__ == "__main__":
    unittest.main()
