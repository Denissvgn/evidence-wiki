#!/usr/bin/env python3
"""Shared provider identifiers and provider-list validation.

The provider allow-lists in ``research.yml`` are authorization boundaries.  A
discovery strategy (for example ``legal``) may compose one or more providers,
but it is never itself permission to contact a network service.  The legacy
strategy identifiers are accepted for one compatibility release so upgraded
workspaces remain readable; callers must not use them as provider authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STANDARDS_DISCOVERY_PROVIDER_IDS = (
    "iso-open-data",
    "eu-product-requirements",
    "uk-geospatial-register",
    "nist",
)

DISCOVERY_PROVIDER_IDS = (
    "arxiv",
    "openalex",
    "github",
    "search",
    "standards",
    *(f"standards:{provider}" for provider in STANDARDS_DISCOVERY_PROVIDER_IDS),
)
ACQUISITION_PROVIDER_IDS = ("arxiv", "openalex", "github", "web")

# These values existed before providers and strategies were separated.  They
# remain parseable for one compatibility release, but do not authorize I/O.
LEGACY_DISCOVERY_STRATEGY_IDS = ("legal", "authors", "companions")
DISCOVERY_ACCEPTED_IDS = (*DISCOVERY_PROVIDER_IDS, *LEGACY_DISCOVERY_STRATEGY_IDS)


class ProviderListError(ValueError):
    """Raised when a configured provider allow-list has an invalid shape."""


@dataclass(frozen=True)
class ProviderList:
    configured: tuple[str, ...]
    providers: tuple[str, ...]
    legacy_strategies: tuple[str, ...]


def validate_provider_ids(
    value: Any,
    *,
    phase: str,
    require_non_empty: bool = False,
) -> ProviderList:
    """Validate one phase allow-list without granting strategy aliases access."""

    if phase == "discovery":
        allowed = DISCOVERY_ACCEPTED_IDS
        legacy = set(LEGACY_DISCOVERY_STRATEGY_IDS)
    elif phase == "acquisition":
        allowed = ACQUISITION_PROVIDER_IDS
        legacy = set()
    else:  # pragma: no cover - internal programming guard
        raise ValueError(f"unknown provider phase: {phase}")

    if value is None:
        configured: list[str] = []
    elif isinstance(value, list):
        configured = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ProviderListError("must be a list of non-empty provider identifiers")
            configured.append(item.strip())
    else:
        raise ProviderListError("must be a list of provider identifiers")

    duplicates = sorted({provider for provider in configured if configured.count(provider) > 1})
    if duplicates:
        raise ProviderListError(f"has duplicate provider(s): {', '.join(duplicates)}")
    unknown = sorted(set(configured) - set(allowed))
    if unknown:
        raise ProviderListError(
            f"has unknown provider(s): {', '.join(unknown)}. Allowed providers: {', '.join(allowed)}"
        )
    concrete = tuple(provider for provider in configured if provider not in legacy)
    if require_non_empty and not concrete:
        raise ProviderListError(
            f"must include at least one provider (a concrete provider, not a strategy id) when {phase} is enabled"
        )

    return ProviderList(
        configured=tuple(configured),
        providers=concrete,
        legacy_strategies=tuple(provider for provider in configured if provider in legacy),
    )


def provider_is_allowed(configured: ProviderList | list[str] | tuple[str, ...], provider: str) -> bool:
    """Return whether a concrete provider was explicitly authorized."""

    if isinstance(configured, ProviderList):
        values = configured.providers
    else:
        values = tuple(item for item in configured if item not in LEGACY_DISCOVERY_STRATEGY_IDS)
    return provider in values
