"""A structurally perfect declaration that claims a reserved built-in provider id.

CR-5 §2.4: built-in ids stay reserved and can never be shadowed. This distribution is
valid in every other respect, so a loader that rejects it is rejecting it for the id alone
— which is the property worth pinning.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PROVIDER_API_VERSION = 1
RESERVED_HOST = "api.keepa-reserved-fixture.invalid"


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


class ReservedIdAcquisitionProvider:
    """Claims the built-in ``web`` acquisition id."""

    id = "web"
    provider_api_version = PROVIDER_API_VERSION
    capabilities = ProviderCapabilities(
        allowed_domains=(RESERVED_HOST,),
        terms_urls=(f"https://{RESERVED_HOST}/terms",),
        license_inference="none",
        captures_raw=True,
        quarantine_on_incomplete=True,
        rate_limit=None,
        credentials=(),
        request_kinds=(),
    )

    def validate_request(self, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            raise ValueError("reserved-id fixture request must be a JSON object")

    def plan_fetch(self, request: Mapping[str, Any]) -> tuple[PlannedRequest, ...]:
        self.validate_request(request)
        return (PlannedRequest(url=f"https://{RESERVED_HOST}/document"),)

    def interpret(self, request: Mapping[str, Any], responses: tuple[bytes, ...]) -> SourceArtifact:
        self.validate_request(request)
        return SourceArtifact(
            filename="keepa-reserved-fixture.json",
            source_type="dataset",
            content=b"".join(bytes(payload) for payload in responses),
            provenance_metadata={"api_host": RESERVED_HOST},
        )
