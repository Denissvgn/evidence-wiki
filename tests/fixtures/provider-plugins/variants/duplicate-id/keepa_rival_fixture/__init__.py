"""A second, independently valid distribution that claims the base fixture's provider id.

Installed beside ``keepa_fixture`` this is the CR-5 §2.4 collision case: two installed
distributions claiming one id must make **both** registrations invalid, because first-wins
would make provider behaviour depend on installation order. Everything about this
declaration is well formed on its own — only the id is contested.

It is written standalone (no import of ``keepa_fixture``) so it can also be installed
alone, where it is simply a valid registration under that id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PROVIDER_API_VERSION = 1
CREDENTIAL_ENV_VAR = "KEEPA_RIVAL_FIXTURE_API_KEY"
RIVAL_HOST = "api.keepa-rival-fixture.invalid"


@dataclass(frozen=True)
class RateLimit:
    requests: int
    per: str


@dataclass(frozen=True)
class ProviderCapabilities:
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
    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    timeout_hint: float | None = None


@dataclass(frozen=True)
class SourceArtifact:
    filename: str
    source_type: str
    content: bytes
    provenance_metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class RivalAcquisitionProvider:
    """Valid in isolation; invalid the moment the base fixture is installed beside it."""

    id = "keepa-fixture"
    provider_api_version = PROVIDER_API_VERSION
    capabilities = ProviderCapabilities(
        allowed_domains=(RIVAL_HOST,),
        terms_urls=(f"https://{RIVAL_HOST}/terms",),
        license_inference="none",
        captures_raw=True,
        quarantine_on_incomplete=True,
        rate_limit=RateLimit(requests=10, per="minute"),
        credentials=(CREDENTIAL_ENV_VAR,),
        request_kinds=("market-data/price_history",),
    )

    def validate_request(self, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping) or not isinstance(request.get("asin"), str):
            raise ValueError(f"{self.id} (rival distribution) request must be an object with a string 'asin'")

    def plan_fetch(self, request: Mapping[str, Any]) -> tuple[PlannedRequest, ...]:
        self.validate_request(request)
        return (
            PlannedRequest(
                url=f"https://{RIVAL_HOST}/product/{request['asin']}",
                headers=(("X-Rival-Key", "{{credential:" + CREDENTIAL_ENV_VAR + "}}"),),
            ),
        )

    def interpret(self, request: Mapping[str, Any], responses: tuple[bytes, ...]) -> SourceArtifact:
        self.validate_request(request)
        return SourceArtifact(
            filename=f"keepa-rival-fixture-{request['asin'].lower()}.json",
            source_type="dataset",
            content=b"".join(bytes(payload) for payload in responses),
            provenance_metadata={"asin": request["asin"], "api_host": RIVAL_HOST},
        )
