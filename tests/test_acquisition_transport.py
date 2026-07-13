import importlib.util
import sys
import traceback
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
TRANSPORT_PATH = SCRIPTS / "_acquisition_transport.py"


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


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://example.org/page.html",
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.read_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.closed = True
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if size is None or size < 0:
            size = len(self.body)
        chunk = self.body[:size]
        self.body = self.body[size:]
        return chunk

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status


class AcquisitionTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = load_script_module("research_acquisition_transport", TRANSPORT_PATH)

    @staticmethod
    def public_resolver(_host, _port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    def test_bounded_download_streams_bytes_and_metadata(self):
        response = FakeResponse(
            b"<html>official</html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

        result = self.transport.bounded_download(
            "https://example.org/page.html",
            allowed_domains=["example.org"],
            max_bytes=64,
            opener=lambda _request, _timeout: response,
            resolver=self.public_resolver,
            expected_content_types=["text/html"],
        )

        self.assertEqual(b"<html>official</html>", result.content)
        self.assertEqual("https://example.org/page.html", result.final_url)
        self.assertEqual(21, result.byte_count)
        self.assertEqual("text/html; charset=utf-8", result.content_type)
        self.assertEqual(200, result.http_status)
        self.assertEqual([], result.redirect_chain)
        self.assertTrue(result.tls_verified)
        self.assertRegex(result.checksum, r"^sha256:[0-9a-f]{64}$")
        self.assertTrue(response.closed)

    def test_content_length_over_cap_refuses_before_read(self):
        response = FakeResponse(
            b"0123456789",
            headers={"Content-Length": "10", "Content-Type": "text/plain"},
        )

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://example.org/page.txt",
                allowed_domains=["example.org"],
                max_bytes=5,
                opener=lambda _request, _timeout: response,
                resolver=self.public_resolver,
                expected_content_types=["text/plain"],
            )

        self.assertEqual("ACQUISITION_CONTENT_TOO_LARGE", ctx.exception.error_code)
        self.assertEqual(0, response.read_calls)

    def test_stream_over_cap_refuses(self):
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://example.org/page.txt",
                allowed_domains=["example.org"],
                max_bytes=5,
                opener=lambda _request, _timeout: FakeResponse(
                    b"012345", headers={"Content-Type": "text/plain"}
                ),
                resolver=self.public_resolver,
                expected_content_types=["text/plain"],
            )

        self.assertEqual("ACQUISITION_CONTENT_TOO_LARGE", ctx.exception.error_code)

    def test_private_ip_url_is_rejected_before_open(self):
        calls = []

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://127.0.0.1/admin",
                allowed_domains=["127.0.0.1"],
                max_bytes=64,
                opener=lambda _request, _timeout: calls.append("opened"),
                resolver=self.public_resolver,
                expected_content_types=["text/html"],
            )

        self.assertEqual("ACQUISITION_URL_UNSAFE", ctx.exception.error_code)
        self.assertEqual([], calls)

    def test_unsafe_redirect_target_is_rejected(self):
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://example.org/page.html",
                allowed_domains=["example.org"],
                max_bytes=64,
                opener=lambda _request, _timeout: FakeResponse(
                    b"redirected",
                    url="https://127.0.0.1/metadata",
                ),
                resolver=self.public_resolver,
                expected_content_types=["text/html"],
            )

        self.assertEqual("ACQUISITION_REDIRECT_UNSAFE", ctx.exception.error_code)

    def test_resolve_hostnames_rejects_privately_resolving_host_by_default(self):
        def fake_resolver(_host, _port):
            return [(2, 1, 6, "", ("10.0.0.5", 0))]

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.validate_https_url(
                "https://internal.example/api",
                resolver=fake_resolver,
            )

        self.assertEqual("ACQUISITION_URL_UNSAFE", ctx.exception.error_code)

    def test_resolve_hostnames_rejects_hostname_resolving_to_private_address(self):
        def fake_resolver(_host, _port):
            return [(2, 1, 6, "", ("10.0.0.5", 0))]

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.validate_https_url(
                "https://internal.example/api",
                resolve_hostnames=True,
                resolver=fake_resolver,
            )

        self.assertEqual("ACQUISITION_URL_UNSAFE", ctx.exception.error_code)

    def test_resolve_hostnames_allows_hostname_resolving_to_public_addresses_only(self):
        def fake_resolver(_host, _port):
            return [(2, 1, 6, "", ("93.184.216.34", 0)), (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0))]

        host = self.transport.validate_https_url(
            "https://publisher.example/paper.pdf",
            resolve_hostnames=True,
            resolver=fake_resolver,
        )

        self.assertEqual("publisher.example", host)

    def test_connection_uses_cached_validated_dns_answers_without_re_resolution(self):
        calls = []

        def rebinding_resolver(_host, _port):
            calls.append("resolved")
            if len(calls) == 1:
                return [(2, 1, 6, "", ("93.184.216.34", 0))]
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        pins = {}
        first = self.transport.pinned_public_addresses("example.org", pins, resolver=rebinding_resolver)
        second = self.transport.pinned_public_addresses("example.org", pins, resolver=rebinding_resolver)

        self.assertEqual(("93.184.216.34",), first)
        self.assertEqual(first, second)
        self.assertEqual(["resolved"], calls)

    def test_default_opener_disables_environment_proxies_and_uses_pinned_https(self):
        captured = {}

        class FakeOpener:
            def open(self, *_args, **_kwargs):
                raise AssertionError("test only inspects the configured handlers")

        with mock.patch.object(
            self.transport,
            "build_opener",
            side_effect=lambda *handlers: captured.setdefault("handlers", handlers) and FakeOpener(),
        ):
            self.transport.build_default_opener(
                allowed_domains=["example.org"],
                redirect_chain=[],
                resolver=self.public_resolver,
            )

        handler_types = {type(handler).__name__ for handler in captured["handlers"]}
        self.assertIn("PinnedHTTPSHandler", handler_types)
        self.assertIn("ProxyHandler", handler_types)

    def test_pinned_https_connection_connects_to_validated_ip_with_hostname_tls(self):
        connection = self.transport.PinnedHTTPSConnection(
            "example.org",
            pins={},
            resolver=self.public_resolver,
            timeout=5,
        )
        connection._context = mock.Mock()
        raw_socket = mock.Mock()
        wrapped_socket = mock.Mock()
        connection._context.wrap_socket.return_value = wrapped_socket

        with mock.patch.object(self.transport.socket, "create_connection", return_value=raw_socket) as connect:
            connection.connect()

        connect.assert_called_once_with(("93.184.216.34", 443), 5, None)
        connection._context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="example.org")
        self.assertIs(wrapped_socket, connection.sock)

    def test_resolve_hostnames_fails_closed_when_resolution_errors(self):
        def failing_resolver(_host, _port):
            raise OSError("no dns available")

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.validate_https_url(
                "https://unresolvable.example/api",
                resolver=failing_resolver,
            )

        self.assertEqual("ACQUISITION_DNS_FAILED", ctx.exception.error_code)

    def test_resolve_hostnames_rejects_empty_malformed_and_mixed_answers(self):
        answer_sets = (
            [],
            [None],
            [(2, 1, 6, "", ("not-an-address", 0))],
            [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("169.254.169.254", 0)),
            ],
            [(2, 1, 6, "", ("100.64.0.1", 0))],
        )
        for answers in answer_sets:
            with self.subTest(answers=answers):
                with self.assertRaises(self.transport.AcquisitionTransportError):
                    self.transport.validate_https_url(
                        "https://mixed.example/api",
                        resolver=lambda _host, _port, values=answers: values,
                    )

    def test_bounded_download_rejects_redirect_to_privately_resolving_host_when_enabled(self):
        def fake_resolver(host, _port):
            if host == "safe.example":
                return [(2, 1, 6, "", ("93.184.216.34", 0))]
            return [(2, 1, 6, "", ("169.254.0.9", 0))]

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://safe.example/page.html",
                max_bytes=64,
                resolve_hostnames=True,
                resolver=fake_resolver,
                opener=lambda _request, _timeout: FakeResponse(
                    b"redirected",
                    url="https://rebind.example/x",
                ),
                expected_content_types=["text/html"],
            )

        self.assertEqual("ACQUISITION_REDIRECT_UNSAFE", ctx.exception.error_code)

    def test_non_success_status_is_rejected_before_read(self):
        response = FakeResponse(b"maintenance", status=503, headers={"Content-Type": "text/html"})

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://example.org/page.html",
                max_bytes=64,
                opener=lambda _request, _timeout: response,
                resolver=self.public_resolver,
                expected_content_types=["text/html"],
            )

        self.assertEqual("ACQUISITION_STATUS_UNEXPECTED", ctx.exception.error_code)
        self.assertEqual(0, response.read_calls)

    def test_missing_or_mismatched_mime_is_rejected_before_read(self):
        for content_type in (None, "application/json"):
            response = FakeResponse(
                b"<html>official</html>",
                headers={"Content-Type": content_type} if content_type else {},
            )
            with self.subTest(content_type=content_type):
                with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                    self.transport.bounded_download(
                        "https://example.org/page.html",
                        max_bytes=64,
                        opener=lambda _request, _timeout, value=response: value,
                        resolver=self.public_resolver,
                        expected_content_types=["text/html", "application/xhtml+xml"],
                    )
                self.assertEqual("ACQUISITION_MIME_UNEXPECTED", ctx.exception.error_code)
                self.assertEqual(0, response.read_calls)

    def test_automated_unverified_tls_path_is_refused(self):
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://example.org/page.html",
                max_bytes=64,
                insecure_tls_reason="reviewed exception",
                resolver=self.public_resolver,
                expected_content_types=["text/html"],
            )

        self.assertEqual("ACQUISITION_TLS_FAILED", ctx.exception.error_code)

    def test_invalid_timeout_is_rejected_before_open(self):
        calls = []
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://example.org/page.html",
                max_bytes=64,
                timeout=0,
                opener=lambda _request, _timeout: calls.append("opened"),
                resolver=self.public_resolver,
                expected_content_types=["text/html"],
            )

        self.assertEqual("CONFIG_INVALID", ctx.exception.error_code)
        self.assertEqual([], calls)

    def test_missing_expected_media_policy_is_rejected_before_open(self):
        calls = []
        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            self.transport.bounded_download(
                "https://example.org/page.html",
                max_bytes=64,
                expected_content_types=[],
                opener=lambda _request, _timeout: calls.append("opened"),
                resolver=self.public_resolver,
            )

        self.assertEqual("CONFIG_INVALID", ctx.exception.error_code)
        self.assertEqual([], calls)

    def test_diagnostics_redact_url_credentials_query_secrets_and_environment_secrets(self):
        secret = "transport-canary-value"
        old = self.transport.os.environ.get("OPENALEX_API_KEY")
        self.transport.os.environ["OPENALEX_API_KEY"] = secret
        try:
            with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
                self.transport.bounded_download(
                    f"https://example.org/page?api_key={secret}&view=full",
                    max_bytes=64,
                    opener=lambda _request, _timeout: (_ for _ in ()).throw(OSError(f"provider echoed {secret}")),
                    resolver=self.public_resolver,
                    expected_content_types=["text/html"],
                )
        finally:
            if old is None:
                self.transport.os.environ.pop("OPENALEX_API_KEY", None)
            else:
                self.transport.os.environ["OPENALEX_API_KEY"] = old

        self.assertNotIn(secret, ctx.exception.message)
        self.assertIn("%5BREDACTED%5D", ctx.exception.message)
        self.assertNotIn(secret, "".join(traceback.format_exception(ctx.exception)))

        credential = "userinfo-canary"
        with self.assertRaises(self.transport.AcquisitionTransportError) as credential_ctx:
            self.transport.validate_https_url(f"https://user:{credential}@example.org/page")
        self.assertNotIn(credential, credential_ctx.exception.message)

    def test_redirect_count_is_bounded(self):
        handler = self.transport.ValidatingRedirectHandler(
            ["example.org"],
            ["https://example.org/first"],
            resolver=self.public_resolver,
            max_redirects=1,
        )

        with self.assertRaises(self.transport.AcquisitionTransportError) as ctx:
            handler.redirect_request(None, None, 302, "Found", {}, "https://example.org/second")

        self.assertEqual("ACQUISITION_REDIRECT_LIMIT", ctx.exception.error_code)


if __name__ == "__main__":
    unittest.main()
