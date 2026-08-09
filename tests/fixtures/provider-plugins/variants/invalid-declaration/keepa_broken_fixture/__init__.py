"""A registration that imports cleanly and then violates every declaration rule at once.

Deliberately a kitchen sink: CR-5 §2.3/§2.4 require the invalid-registration reason to
name *every* violation, not the first one found, so the fixture that pins that behaviour
has to carry more than one. The methods are all present and callable — nothing here fails
for a missing attribute; each failure is a value rule.

Violations, in declaration order: an id that breaks ``^[a-z0-9][a-z0-9._-]*$``; an
unsupported ``provider_api_version``; empty ``allowed_domains``; empty ``terms_urls``; a
``license_inference`` outside ``yes|partial|none``; ``captures_raw`` and
``quarantine_on_incomplete`` declared ``False`` when v1 requires ``True``; a rate limit
with a non-positive count and an unknown window; and a credential written as a value-ish
lower-case name instead of an env-var NAME matching ``^[A-Z][A-Z0-9_]*$``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


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


class BrokenDeclarationAcquisitionProvider:
    """Every attribute the contract names is present; every value rule is broken."""

    id = "Keepa_Broken_Fixture"
    provider_api_version = 2
    capabilities = ProviderCapabilities(
        allowed_domains=(),
        terms_urls=(),
        license_inference="maybe",
        captures_raw=False,
        quarantine_on_incomplete=False,
        rate_limit=RateLimit(requests=0, per="fortnight"),
        credentials=("keepa_broken_fixture_key",),
        request_kinds=("",),
    )

    def validate_request(self, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            raise ValueError("broken-declaration fixture request must be a JSON object")

    def plan_fetch(self, request: Mapping[str, Any]) -> tuple[PlannedRequest, ...]:
        self.validate_request(request)
        return (PlannedRequest(url="https://api.keepa-broken-fixture.invalid/product"),)

    def interpret(self, request: Mapping[str, Any], responses: tuple[bytes, ...]) -> SourceArtifact:
        self.validate_request(request)
        return SourceArtifact(
            filename="keepa-broken-fixture.json",
            source_type="dataset",
            content=b"".join(bytes(payload) for payload in responses),
        )
