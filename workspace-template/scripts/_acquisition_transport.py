#!/usr/bin/env python3
"""Bounded HTTPS acquisition helpers shared by provider adapters."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import socket
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPSConnection
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

Resolver = Callable[[str, "int | None"], list[tuple]]

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CHUNK_SIZE = 64 * 1024
DEFAULT_USER_AGENT = "evidence-wiki acquisition/1.0"
DEFAULT_MAX_REDIRECTS = 5
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|auth|authorization|credential|password|secret|signature|token)(?:$|[_-])",
    re.IGNORECASE,
)
_URL_IN_TEXT = re.compile(r"https://[^\s(){}<>\"']+")
_SECRET_ENV_NAMES = (
    "OPENALEX_API_KEY",
    "GITHUB_TOKEN",
)
# Credential names/values declared by a registered provider and resolved during this
# command. Built-in providers know their secret env vars at import time
# (_SECRET_ENV_NAMES above); a registered provider's are only known once its declaration
# is read, so they are registered here and consulted by redact_diagnostic exactly like
# the built-in names. Empty unless a registered provider resolved a credential, which
# keeps every built-in diagnostic byte-identical.
_REGISTERED_SECRET_ENV_NAMES: list[str] = []
_REGISTERED_SECRET_VALUES: list[str] = []

CREDENTIAL_PLACEHOLDER_RE = re.compile(r"\{\{credential:([A-Z][A-Z0-9_]*)\}\}")
CREDENTIAL_PLACEHOLDER_PREFIX = "{{credential:"
PLANNED_REQUEST_METHODS = ("GET", "POST")
# A registered provider declares domains and credentials, not media types; the executor
# accepts the API payload families acquisition already promotes and leaves narrowing to
# the caller's expected_content_types argument.
PLANNED_REQUEST_CONTENT_TYPES = ("application/*", "text/*")
# Headers the transport owns. A plan that sets them could desynchronise the framing of
# the request the package is executing on the provider's behalf.
RESERVED_PLANNED_HEADER_NAMES = ("host", "content-length", "transfer-encoding")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
# urlsplit silently drops tabs and newlines, so a planned URL carrying them would be
# validated as one string and dialled as another. Refuse them at the shape check.
_URL_FORBIDDEN_CHARACTERS = re.compile(r"[\x00-\x20\x7f]")


class AcquisitionTransportError(Exception):
    """Structured transport failure with stable machine-readable code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.remediation = remediation


@dataclass(frozen=True)
class DownloadResult:
    content: bytes
    final_url: str
    byte_count: int
    checksum: str
    http_status: int | None
    content_type: str | None
    redirect_chain: list[str]
    tls_verified: bool
    insecure_tls_reason: str | None = None


def pinned_public_addresses(
    host: str,
    pins: dict[str, tuple[str, ...]],
    *,
    resolver: Resolver,
) -> tuple[str, ...]:
    """Resolve a host once per opener and retain only the validated public answers."""
    normalized = normalize_host(host)
    addresses = pins.get(normalized)
    if addresses is None:
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            addresses = resolved_hostname_addresses(normalized, resolver=resolver)
        else:
            if not is_public_hostname(normalized):
                raise AcquisitionTransportError(
                    "ACQUISITION_URL_UNSAFE",
                    f"Acquisition URL host is not public: {normalized}",
                )
            addresses = (normalized,)
        pins[normalized] = addresses
    return addresses


class PinnedHTTPSConnection(HTTPSConnection):
    """HTTPS connection whose TCP target is a previously validated DNS answer."""

    def __init__(self, host: str, *, pins: dict[str, tuple[str, ...]], resolver: Resolver, **kwargs: Any) -> None:
        self._address_pins = pins
        self._resolver = resolver
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        addresses = pinned_public_addresses(self.host, self._address_pins, resolver=self._resolver)
        last_error: OSError | None = None
        sock = None
        for address in addresses:
            try:
                sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
                break
            except OSError as exc:
                last_error = exc
        if sock is None:
            raise last_error or OSError("no validated acquisition address was connectable")
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, *, pins: dict[str, tuple[str, ...]], resolver: Resolver) -> None:
        super().__init__()
        self._pins = pins
        self._resolver = resolver

    def https_open(self, request: Request):
        return self.do_open(
            lambda host, **kwargs: PinnedHTTPSConnection(
                host,
                pins=self._pins,
                resolver=self._resolver,
                **kwargs,
            ),
            request,
        )


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def result_from_bytes(
    payload: bytes,
    *,
    url: str,
    content_type: str | None = None,
    http_status: int | None = None,
    redirect_chain: list[str] | None = None,
    tls_verified: bool = True,
    insecure_tls_reason: str | None = None,
) -> DownloadResult:
    return DownloadResult(
        content=payload,
        final_url=url,
        byte_count=len(payload),
        checksum=sha256_bytes(payload),
        http_status=http_status,
        content_type=content_type,
        redirect_chain=list(redirect_chain or []),
        tls_verified=tls_verified,
        insecure_tls_reason=insecure_tls_reason,
    )


def redact_url(url: str) -> str:
    """Return a diagnostic-safe URL with credentials and query secrets removed."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{host}{port}"
        query = urlencode(
            [
                (key, "[REDACTED]" if _SENSITIVE_QUERY_KEY.search(key) else value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return "[invalid URL redacted]"


def redact_diagnostic(value: object, *, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """Redact known secret values and credential-bearing URLs in diagnostics."""
    text = str(value)
    secret_values = list(secrets)
    for name in (*_SECRET_ENV_NAMES, *_REGISTERED_SECRET_ENV_NAMES):
        candidate = os.environ.get(name)
        if isinstance(candidate, str) and candidate:
            secret_values.append(candidate)
    secret_values.extend(_REGISTERED_SECRET_VALUES)
    for secret in secret_values:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), text)


def register_secret_value(value: str) -> None:
    """Register a resolved secret so every later diagnostic redacts it."""
    if isinstance(value, str) and value and value not in _REGISTERED_SECRET_VALUES:
        _REGISTERED_SECRET_VALUES.append(value)


def register_secret_env_name(name: str) -> None:
    """Register a declared credential env-var name for env-sourced redaction."""
    if (
        isinstance(name, str)
        and name
        and name not in _SECRET_ENV_NAMES
        and name not in _REGISTERED_SECRET_ENV_NAMES
    ):
        _REGISTERED_SECRET_ENV_NAMES.append(name)


def registered_secret_values() -> tuple[str, ...]:
    """Return the secrets registered for redaction during this command."""
    return tuple(_REGISTERED_SECRET_VALUES)


def registered_secret_env_names() -> tuple[str, ...]:
    """Return the declared credential names registered during this command."""
    return tuple(_REGISTERED_SECRET_ENV_NAMES)


def reset_registered_secrets() -> None:
    """Drop the per-command registry; the process ends the redaction lifetime."""
    _REGISTERED_SECRET_ENV_NAMES.clear()
    _REGISTERED_SECRET_VALUES.clear()


def normalize_host(host: str | None) -> str:
    if not isinstance(host, str) or not host.strip():
        raise AcquisitionTransportError(
            "ACQUISITION_URL_UNSAFE",
            "Acquisition URL must include a non-empty hostname.",
            remediation="Use an absolute HTTPS URL with a public hostname.",
        )
    return host.strip().strip("[]").lower()


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_public_address(address.ipv4_mapped)
    return address.is_global and not address.is_multicast


def is_public_hostname(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return _is_public_address(address)


def resolved_hostname_addresses(host: str, *, resolver: Resolver = socket.getaddrinfo) -> tuple[str, ...]:
    """Resolve and validate every DNS answer, failing closed on uncertainty."""
    try:
        resolved = resolver(host, None)
    except (OSError, UnicodeError, ValueError) as exc:
        raise AcquisitionTransportError(
            "ACQUISITION_DNS_FAILED",
            f"DNS resolution failed for acquisition host {host!r}: {redact_diagnostic(exc)}",
            remediation="Retry after DNS is healthy or acquire the source manually after review.",
        ) from None
    addresses: list[str] = []
    for entry in resolved or []:
        try:
            sockaddr = entry[4] if len(entry) > 4 else None
            candidate = sockaddr[0] if sockaddr else None
        except (IndexError, TypeError):
            candidate = None
        if not isinstance(candidate, str):
            raise AcquisitionTransportError(
                "ACQUISITION_DNS_FAILED",
                f"DNS resolution returned an invalid address for acquisition host {host!r}.",
                remediation="Retry after DNS is healthy or acquire the source manually after review.",
            )
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            raise AcquisitionTransportError(
                "ACQUISITION_DNS_FAILED",
                f"DNS resolution returned an invalid address for acquisition host {host!r}.",
                remediation="Retry after DNS is healthy or acquire the source manually after review.",
            ) from None
        if not _is_public_address(address):
            raise AcquisitionTransportError(
                "ACQUISITION_URL_UNSAFE",
                f"Acquisition host {host!r} resolves to a non-public address.",
                remediation="Use a source host whose complete DNS answer set contains only public addresses.",
            )
        addresses.append(str(address))
    if not addresses:
        raise AcquisitionTransportError(
            "ACQUISITION_DNS_FAILED",
            f"DNS resolution returned no addresses for acquisition host {host!r}.",
            remediation="Retry after DNS is healthy or acquire the source manually after review.",
        )
    return tuple(addresses)


def resolved_hostname_is_public(host: str, *, resolver: Resolver = socket.getaddrinfo) -> bool:
    """Compatibility predicate; strict callers should use URL validation directly."""
    try:
        resolved_hostname_addresses(host, resolver=resolver)
    except AcquisitionTransportError:
        return False
    return True


def domain_allowed(host: str, allowed_domains: list[str] | tuple[str, ...] | None) -> bool:
    if not allowed_domains:
        return True
    normalized_allowed = [normalize_host(domain) for domain in allowed_domains]
    return any(host == domain or host.endswith(f".{domain}") for domain in normalized_allowed)


def validate_https_url(
    url: str,
    *,
    allowed_domains: list[str] | tuple[str, ...] | None = None,
    error_code: str = "ACQUISITION_URL_UNSAFE",
    resolve_hostnames: bool = True,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise AcquisitionTransportError(
            error_code,
            f"Acquisition URL must be HTTPS with a public hostname: {redact_url(url)}",
            remediation="Use an HTTPS URL from an explicitly reviewed source.",
        )
    if parsed.username or parsed.password:
        raise AcquisitionTransportError(
            error_code,
            "Acquisition URL must not include username or password components.",
            remediation="Remove credentials from the URL and pass secrets only through supported environment variables.",
        )
    host = normalize_host(parsed.hostname)
    if not is_public_hostname(host):
        raise AcquisitionTransportError(
            error_code,
            f"Acquisition URL host is not public: {host}",
            remediation="Use a public source URL; internal, loopback, and link-local hosts are refused.",
        )
    if not domain_allowed(host, allowed_domains):
        raise AcquisitionTransportError(
            "ACQUISITION_DOMAIN_NOT_ALLOWED" if error_code != "ACQUISITION_REDIRECT_UNSAFE" else error_code,
            f"Acquisition URL host {host!r} is not in the configured allowed domains.",
            remediation="Add the reviewed domain to integrations.acquisition.web.allowed_domains or choose another URL.",
        )
    if resolve_hostnames:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            try:
                resolved_hostname_addresses(host, resolver=resolver)
            except AcquisitionTransportError as exc:
                if error_code == "ACQUISITION_REDIRECT_UNSAFE" and exc.error_code == "ACQUISITION_URL_UNSAFE":
                    raise AcquisitionTransportError(error_code, exc.message, remediation=exc.remediation) from None
                raise
    return host


class ValidatingRedirectHandler(HTTPRedirectHandler):
    def __init__(
        self,
        allowed_domains: list[str] | tuple[str, ...] | None,
        redirect_chain: list[str],
        *,
        resolve_hostnames: bool = True,
        resolver: Resolver = socket.getaddrinfo,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        super().__init__()
        self.allowed_domains = allowed_domains
        self.redirect_chain = redirect_chain
        self.resolve_hostnames = resolve_hostnames
        self.resolver = resolver
        self.max_redirects = max_redirects

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if len(self.redirect_chain) >= self.max_redirects:
            raise AcquisitionTransportError(
                "ACQUISITION_REDIRECT_LIMIT",
                f"Acquisition exceeded the redirect limit of {self.max_redirects}.",
                remediation="Use the canonical final HTTPS URL or review the redirect chain manually.",
            )
        validate_https_url(
            newurl,
            allowed_domains=self.allowed_domains,
            error_code="ACQUISITION_REDIRECT_UNSAFE",
            resolve_hostnames=self.resolve_hostnames,
            resolver=self.resolver,
        )
        self.redirect_chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def header_value(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name) if hasattr(headers, "get") else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def content_length(headers: Any) -> int | None:
    value = header_value(headers, "Content-Length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def require_positive_limit(max_bytes: int) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise AcquisitionTransportError(
            "CONFIG_INVALID",
            "Acquisition max_bytes must be a positive integer.",
            remediation="Set the provider byte cap to a positive integer.",
        )


def require_positive_timeout(timeout: float) -> None:
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise AcquisitionTransportError(
            "CONFIG_INVALID",
            "Acquisition timeout must be a positive finite number.",
            remediation="Set the provider timeout to a positive finite number of seconds.",
        )


def normalize_content_type(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type or None


def content_type_allowed(content_type: str | None, expected: tuple[str, ...] | list[str] | None) -> bool:
    if not expected:
        return True
    actual = normalize_content_type(content_type)
    if actual is None:
        return False
    for candidate in expected:
        normalized = normalize_content_type(candidate)
        if normalized == actual:
            return True
        if normalized and normalized.endswith("/*") and actual.startswith(normalized[:-1]):
            return True
    return False


def require_expected_content_types(expected_content_types: tuple[str, ...] | list[str] | None) -> None:
    if not expected_content_types or any(normalize_content_type(value) is None for value in expected_content_types):
        raise AcquisitionTransportError(
            "CONFIG_INVALID",
            "Acquisition must declare at least one expected response media type.",
            remediation="Configure the provider's reviewed response media types before acquisition.",
        )


def validate_response_metadata(
    *,
    status: int | None,
    content_type: str | None,
    expected_content_types: tuple[str, ...] | list[str] | None,
) -> None:
    require_expected_content_types(expected_content_types)
    if status is None or status < 200 or status > 299:
        label = "missing" if status is None else str(status)
        raise AcquisitionTransportError(
            "ACQUISITION_STATUS_UNEXPECTED",
            f"Acquisition response status is not successful: {label}.",
            remediation="Use a source URL that returns a successful 2xx response.",
        )
    if not content_type_allowed(content_type, expected_content_types):
        actual = normalize_content_type(content_type) or "missing"
        expected = ", ".join(expected_content_types or ())
        raise AcquisitionTransportError(
            "ACQUISITION_MIME_UNEXPECTED",
            f"Acquisition response media type {actual!r} is not allowed; expected one of: {expected}.",
            remediation="Use a source endpoint that serves the expected content type; do not promote an error page.",
        )


def validate_download_result(
    result: DownloadResult,
    *,
    source_url: str,
    allowed_domains: list[str] | tuple[str, ...] | None,
    max_bytes: int,
    expected_content_types: tuple[str, ...] | list[str],
    resolve_hostnames: bool = True,
    resolver: Resolver = socket.getaddrinfo,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> DownloadResult:
    """Apply the promotion policy to a completed or injected download result."""
    require_positive_limit(max_bytes)
    require_expected_content_types(expected_content_types)
    final_code = "ACQUISITION_REDIRECT_UNSAFE" if result.final_url != source_url else "ACQUISITION_URL_UNSAFE"
    validate_https_url(
        result.final_url,
        allowed_domains=allowed_domains,
        error_code=final_code,
        resolve_hostnames=resolve_hostnames,
        resolver=resolver,
    )
    if len(result.redirect_chain) > max_redirects:
        raise AcquisitionTransportError(
            "ACQUISITION_REDIRECT_LIMIT",
            f"Acquisition exceeded the redirect limit of {max_redirects}.",
            remediation="Use the canonical final HTTPS URL or review the redirect chain manually.",
        )
    for redirect_url in result.redirect_chain:
        validate_https_url(
            redirect_url,
            allowed_domains=allowed_domains,
            error_code="ACQUISITION_REDIRECT_UNSAFE",
            resolve_hostnames=resolve_hostnames,
            resolver=resolver,
        )
    if not result.tls_verified or result.insecure_tls_reason is not None:
        raise AcquisitionTransportError(
            "ACQUISITION_TLS_FAILED",
            "Acquisition refused a response that was not obtained with verified TLS.",
            remediation="Use an endpoint with a valid, trusted TLS certificate chain.",
        )
    if (
        result.byte_count != len(result.content)
        or result.checksum != sha256_bytes(result.content)
        or result.byte_count > max_bytes
    ):
        raise AcquisitionTransportError(
            "ACQUISITION_CONTENT_TOO_LARGE" if len(result.content) > max_bytes else "ACQUISITION_RESPONSE_INVALID",
            "Acquisition response byte metadata is invalid or exceeds the configured limit.",
            remediation="Retry through the bounded transport and do not retain partial or oversized bytes.",
        )
    validate_response_metadata(
        status=result.http_status,
        content_type=result.content_type,
        expected_content_types=expected_content_types,
    )
    return result


def read_bounded_response(response: Any, *, max_bytes: int, source_url: str) -> bytes:
    declared = content_length(getattr(response, "headers", None))
    if declared is not None and declared > max_bytes:
        raise AcquisitionTransportError(
            "ACQUISITION_CONTENT_TOO_LARGE",
            f"Acquisition response declares {declared} bytes, exceeding the configured limit of {max_bytes}.",
            remediation="Raise the reviewed byte cap or acquire a smaller source artifact.",
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(DEFAULT_CHUNK_SIZE, max_bytes + 1 - total))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise AcquisitionTransportError(
                "ACQUISITION_RESPONSE_INVALID",
                f"Acquisition transport returned a non-byte chunk for {redact_url(source_url)}.",
                remediation="Fix the transport adapter and retry.",
            )
        total += len(chunk)
        if total > max_bytes:
            raise AcquisitionTransportError(
                "ACQUISITION_CONTENT_TOO_LARGE",
                f"Acquisition response exceeded the configured limit of {max_bytes} bytes.",
                remediation="Raise the reviewed byte cap or acquire a smaller source artifact.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def response_url(response: Any, fallback: str) -> str:
    if hasattr(response, "geturl"):
        value = response.geturl()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def response_status(response: Any) -> int | None:
    if hasattr(response, "getcode"):
        value = response.getcode()
    else:
        value = getattr(response, "status", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_default_opener(
    *,
    allowed_domains: list[str] | tuple[str, ...] | None,
    redirect_chain: list[str],
    insecure_tls_reason: str | None = None,
    resolve_hostnames: bool = True,
    resolver: Resolver = socket.getaddrinfo,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
):
    if insecure_tls_reason is not None:
        raise AcquisitionTransportError(
            "ACQUISITION_TLS_FAILED",
            "Automated acquisition cannot disable TLS certificate verification.",
            remediation="Use an endpoint with a valid, trusted TLS certificate chain.",
        )
    pins: dict[str, tuple[str, ...]] = {}
    handlers: list[Any] = [
        ProxyHandler({}),
        PinnedHTTPSHandler(pins=pins, resolver=resolver),
        ValidatingRedirectHandler(
            allowed_domains,
            redirect_chain,
            resolve_hostnames=resolve_hostnames,
            resolver=resolver,
            max_redirects=max_redirects,
        )
    ]
    opener = build_opener(*handlers)
    return lambda request, timeout: opener.open(request, timeout=timeout)


def bounded_download(
    url: str,
    *,
    allowed_domains: list[str] | tuple[str, ...] | None = None,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
    insecure_tls_reason: str | None = None,
    opener: Callable[[Request, float], Any] | None = None,
    resolve_hostnames: bool = True,
    resolver: Resolver = socket.getaddrinfo,
    expected_content_types: tuple[str, ...] | list[str],
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    data: bytes | None = None,
    method: str | None = None,
) -> DownloadResult:
    require_positive_limit(max_bytes)
    require_positive_timeout(timeout)
    require_expected_content_types(expected_content_types)
    if insecure_tls_reason is not None:
        raise AcquisitionTransportError(
            "ACQUISITION_TLS_FAILED",
            "Automated acquisition cannot disable TLS certificate verification.",
            remediation="Use an endpoint with a valid, trusted TLS certificate chain.",
        )
    validate_https_url(url, allowed_domains=allowed_domains, resolve_hostnames=resolve_hostnames, resolver=resolver)
    redirect_chain: list[str] = []
    opener_fn = opener or build_default_opener(
        allowed_domains=allowed_domains,
        redirect_chain=redirect_chain,
        insecure_tls_reason=insecure_tls_reason,
        resolve_hostnames=resolve_hostnames,
        resolver=resolver,
        max_redirects=max_redirects,
    )
    request_headers = {"User-Agent": DEFAULT_USER_AGENT}
    request_headers.update(headers or {})
    # data/method default to None, which is exactly what Request() assumes when they are
    # omitted, so the built-in GET path is unchanged.
    request = Request(  # noqa: S310 - validated HTTPS URL plus domain policy
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with opener_fn(request, timeout) as response:
            final_url = response_url(response, url)
            if final_url != url:
                validate_https_url(
                    final_url,
                    allowed_domains=allowed_domains,
                    error_code="ACQUISITION_REDIRECT_UNSAFE",
                    resolve_hostnames=resolve_hostnames,
                    resolver=resolver,
                )
                if final_url not in redirect_chain:
                    redirect_chain.append(final_url)
            status = response_status(response)
            response_content_type = header_value(getattr(response, "headers", None), "Content-Type")
            validate_response_metadata(
                status=status,
                content_type=response_content_type,
                expected_content_types=expected_content_types,
            )
            payload = read_bounded_response(response, max_bytes=max_bytes, source_url=final_url)
            result = result_from_bytes(
                payload,
                url=final_url,
                content_type=response_content_type,
                http_status=status,
                redirect_chain=redirect_chain,
            )
            return validate_download_result(
                result,
                source_url=url,
                allowed_domains=allowed_domains,
                max_bytes=max_bytes,
                expected_content_types=expected_content_types,
                resolve_hostnames=resolve_hostnames,
                resolver=resolver,
                max_redirects=max_redirects,
            )
    except AcquisitionTransportError:
        raise
    except ssl.SSLError as exc:
        raise AcquisitionTransportError(
            "ACQUISITION_TLS_FAILED",
            f"TLS verification failed for {redact_url(url)}: {redact_diagnostic(exc)}",
            remediation="Use an endpoint with a valid, trusted TLS certificate chain.",
        ) from None
    except HTTPError as exc:
        try:
            exc.close()
        except Exception:  # noqa: S110 - best-effort cleanup before raising the transport error.
            pass
        raise AcquisitionTransportError(
            "ACQUISITION_NETWORK_ERROR",
            f"Acquisition request failed with HTTP {exc.code}: {redact_url(url)}",
            remediation="Retry later, check the URL, or lower request volume.",
        ) from None
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, ssl.SSLError):
            raise AcquisitionTransportError(
                "ACQUISITION_TLS_FAILED",
                f"TLS verification failed for {redact_url(url)}: {redact_diagnostic(reason)}",
                remediation="Use an endpoint with a valid, trusted TLS certificate chain.",
            ) from None
        raise AcquisitionTransportError(
            "ACQUISITION_NETWORK_ERROR",
            f"Acquisition request failed for {redact_url(url)}: {redact_diagnostic(exc)}",
            remediation="Retry later, check network access, or lower request volume.",
        ) from None
    except (TimeoutError, OSError) as exc:
        raise AcquisitionTransportError(
            "ACQUISITION_NETWORK_ERROR",
            f"Acquisition request failed for {redact_url(url)}: {redact_diagnostic(exc)}",
            remediation="Retry later, check network access, or lower request volume.",
        ) from None


# --- Registered-provider transport executor ----------------------------------
#
# A registered provider plans HTTPS requests and interprets responses; this package
# executes them. Every guarantee the built-in providers already carry -- DNS pinning,
# public-address enforcement, redirect validation, size bounds, diagnostic redaction --
# is inherited here rather than re-implemented, and the provider's declared
# allowed_domains is checked with the same validate_https_url call arXiv uses. The
# declaration, not convention, is the boundary.


@dataclass(frozen=True)
class ValidatedPlannedRequest:
    """A provider plan whose shape has been checked before any network work."""

    url: str
    method: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    timeout_hint: float | None


def plan_invalid(message: str, *, remediation: str) -> AcquisitionTransportError:
    """Build a PROVIDER_PLAN_INVALID error whose text is redacted before it escapes."""
    return AcquisitionTransportError(
        "PROVIDER_PLAN_INVALID",
        redact_diagnostic(message),
        remediation=remediation,
    )


def domain_not_declared(message: str, *, remediation: str) -> AcquisitionTransportError:
    """Build an ACQUISITION_DOMAIN_NOT_DECLARED error with redacted text."""
    return AcquisitionTransportError(
        "ACQUISITION_DOMAIN_NOT_DECLARED",
        redact_diagnostic(message),
        remediation=remediation,
    )


def plan_attribute(planned: Any, name: str, default: Any) -> Any:
    """Read one plan attribute; provider objects are untrusted, so a raising one refuses."""
    try:
        return getattr(planned, name, default)
    except Exception:
        # A descriptor that raises is an invalid plan, never a crashed command.
        raise plan_invalid(
            f"Planned request attribute {name!r} could not be read.",
            remediation="Return a plain PlannedRequest whose fields are already computed.",
        ) from None


_HEADER_SHAPE_MESSAGE = "Planned request headers must be a sequence of (name, value) pairs."
_HEADER_SHAPE_REMEDIATION = "Return headers as a tuple of (name, value) string pairs."


def validated_header_pairs(headers: Any) -> tuple[tuple[str, str], ...]:
    """Return planned headers as checked (name, value) pairs, or refuse the plan."""
    if headers is None:
        return ()
    if isinstance(headers, (str, bytes, bytearray)):
        raise plan_invalid(_HEADER_SHAPE_MESSAGE, remediation=_HEADER_SHAPE_REMEDIATION)
    try:
        items: Any = list(headers.items()) if hasattr(headers, "items") else list(headers)
    except Exception:
        raise plan_invalid(_HEADER_SHAPE_MESSAGE, remediation=_HEADER_SHAPE_REMEDIATION) from None
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, (str, bytes, bytearray)):
            raise plan_invalid(_HEADER_SHAPE_MESSAGE, remediation=_HEADER_SHAPE_REMEDIATION)
        try:
            name, value = item
        except Exception:
            raise plan_invalid(_HEADER_SHAPE_MESSAGE, remediation=_HEADER_SHAPE_REMEDIATION) from None
        if not isinstance(name, str) or not name.strip():
            raise plan_invalid(
                "Planned request header names must be non-empty strings.",
                remediation="Drop the empty header or give it a name.",
            )
        if not isinstance(value, str) or not value.strip():
            raise plan_invalid(
                f"Planned request header {str(name).strip()!r} must have a non-empty string value.",
                remediation="Drop the empty header or give it a value.",
            )
        name = name.strip()
        value = value.strip()
        if not _HEADER_NAME_RE.match(name):
            raise plan_invalid(
                f"Planned request header name is not a valid HTTP header token: {name!r}",
                remediation="Use a header name made of HTTP token characters only.",
            )
        if _CONTROL_CHARACTERS.search(value):
            raise plan_invalid(
                f"Planned request header {name!r} value contains control characters.",
                remediation="Remove control characters from the planned header value.",
            )
        lowered = name.lower()
        if lowered in RESERVED_PLANNED_HEADER_NAMES:
            raise plan_invalid(
                f"Planned request header {name!r} is set by the package transport and cannot be planned.",
                remediation=f"Remove {name} from the plan; the transport sets it.",
            )
        if lowered in seen:
            raise plan_invalid(
                f"Planned request repeats header {name!r}.",
                remediation="Send each header name at most once per planned request.",
            )
        seen.add(lowered)
        pairs.append((name, value))
    return tuple(pairs)


def validate_planned_request(planned: Any) -> ValidatedPlannedRequest:
    """Shape-check a provider plan; nothing else runs until this passes."""
    url = plan_attribute(planned, "url", None)
    if not isinstance(url, str) or not url.strip():
        raise plan_invalid(
            "Planned request url must be a non-empty string.",
            remediation="Return an absolute https:// URL from the provider plan.",
        )
    url = url.strip()
    if _URL_FORBIDDEN_CHARACTERS.search(url):
        raise plan_invalid(
            "Planned request url must not contain whitespace or control characters.",
            remediation="Percent-encode the URL before returning it from the provider plan.",
        )
    if CREDENTIAL_PLACEHOLDER_PREFIX in url:
        raise plan_invalid(
            "Planned request url must not embed a credential placeholder; credentials belong in header values.",
            remediation="Move the credential into a request header value planned as {{credential:NAME}}.",
        )
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise plan_invalid(
            "Planned request url is not a parseable URL.",
            remediation="Return an absolute https:// URL from the provider plan.",
        ) from None
    if parsed.scheme.lower() != "https" or not parsed.netloc or not hostname:
        raise plan_invalid(
            f"Planned request url must be an absolute https:// URL: {redact_url(url)}",
            remediation="Return an absolute https:// URL from the provider plan.",
        )
    method_value = plan_attribute(planned, "method", "GET")
    if not isinstance(method_value, str) or not method_value.strip():
        raise plan_invalid(
            "Planned request method must be a non-empty string.",
            remediation=f"Plan one of: {', '.join(PLANNED_REQUEST_METHODS)}.",
        )
    method = method_value.strip().upper()
    if method not in PLANNED_REQUEST_METHODS:
        raise plan_invalid(
            f"Planned request method {method_value.strip()!r} is not supported.",
            remediation=f"Plan one of: {', '.join(PLANNED_REQUEST_METHODS)}.",
        )
    headers = validated_header_pairs(plan_attribute(planned, "headers", ()))
    body = plan_attribute(planned, "body", None)
    if body is not None:
        if isinstance(body, bytearray):
            body = bytes(body)
        if not isinstance(body, bytes):
            raise plan_invalid(
                "Planned request body must be bytes.",
                remediation="Encode the planned request body to bytes.",
            )
        if method != "POST":
            raise plan_invalid(
                f"Planned request body is only valid on POST, not {method}.",
                remediation="Plan the request as POST or drop the body.",
            )
    timeout_hint = plan_attribute(planned, "timeout_hint", None)
    if timeout_hint is not None:
        if (
            isinstance(timeout_hint, bool)
            or not isinstance(timeout_hint, (int, float))
            or not math.isfinite(float(timeout_hint))
            or float(timeout_hint) <= 0
        ):
            raise plan_invalid(
                "Planned request timeout_hint must be a positive finite number of seconds.",
                remediation="Drop timeout_hint or set it to a positive number of seconds.",
            )
        timeout_hint = float(timeout_hint)
    return ValidatedPlannedRequest(
        url=url,
        method=method,
        headers=headers,
        body=body,
        timeout_hint=timeout_hint,
    )


def normalize_declared_domains(allowed_domains: Any) -> tuple[str, ...]:
    """Return the provider's declaration; an absent or malformed one declares nothing."""
    remediation = "Declare every reachable host in the provider's allowed_domains as a bare hostname."
    if allowed_domains is None or isinstance(allowed_domains, (str, bytes, bytearray)):
        raise domain_not_declared(
            "The provider declared no allowed_domains, so every host is outside its declaration.",
            remediation=remediation,
        )
    try:
        entries = list(allowed_domains)
    except TypeError:
        raise domain_not_declared(
            "The provider's allowed_domains declaration is not a sequence of hostnames.",
            remediation=remediation,
        ) from None
    if not entries:
        raise domain_not_declared(
            "The provider declared no allowed_domains, so every host is outside its declaration.",
            remediation=remediation,
        )
    normalized: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise domain_not_declared(
                "The provider's allowed_domains declaration contains an entry that is not a hostname.",
                remediation=remediation,
            )
        candidate = entry.strip().lower()
        if "://" in candidate or "/" in candidate or "\\" in candidate:
            raise domain_not_declared(
                f"The provider's allowed_domains entry must be a bare hostname, not a URL: {candidate!r}",
                remediation=remediation,
            )
        normalized.append(candidate)
    return tuple(normalized)


def enforce_declared_domain(
    url: str,
    declared_domains: tuple[str, ...],
    *,
    resolve_hostnames: bool = True,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Refuse a planned URL outside the declaration before any DNS or socket work."""
    try:
        return validate_https_url(
            url,
            allowed_domains=list(declared_domains),
            resolve_hostnames=resolve_hostnames,
            resolver=resolver,
        )
    except AcquisitionTransportError as exc:
        if exc.error_code != "ACQUISITION_DOMAIN_NOT_ALLOWED":
            raise
        try:
            host = normalize_host(urlsplit(url).hostname)
        except (AcquisitionTransportError, ValueError):
            host = "unknown"
        raise domain_not_declared(
            f"Planned request host {host!r} is outside the provider's declared allowed_domains "
            f"({', '.join(declared_domains)}).",
            remediation=(
                "Plan requests only within the provider's declared allowed_domains, or install a provider "
                "distribution whose declaration covers this host."
            ),
        ) from None


def declared_credential_names(capabilities_credentials: Any) -> tuple[str, ...]:
    """Return the declared credential env-var names, refusing a malformed declaration."""
    if capabilities_credentials is None:
        return ()
    if isinstance(capabilities_credentials, (str, bytes, bytearray)):
        raise plan_invalid(
            "The provider's declared credentials must be a sequence of environment variable names.",
            remediation="Declare credentials as a tuple of environment variable names.",
        )
    try:
        entries = list(capabilities_credentials)
    except TypeError:
        raise plan_invalid(
            "The provider's declared credentials must be a sequence of environment variable names.",
            remediation="Declare credentials as a tuple of environment variable names.",
        ) from None
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            raise plan_invalid(
                "The provider's declared credentials must be non-empty environment variable names.",
                remediation="Declare credentials as a tuple of environment variable names.",
            )
        names.append(entry.strip())
    return tuple(names)


def resolve_credential_placeholders(
    headers: Any,
    *,
    capabilities_credentials: Any,
    env: Mapping[str, str] | None,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Resolve {{credential:NAME}} header placeholders from declared env variables.

    Returns the resolved headers plus the secret values. Values are also registered for
    redaction here, so a refusal raised part-way through a header list cannot echo an
    already-resolved secret; they are returned so the caller can register them with any
    other diagnostic surface it owns.
    """
    declared = declared_credential_names(capabilities_credentials)
    for name in declared:
        register_secret_env_name(name)
    environment: Mapping[str, str] = env if isinstance(env, Mapping) else {}
    resolved: list[tuple[str, str]] = []
    secrets: list[str] = []
    for name, value in validated_header_pairs(headers):
        rendered = value
        for variable in dict.fromkeys(CREDENTIAL_PLACEHOLDER_RE.findall(value)):
            if variable not in declared:
                raise plan_invalid(
                    f"Planned request header {name!r} references credential {variable!r}, "
                    "which the provider did not declare.",
                    remediation=(
                        f"Declare {variable} in the provider's capability credentials, or plan a header that "
                        "only references declared credentials."
                    ),
                )
            raw = environment.get(variable)
            if not isinstance(raw, str) or not raw.strip():
                raise plan_invalid(
                    f"Declared credential {variable!r} is not set in the environment.",
                    remediation=f"Export {variable} before running acquisition through this provider.",
                )
            secret = raw.strip()
            if _CONTROL_CHARACTERS.search(secret):
                raise plan_invalid(
                    f"Declared credential {variable!r} contains control characters and cannot be sent as a header.",
                    remediation=f"Fix the value exported as {variable}.",
                )
            register_secret_value(secret)
            if secret not in secrets:
                secrets.append(secret)
            rendered = rendered.replace(f"{CREDENTIAL_PLACEHOLDER_PREFIX}{variable}}}}}", secret)
        if CREDENTIAL_PLACEHOLDER_PREFIX in rendered:
            raise plan_invalid(
                f"Planned request header {name!r} contains a malformed credential placeholder.",
                remediation="Reference credentials as {{credential:NAME}} with an uppercase variable name.",
            )
        if _CONTROL_CHARACTERS.search(rendered):
            raise plan_invalid(
                f"Planned request header {name!r} value contains control characters after credential resolution.",
                remediation="Fix the planned header or the exported credential value.",
            )
        resolved.append((name, rendered))
    return tuple(resolved), tuple(secrets)


def execute_planned_request(
    planned: Any,
    *,
    allowed_domains: list[str] | tuple[str, ...] | None,
    credentials: list[str] | tuple[str, ...] | None,
    env: Mapping[str, str] | None,
    timeout: float,
    max_bytes: int,
    opener: Callable[[Request, float], Any] | None = None,
    resolve_hostnames: bool = True,
    resolver: Resolver = socket.getaddrinfo,
    expected_content_types: tuple[str, ...] | list[str] = PLANNED_REQUEST_CONTENT_TYPES,
) -> DownloadResult:
    """Execute one provider-planned request through the package's pinned transport.

    The order is the contract: shape, then declaration, then credentials, then the
    network. A host outside the declaration is refused with
    ACQUISITION_DOMAIN_NOT_DECLARED before any DNS or socket work.
    """
    plan = validate_planned_request(planned)
    require_positive_limit(max_bytes)
    require_positive_timeout(timeout)
    if plan.body is not None and len(plan.body) > max_bytes:
        raise plan_invalid(
            f"Planned request body ({len(plan.body)} bytes) exceeds the configured limit of {max_bytes} bytes.",
            remediation="Plan a smaller request body or raise the reviewed byte cap.",
        )
    declared_domains = normalize_declared_domains(allowed_domains)
    enforce_declared_domain(
        plan.url,
        declared_domains,
        resolve_hostnames=resolve_hostnames,
        resolver=resolver,
    )
    resolved_headers, _secrets = resolve_credential_placeholders(
        plan.headers,
        capabilities_credentials=credentials,
        env=env,
    )
    # A plan may tighten the caller's timeout, never widen it.
    effective_timeout = float(timeout) if plan.timeout_hint is None else min(float(timeout), plan.timeout_hint)
    try:
        return bounded_download(
            plan.url,
            allowed_domains=list(declared_domains),
            max_bytes=max_bytes,
            timeout=effective_timeout,
            headers=dict(resolved_headers),
            opener=opener,
            resolve_hostnames=resolve_hostnames,
            resolver=resolver,
            expected_content_types=expected_content_types,
            data=plan.body,
            method=plan.method,
        )
    except AcquisitionTransportError as exc:
        # Re-raise through the redaction pass rather than chaining: a resolved credential
        # must not survive in the message, the remediation, or a chained traceback.
        raise AcquisitionTransportError(
            exc.error_code,
            redact_diagnostic(exc.message),
            remediation=redact_diagnostic(exc.remediation) if exc.remediation else None,
        ) from None
