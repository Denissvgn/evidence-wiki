"""Authoring contract for third-party acquisition and discovery providers (v1).

This module is a *convenience*, not the authority.  A plugin distribution imports it to
get frozen, self-validating declaration types and two abstract base classes, so that a
mistake in a capability declaration surfaces at import time in the plugin's own test
suite instead of at registration time in somebody's workspace.

**The authority is ``workspace-template/scripts/_provider_plugins.py``.**  That script
validates every registered provider *structurally* — by the attributes, shapes, and value
rules it finds on the object — and **never** by ``isinstance`` against the classes here.
Two consequences follow, and both are intended:

- a provider written without ``evidence_wiki`` installed at all, whose class merely
  matches the shape below, is a valid registration;
- a provider that subclasses :class:`AcquisitionProvider` but declares, say, an empty
  ``allowed_domains`` is *not* valid — inheritance proves nothing.

The split exists because workspace scripts must keep running in a deployed workspace
where only the scripts exist, so they may never ``import evidence_wiki``.  If the
registry loader depended on these classes, a workspace deployed from one template
version running beside a differently-versioned installed package would turn version skew
into a live registration failure.  Duck typing makes the declaration — not the import
graph — the contract.

**Trust model (planner/executor).**  A provider is a request *planner* and a response
*interpreter*: it decides which HTTPS requests should happen and what the returned bytes
mean, and it never touches a socket.  The package's own pinned transport executes each
planned request, refusing any host outside the declared ``allowed_domains``, resolving
``{{credential:NAME}}`` placeholders from the environment (so credential *values* never
enter provider code, provenance, or diagnostics) and applying the same size bounds,
budgets, and atomic-write/quarantine discipline the built-in providers get.

Nothing in this module performs I/O: no file, network, environment, or clock access.  It
is pure declaration and validation, and it imports only the standard library.
"""

from __future__ import annotations

import abc
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

__all__ = [
    "ACQUISITION_ENTRY_POINT_GROUP",
    "CREDENTIAL_PLACEHOLDER_RE",
    "DISCOVERY_ENTRY_POINT_GROUP",
    "MAX_PLANNED_REQUESTS",
    "PROVIDER_API_VERSION",
    "AcquisitionProvider",
    "DiscoveryProvider",
    "PlannedRequest",
    "ProviderCapabilities",
    "RateLimit",
    "SourceArtifact",
]

#: Shape version of this contract.  A provider declares ``provider_api_version`` and the
#: registry loader refuses versions it does not implement.
PROVIDER_API_VERSION = 1

#: ``importlib.metadata`` entry-point groups a distribution registers under.
ACQUISITION_ENTRY_POINT_GROUP = "evidence_wiki.acquisition_providers"
DISCOVERY_ENTRY_POINT_GROUP = "evidence_wiki.discovery_providers"

#: Credential reference syntax, valid in planned-request **header values only**.  The
#: captured group is an environment variable *name*; the package resolves the value at
#: execution time and registers it for redaction.
CREDENTIAL_PLACEHOLDER_RE = re.compile(r"\{\{credential:([A-Z][A-Z0-9_]*)\}\}")

#: Upper bound on the number of planned requests the package will execute for one
#: command.  A plan fetches one artifact; it is not a crawl.
MAX_PLANNED_REQUESTS = 8

_RATE_LIMIT_WINDOWS = ("minute", "hour")
_LICENSE_INFERENCE_VALUES = ("yes", "partial", "none")
_HTTP_METHODS = ("GET", "POST")
_TERMS_PER_ORIGIN = "per-origin"
_HTTPS_PREFIX = "https://"
_CREDENTIAL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CREDENTIAL_PLACEHOLDER_PREFIX = "{{credential:"
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _fail(field_name: str, reason: str, value: Any) -> ValueError:
    """Build the house-style rejection: the field name, what is wrong, the value."""

    return ValueError(f"{field_name} {reason}: {value!r}")


def _coerce_str_tuple(value: Any, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    """Accept a list or tuple of non-empty strings and return it as a tuple.

    A bare string is refused rather than iterated: silently exploding ``"example.com"``
    into eleven single-character "domains" is the kind of accident this contract exists
    to catch.
    """

    if isinstance(value, (str, bytes, bytearray)):
        raise _fail(field_name, "must be a list or tuple of strings, not a bare string", value)
    if not isinstance(value, (list, tuple)):
        raise _fail(field_name, "must be a list or tuple of strings", value)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _fail(field_name, "entries must be non-empty strings", item)
        items.append(item)
    if not items and not allow_empty:
        raise _fail(field_name, "must not be empty", value)
    return tuple(items)


def _reject_duplicates(items: tuple[str, ...], field_name: str) -> None:
    duplicates = sorted({item for item in items if items.count(item) > 1})
    if duplicates:
        raise _fail(field_name, "must not repeat an entry", duplicates)


def _validate_allowed_domains(value: Any) -> tuple[str, ...]:
    """Bare hostnames only, lowercased — mirroring ``fetch_sources.validate_domain_list``.

    Surrounding whitespace is stripped and case is folded down (an uppercase declaration
    is accepted and normalised, never silently mismatched at the transport's host check);
    anything that looks like a URL, a path, or an embedded space is refused.
    """

    field_name = "ProviderCapabilities.allowed_domains"
    domains = _coerce_str_tuple(value, field_name)
    normalized: list[str] = []
    for domain in domains:
        candidate = domain.strip()
        if "://" in candidate or "/" in candidate or "\\" in candidate:
            raise _fail(field_name, "entries must be bare hostnames, not URLs or paths", domain)
        if any(character.isspace() for character in candidate):
            raise _fail(field_name, "entries must not contain whitespace", domain)
        normalized.append(candidate.lower())
    return tuple(normalized)


def _validate_terms_urls(value: Any) -> tuple[str, ...]:
    field_name = "ProviderCapabilities.terms_urls"
    urls = _coerce_str_tuple(value, field_name)
    for url in urls:
        if url == _TERMS_PER_ORIGIN:
            continue
        if not url.startswith(_HTTPS_PREFIX) or len(url) <= len(_HTTPS_PREFIX):
            raise _fail(
                field_name,
                f'entries must be https:// URLs or the literal "{_TERMS_PER_ORIGIN}"',
                url,
            )
    return urls


def _validate_credentials(value: Any) -> tuple[str, ...]:
    field_name = "ProviderCapabilities.credentials"
    names = _coerce_str_tuple(value, field_name, allow_empty=True)
    for name in names:
        if not _CREDENTIAL_NAME_RE.fullmatch(name):
            raise _fail(
                field_name,
                "entries must be environment variable NAMES matching ^[A-Z][A-Z0-9_]*$",
                name,
            )
    _reject_duplicates(names, field_name)
    return names


def _validate_request_kinds(value: Any) -> tuple[str, ...]:
    # v1 records declared kinds (provenance, doctor) but does not route on them.
    field_name = "ProviderCapabilities.request_kinds"
    kinds = _coerce_str_tuple(value, field_name, allow_empty=True)
    _reject_duplicates(kinds, field_name)
    return kinds


def _require_declared_true(field_name: str, value: Any, guarantee: str) -> None:
    if value is not True:
        raise ValueError(
            f"ProviderCapabilities.{field_name} must be True in provider API "
            f"v{PROVIDER_API_VERSION} ({guarantee}), so a False declaration is a contract "
            f"the package will not honour: {value!r}"
        )


def _validate_headers(value: Any) -> tuple[tuple[str, str], ...]:
    """Validate header pairs without ever echoing a header *value* in a message.

    A header value is the one place this contract invites a credential reference, and an
    author who hard-codes a secret there instead of using a placeholder would otherwise
    see it copied verbatim into a ``ValueError`` that the package reports as
    ``PROVIDER_PLAN_INVALID`` detail.  Rejections therefore name the header and its
    position; the value itself is never rendered.
    """

    field_name = "PlannedRequest.headers"
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, (list, tuple)):
        raise _fail(field_name, "must be a list or tuple of (name, value) pairs", value)
    pairs: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, (str, bytes, bytearray)) or not isinstance(item, (list, tuple)) or len(item) != 2:
            raise _fail(field_name, f"entry {index} must be a (name, value) pair", item)
        name, header_value = item
        if not isinstance(name, str) or not name.strip():
            raise _fail(field_name, f"entry {index} name must be a non-empty string", name)
        if any(character in name for character in ("\r", "\n", "\x00")):
            raise _fail(field_name, f"entry {index} name must not contain control characters", name)
        if not isinstance(header_value, str) or not header_value.strip():
            raise ValueError(
                f"{field_name} entry {index} ({name!r}) value must be a non-empty string, "
                f"got {type(header_value).__name__}"
            )
        if any(character in header_value for character in ("\r", "\n", "\x00")):
            raise ValueError(f"{field_name} entry {index} ({name!r}) value must not contain control characters")
        pairs.append((name, header_value))
    return tuple(pairs)


def _validate_filename(value: Any) -> None:
    field_name = "SourceArtifact.filename"
    if not isinstance(value, str) or not value.strip():
        raise _fail(field_name, "must be a non-empty bare filename", value)
    if "/" in value or "\\" in value:
        raise _fail(field_name, "must be a bare filename without path separators", value)
    if value in {".", ".."}:
        raise _fail(field_name, "must be a filename, not a directory reference", value)
    if ".." in value:
        raise _fail(field_name, 'must not contain ".."', value)
    if _DRIVE_PREFIX_RE.match(value):
        raise _fail(field_name, "must be a bare filename, not a drive-qualified path", value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _fail(field_name, "must not contain control characters", value)


def _validate_json_object_keys(value: Any, field_name: str, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _fail(field_name, f"keys must be strings (at {path or '<root>'})", key)
            _validate_json_object_keys(item, field_name, f"{path}.{key}" if path else key)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_object_keys(item, field_name, f"{path}[{index}]")


def _validate_provenance_metadata(value: Any) -> Mapping[str, Any]:
    """Copy the mapping and prove it survives a JSON round trip unchanged.

    The copy is shallow: nested containers still belong to the caller.  ``json.dumps``
    runs first so that a circular structure is reported as the ``ValueError`` json itself
    raises rather than as a recursion error from the key walk.
    """

    field_name = "SourceArtifact.provenance_metadata"
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping, got {type(value).__name__}")
    payload = dict(value)
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-safe: {exc}") from exc
    _validate_json_object_keys(payload, field_name)
    return MappingProxyType(payload)


@dataclass(frozen=True)
class RateLimit:
    """A ceiling the package enforces, not a hint the provider self-polices.

    ``requests`` executions per ``per`` window, accounted durably per run.
    """

    requests: int
    per: str

    def __post_init__(self) -> None:
        # ``isinstance(True, int)`` is True, so booleans have to be excluded by hand:
        # ``RateLimit(True, per="minute")`` would otherwise declare a limit of one.
        if isinstance(self.requests, bool) or not isinstance(self.requests, int):
            raise _fail("RateLimit.requests", "must be an int", self.requests)
        if self.requests < 1:
            raise _fail("RateLimit.requests", "must be >= 1", self.requests)
        if self.per not in _RATE_LIMIT_WINDOWS:
            raise _fail("RateLimit.per", f"must be one of {_RATE_LIMIT_WINDOWS}", self.per)


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider promises, in the terms the package can check.

    ``credentials`` holds environment variable **names** only.  A credential *value*
    never appears in a declaration, in a plan, in provenance, or in a diagnostic: the
    package resolves declared names from the environment at execution time and registers
    the resolved values for redaction.

    Lists are accepted anywhere a tuple is declared and are frozen into tuples, so an
    author writing ``allowed_domains=["api.example.com"]`` gets a usable object.
    """

    allowed_domains: tuple[str, ...]
    terms_urls: tuple[str, ...]
    license_inference: str
    captures_raw: bool = True
    quarantine_on_incomplete: bool = True
    rate_limit: RateLimit | None = None
    credentials: tuple[str, ...] = ()
    request_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_domains", _validate_allowed_domains(self.allowed_domains))
        object.__setattr__(self, "terms_urls", _validate_terms_urls(self.terms_urls))
        if self.license_inference not in _LICENSE_INFERENCE_VALUES:
            raise _fail(
                "ProviderCapabilities.license_inference",
                f"must be one of {_LICENSE_INFERENCE_VALUES}",
                self.license_inference,
            )
        _require_declared_true(
            "captures_raw",
            self.captures_raw,
            "the package's writer stores the fetched bytes unconditionally",
        )
        _require_declared_true(
            "quarantine_on_incomplete",
            self.quarantine_on_incomplete,
            "the package quarantines an incomplete acquisition unconditionally",
        )
        if self.rate_limit is not None and not isinstance(self.rate_limit, RateLimit):
            raise _fail("ProviderCapabilities.rate_limit", "must be a RateLimit or None", self.rate_limit)
        object.__setattr__(self, "credentials", _validate_credentials(self.credentials))
        object.__setattr__(self, "request_kinds", _validate_request_kinds(self.request_kinds))


@dataclass(frozen=True)
class PlannedRequest:
    """One HTTPS request the package will execute on the provider's behalf.

    A credential may be referenced as ``{{credential:NAME}}`` in a header **value** and
    nowhere else.  A placeholder in the URL is refused: URLs are recorded in provenance,
    logs, and error envelopes through a redactor that cannot know a query parameter holds
    a secret, so a credential smuggled into a URL would defeat redaction outright.
    """

    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    timeout_hint: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.strip():
            raise _fail("PlannedRequest.url", "must be a non-empty string", self.url)
        if not self.url.startswith(_HTTPS_PREFIX) or len(self.url) <= len(_HTTPS_PREFIX):
            raise _fail("PlannedRequest.url", "must be an https:// URL with a host", self.url)
        if any(character.isspace() for character in self.url):
            raise _fail("PlannedRequest.url", "must not contain whitespace", self.url)
        if _CREDENTIAL_PLACEHOLDER_PREFIX in self.url:
            raise ValueError(
                "PlannedRequest.url must not embed a {{credential:NAME}} placeholder: a credential "
                "in a URL would survive URL redaction in logs, provenance, and error envelopes, so "
                f"placeholders are accepted in header values only: {self.url!r}"
            )
        if self.method not in _HTTP_METHODS:
            raise _fail("PlannedRequest.method", f"must be one of {_HTTP_METHODS}", self.method)
        object.__setattr__(self, "headers", _validate_headers(self.headers))
        if self.body is not None:
            # A request body can carry an authentication payload, so it is described by
            # type and length in rejections, never rendered.
            if not isinstance(self.body, bytes):
                raise ValueError(f"PlannedRequest.body must be bytes or None, got {type(self.body).__name__}")
            if self.method != "POST":
                raise ValueError(
                    'PlannedRequest.body is only allowed when method is "POST": '
                    f"{len(self.body)} bytes were planned with method {self.method!r}"
                )
        if self.timeout_hint is not None:
            if isinstance(self.timeout_hint, bool) or not isinstance(self.timeout_hint, (int, float)):
                raise _fail("PlannedRequest.timeout_hint", "must be a number of seconds or None", self.timeout_hint)
            if not 0 < self.timeout_hint < float("inf"):
                raise _fail(
                    "PlannedRequest.timeout_hint",
                    "must be a positive, finite number of seconds",
                    self.timeout_hint,
                )
            object.__setattr__(self, "timeout_hint", float(self.timeout_hint))


@dataclass(frozen=True)
class SourceArtifact:
    """What the provider says the responses mean; the package decides where it lands.

    ``filename`` is a bare name because the package owns the directory — the target root
    comes from configuration and is never negotiable from a plugin.  ``content`` is the
    exact byte string the package will write, ``source_type`` is validated against the
    workspace's delivery vocabulary at write time, and ``provenance_metadata`` is copied
    into the sidecar under a provider-namespaced key.
    """

    filename: str
    source_type: str
    content: bytes
    provenance_metadata: Mapping[str, Any] = MappingProxyType({})
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_filename(self.filename)
        if not isinstance(self.source_type, str) or not self.source_type.strip():
            raise _fail("SourceArtifact.source_type", "must be a non-empty string", self.source_type)
        if not isinstance(self.content, bytes):
            # The fetched payload itself is never rendered into an error message.
            raise ValueError(f"SourceArtifact.content must be bytes, got {type(self.content).__name__}")
        object.__setattr__(self, "provenance_metadata", _validate_provenance_metadata(self.provenance_metadata))
        object.__setattr__(
            self,
            "warnings",
            _coerce_str_tuple(self.warnings, "SourceArtifact.warnings", allow_empty=True),
        )


class AcquisitionProvider(abc.ABC):
    """Plans fetches and interprets responses for one acquisition source family.

    Subclassing is a convenience: the registry loader checks the shape below, not the
    base class.  Declare ``id`` (``^[a-z0-9][a-z0-9._-]*$``, never shadowing a built-in
    id), ``provider_api_version`` (``1``), and ``capabilities``.
    """

    id: str
    provider_api_version: int
    capabilities: ProviderCapabilities

    @abc.abstractmethod
    def validate_request(self, request: Mapping[str, Any]) -> None:
        """Refuse a request document this provider does not understand.

        The request envelope is the package's; its contents are the provider's.  Raise
        ``ValueError`` with a message the operator can act on — the package carries it in
        the error detail — and return ``None`` to accept.
        """

    @abc.abstractmethod
    def plan_fetch(self, request: Mapping[str, Any]) -> tuple[PlannedRequest, ...]:
        """Return the requests to execute, in order, for an accepted request document.

        At most :data:`MAX_PLANNED_REQUESTS`; every URL must fall inside the declared
        ``allowed_domains``.  This method must not perform I/O.
        """

    @abc.abstractmethod
    def interpret(self, request: Mapping[str, Any], responses: tuple[bytes, ...]) -> SourceArtifact:
        """Turn the executed responses into one artifact descriptor.

        ``responses`` is positionally aligned with the planned requests.  Raising here
        abandons the whole acquisition: the package leaves no partial artifact behind.
        """


class DiscoveryProvider(abc.ABC):
    """Plans searches and interprets responses into candidates — never evidence.

    Candidates re-enter the pipeline through the same coercion, classification, and trust
    rejection every other search result passes, so a registered discovery provider is
    trusted no further than the built-in search backends.
    """

    id: str
    provider_api_version: int
    capabilities: ProviderCapabilities

    @abc.abstractmethod
    def validate_request(self, request: Mapping[str, Any]) -> None:
        """Refuse a search request document this provider does not understand."""

    @abc.abstractmethod
    def plan_search(self, request: Mapping[str, Any]) -> tuple[PlannedRequest, ...]:
        """Return the search requests to execute, in order, bounded as in ``plan_fetch``."""

    @abc.abstractmethod
    def interpret_candidates(
        self,
        request: Mapping[str, Any],
        responses: tuple[bytes, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        """Turn the executed responses into candidate mappings for the search pipeline."""
