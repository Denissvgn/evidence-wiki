"""A third-party provider distribution, authored the way a real plugin author would.

This module is the reference implementation of the CR-5 provider contract, and it is
deliberately written **without** ``evidence_wiki`` installed: it declares its own frozen
dataclasses for every shape the contract names and never imports the package. That is the
point of the fixture. ``_provider_plugins.py`` validates registrations *structurally*, so
a plugin that matches the shape is valid even though it shares no types with the package;
importing ``evidence_wiki`` here would quietly turn that property into an untested claim.

The provider is a planner and an interpreter, never a fetcher (CR-5 §2.1). It declares the
hosts it may reach and the environment variable holding its key; it never reads that
variable. Header values carry the ``{{credential:NAME}}`` placeholder, and the package's
own pinned transport resolves it at execution time, so no secret is ever in reach of this
code.

Every declared host is under the reserved ``.invalid`` TLD (RFC 2606), which can never
resolve. A test that reaches real transport without a stub fails on DNS instead of opening
a socket to somebody's live service.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

PROVIDER_API_VERSION = 1

ACQUISITION_PROVIDER_ID = "keepa-fixture"
DISCOVERY_PROVIDER_ID = "keepa-search-fixture"

#: Environment variable *name* the package resolves at execution time. A name is not a
#: secret: the fixture never reads it and never carries a value.
CREDENTIAL_ENV_VAR = "KEEPA_FIXTURE_API_KEY"
CREDENTIAL_PLACEHOLDER = "{{credential:" + CREDENTIAL_ENV_VAR + "}}"

API_HOST = "api.keepa-fixture.invalid"
ASSET_HOST = "assets.keepa-fixture.invalid"
CATALOG_HOST = "www.keepa-fixture.invalid"

TERMS_URL = f"https://{API_HOST}/terms"
ASSET_TERMS_URL = f"https://{ASSET_HOST}/terms"

DEFAULT_HISTORY_DAYS = 90
MAX_HISTORY_DAYS = 365
DEFAULT_MAX_RESULTS = 5
MAX_MAX_RESULTS = 25

ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")

ACQUISITION_REQUEST_FIELDS = ("asin", "history_days")
DISCOVERY_REQUEST_FIELDS = ("query", "max_results")


# --- Contract shapes ---------------------------------------------------------
#
# Duck-typed equivalents of evidence_wiki.providers. Same attribute names, same value
# shapes, no shared types: matching the shape is the contract (CR-5 §2.2).


@dataclass(frozen=True)
class RateLimit:
    """A ceiling on planned-request executions, accounted per run by the package."""

    requests: int
    per: str


@dataclass(frozen=True)
class ProviderCapabilities:
    """What the provider declares it needs, and what it promises about its output."""

    allowed_domains: tuple[str, ...]
    terms_urls: tuple[str, ...]
    license_inference: str
    captures_raw: bool = True
    quarantine_on_incomplete: bool = True
    rate_limit: RateLimit | None = None
    credentials: tuple[str, ...] = ()
    request_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedRequest:
    """One HTTPS request the package's transport will execute on the plugin's behalf."""

    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    timeout_hint: float | None = None


@dataclass(frozen=True)
class SourceArtifact:
    """The bytes and metadata the package writes; the plugin never touches the workspace."""

    filename: str
    source_type: str
    content: bytes
    provenance_metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


# --- Shared request validation ----------------------------------------------


def _require_mapping(request: Any, provider_id: str) -> Mapping[str, Any]:
    if not isinstance(request, Mapping):
        raise ValueError(f"{provider_id} request must be a JSON object, not {type(request).__name__}")
    return request


def _reject_unknown_fields(request: Mapping[str, Any], provider_id: str, known: tuple[str, ...]) -> None:
    unknown = sorted(str(key) for key in request if str(key) not in known)
    if unknown:
        raise ValueError(
            f"{provider_id} request has unknown field(s): {', '.join(unknown)}. "
            f"Supported fields: {', '.join(known)}"
        )


def _bounded_int(value: Any, *, provider_id: str, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    # bool is an int subclass; `true` in a JSON request is a malformed count, not 1.
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{provider_id} request field {name!r} must be an integer between {low} and {high}")
    return value


def _decoded_json(payload: Any, *, provider_id: str, label: str) -> Any:
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError(f"{provider_id} expected {label} as bytes, got {type(payload).__name__}")
    try:
        return json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{provider_id} could not read {label} as UTF-8 JSON: {exc}") from exc


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _responses_tuple(responses: Any, *, provider_id: str, expected: int) -> tuple[bytes, ...]:
    if isinstance(responses, (str, bytes, bytearray)) or not isinstance(responses, Sequence):
        raise ValueError(f"{provider_id} expected a sequence of response payloads")
    if len(responses) != expected:
        raise ValueError(f"{provider_id} expected {expected} response(s), got {len(responses)}")
    return tuple(responses)


# --- Acquisition -------------------------------------------------------------


class KeepaFixtureAcquisitionProvider:
    """Plans a two-request price-history fetch and interprets the pair deterministically."""

    id = ACQUISITION_PROVIDER_ID
    provider_api_version = PROVIDER_API_VERSION
    capabilities = ProviderCapabilities(
        allowed_domains=(API_HOST, ASSET_HOST),
        terms_urls=(TERMS_URL, ASSET_TERMS_URL),
        license_inference="partial",
        captures_raw=True,
        quarantine_on_incomplete=True,
        rate_limit=RateLimit(requests=60, per="minute"),
        credentials=(CREDENTIAL_ENV_VAR,),
        request_kinds=("market-data/price_history",),
    )

    def validate_request(self, request: Mapping[str, Any]) -> None:
        """Refuse anything this provider cannot serve, with a reason a host can print."""

        mapping = _require_mapping(request, self.id)
        _reject_unknown_fields(mapping, self.id, ACQUISITION_REQUEST_FIELDS)
        asin = mapping.get("asin")
        if not isinstance(asin, str) or ASIN_PATTERN.fullmatch(asin) is None:
            raise ValueError(
                f"{self.id} request field 'asin' must be a 10-character upper-case alphanumeric product id"
            )
        _bounded_int(
            mapping.get("history_days"),
            provider_id=self.id,
            name="history_days",
            default=DEFAULT_HISTORY_DAYS,
            low=1,
            high=MAX_HISTORY_DAYS,
        )

    def plan_fetch(self, request: Mapping[str, Any]) -> tuple[PlannedRequest, ...]:
        """Return the two requests that together make one artifact."""

        self.validate_request(request)
        asin = request["asin"]
        history_days = _bounded_int(
            request.get("history_days"),
            provider_id=self.id,
            name="history_days",
            default=DEFAULT_HISTORY_DAYS,
            low=1,
            high=MAX_HISTORY_DAYS,
        )
        query = urlencode({"asin": asin, "days": history_days})
        return (
            PlannedRequest(
                url=f"https://{API_HOST}/product?{query}",
                method="GET",
                headers=(
                    ("Accept", "application/json"),
                    # The package substitutes the placeholder at execution time and
                    # registers the resolved value for redaction; the plugin never sees it.
                    ("X-Keepa-Fixture-Key", CREDENTIAL_PLACEHOLDER),
                ),
                timeout_hint=15.0,
            ),
            PlannedRequest(
                url=f"https://{ASSET_HOST}/product/{quote(asin, safe='')}/history.csv",
                method="GET",
                headers=(("Accept", "text/csv"),),
                timeout_hint=30.0,
            ),
        )

    def interpret(self, request: Mapping[str, Any], responses: tuple[bytes, ...]) -> SourceArtifact:
        """Fold both responses into one deterministic JSON artifact."""

        self.validate_request(request)
        product_bytes, history_bytes = _responses_tuple(responses, provider_id=self.id, expected=2)
        product = _decoded_json(product_bytes, provider_id=self.id, label="the product response")
        if not isinstance(product, Mapping):
            raise ValueError(f"{self.id} product response must be a JSON object")
        if not isinstance(history_bytes, (bytes, bytearray)):
            raise ValueError(f"{self.id} expected the history response as bytes")

        history_text = bytes(history_bytes).decode("utf-8", errors="replace")
        history_rows = [line for line in history_text.splitlines() if line.strip()]
        asin = request["asin"]
        history_days = _bounded_int(
            request.get("history_days"),
            provider_id=self.id,
            name="history_days",
            default=DEFAULT_HISTORY_DAYS,
            low=1,
            high=MAX_HISTORY_DAYS,
        )

        warnings: list[str] = []
        title = product.get("title")
        if not isinstance(title, str) or not title.strip():
            warnings.append(f"{self.id}: product response carries no title; the artifact records the id only")
            title = None
        if len(history_rows) <= 1:
            warnings.append(f"{self.id}: price history is empty; only the header row was returned")

        document = {
            "schema": "keepa-fixture/price-history/1",
            "asin": asin,
            "history_days": history_days,
            "title": title,
            "currency": product.get("currency"),
            "price_history_rows": history_rows[1:],
            "price_history_header": history_rows[0] if history_rows else None,
            "response_digests": [_digest(bytes(product_bytes)), _digest(bytes(history_bytes))],
        }
        content = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

        return SourceArtifact(
            filename=f"keepa-fixture-{asin.lower()}.json",
            source_type="dataset",
            content=content,
            provenance_metadata={
                "asin": asin,
                "history_days": history_days,
                "api_host": API_HOST,
                "asset_host": ASSET_HOST,
                "terms_url": TERMS_URL,
                "observation_count": max(len(history_rows) - 1, 0),
                "response_digests": document["response_digests"],
            },
            warnings=tuple(warnings),
        )


# --- Discovery ---------------------------------------------------------------


class KeepaFixtureDiscoveryProvider:
    """Plans one catalogue search and interprets it into candidate records.

    The records match the raw search-result shape ``coerce_search_results`` accepts
    (``url``, ``title``, and the optional ``snippet``/``published``/``license``/
    ``terms_url``/``official``/``trust_tier``/``source_type`` hints), so registered
    candidates go through exactly the same classification and trust rejection as any
    other search hit. Discovery never acquires evidence: these are candidates.
    """

    id = DISCOVERY_PROVIDER_ID
    provider_api_version = PROVIDER_API_VERSION
    capabilities = ProviderCapabilities(
        allowed_domains=(API_HOST,),
        terms_urls=(TERMS_URL,),
        license_inference="none",
        captures_raw=True,
        quarantine_on_incomplete=True,
        rate_limit=RateLimit(requests=30, per="minute"),
        credentials=(CREDENTIAL_ENV_VAR,),
        request_kinds=("market-data/product_search",),
    )

    def validate_request(self, request: Mapping[str, Any]) -> None:
        mapping = _require_mapping(request, self.id)
        _reject_unknown_fields(mapping, self.id, DISCOVERY_REQUEST_FIELDS)
        query = mapping.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"{self.id} request field 'query' must be a non-empty string")
        _bounded_int(
            mapping.get("max_results"),
            provider_id=self.id,
            name="max_results",
            default=DEFAULT_MAX_RESULTS,
            low=1,
            high=MAX_MAX_RESULTS,
        )

    def plan_search(self, request: Mapping[str, Any]) -> tuple[PlannedRequest, ...]:
        self.validate_request(request)
        max_results = _bounded_int(
            request.get("max_results"),
            provider_id=self.id,
            name="max_results",
            default=DEFAULT_MAX_RESULTS,
            low=1,
            high=MAX_MAX_RESULTS,
        )
        query = urlencode({"term": request["query"].strip(), "limit": max_results})
        return (
            PlannedRequest(
                url=f"https://{API_HOST}/search?{query}",
                method="GET",
                headers=(
                    ("Accept", "application/json"),
                    ("X-Keepa-Fixture-Key", CREDENTIAL_PLACEHOLDER),
                ),
                timeout_hint=15.0,
            ),
        )

    def interpret_candidates(
        self,
        request: Mapping[str, Any],
        responses: tuple[bytes, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        self.validate_request(request)
        (payload_bytes,) = _responses_tuple(responses, provider_id=self.id, expected=1)
        payload = _decoded_json(payload_bytes, provider_id=self.id, label="the search response")
        if isinstance(payload, Mapping):
            payload = payload.get("results")
        if not isinstance(payload, list):
            raise ValueError(f"{self.id} search response must be a results array or an object with a 'results' array")

        max_results = _bounded_int(
            request.get("max_results"),
            provider_id=self.id,
            name="max_results",
            default=DEFAULT_MAX_RESULTS,
            low=1,
            high=MAX_MAX_RESULTS,
        )

        candidates: list[Mapping[str, Any]] = []
        for entry in payload:
            if not isinstance(entry, Mapping):
                continue
            asin = entry.get("asin")
            if not isinstance(asin, str) or ASIN_PATTERN.fullmatch(asin) is None:
                continue
            title = entry.get("title")
            candidates.append(
                {
                    "url": f"https://{CATALOG_HOST}/product/{quote(asin, safe='')}",
                    "title": title if isinstance(title, str) and title.strip() else f"Product {asin}",
                    "snippet": entry.get("snippet") if isinstance(entry.get("snippet"), str) else None,
                    "published": entry.get("first_seen") if isinstance(entry.get("first_seen"), str) else None,
                    "source_type": "dataset",
                    # A vendor's own catalogue is primary for its own listings, but it is
                    # not an official authority for anything, so the officialness hint is
                    # explicitly false rather than absent.
                    "official": False,
                    "trust_tier": "primary_non_official",
                    "license": None,
                    "terms_url": TERMS_URL,
                }
            )
            if len(candidates) >= max_results:
                break
        return tuple(candidates)
