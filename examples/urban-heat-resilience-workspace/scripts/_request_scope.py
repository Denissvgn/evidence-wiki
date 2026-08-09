#!/usr/bin/env python3
"""Structured request scope: what would satisfy a source request, machine-readably.

A source request carries free-text ``query_or_identifier`` for a human and, since
CR-4, an optional ``scope`` mapping for a machine::

    source_requests.py add --kind pack:market-data/supplier_quote \\
        --scope facet_id=supplier_quote --scope candidate=acme-widget ...

Without it, pairing a delivered source back to the request it answers degrades to
convention — hosts zip ``blocking_request_ids`` against delivered sources *by
position*, and a wrong guess records "request X was fulfilled by source Y" as
audited fact. With it, the delivering side states the same keys in its
``.provenance.yml`` sidecar and the two are compared instead of assumed.

The package stores and matches these keys; it never interprets them. ``facet_id``
is the convention coverage-manifest tooling uses, not a schema this module knows.

Match semantics are layered, so opting in is never a backward-compatibility break:

1. **Contradiction** — keys present on *both* sides must agree. Always checked; it
   cannot fire unless both the request and the delivery declared scope.
2. **Absence** — a key on only one side is not a contradiction by default. Callers
   that can guarantee their pipeline stamps scope opt into refusing it
   (``fulfill --require-scope``), which closes the hole where an unstamped delivery
   would otherwise sail past every check.
"""

from __future__ import annotations

import re
from typing import Any

SCOPE_KEY_RE = re.compile(r"^[a-z0-9_][a-z0-9._-]*$")
FACET_SCOPE_KEY = "facet_id"

SCOPE_REMEDIATION = "Pass scope pairs as key=value with a lowercase key, as documented in docs/source-delivery.md."


class RequestScopeError(Exception):
    """Structured request-scope failure carrying a stable machine-readable code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.remediation = remediation or SCOPE_REMEDIATION
        self.details = details or {}


def parse_scope_pairs(values: list[str] | None, *, option: str = "--scope") -> dict[str, str]:
    """Parse repeatable ``key=value`` option values into a scope mapping.

    Returns an empty mapping when the option was not given, which callers store as
    "no scope declared" rather than as an empty declaration.
    """
    scope: dict[str, str] = {}
    for raw in values or []:
        if not isinstance(raw, str) or "=" not in raw:
            raise RequestScopeError(
                "REQUEST_SCOPE_INVALID",
                f"{option} must be key=value; rejected {raw!r}",
                details={"option": option, "value": raw},
            )
        raw_key, raw_value = raw.split("=", 1)
        key = raw_key.strip()
        value = raw_value.strip()
        if not key or SCOPE_KEY_RE.fullmatch(key) is None:
            raise RequestScopeError(
                "REQUEST_SCOPE_INVALID",
                (
                    f"{option} key must match {SCOPE_KEY_RE.pattern} "
                    f"(lowercase letters, digits, dot, underscore, hyphen); rejected {raw_key.strip()!r}"
                ),
                details={"option": option, "key": raw_key.strip()},
            )
        if not value:
            raise RequestScopeError(
                "REQUEST_SCOPE_INVALID",
                f"{option} value for key {key!r} must be a non-empty string",
                details={"option": option, "key": key},
            )
        if key in scope:
            raise RequestScopeError(
                "REQUEST_SCOPE_INVALID",
                f"{option} key {key!r} was given more than once",
                details={"option": option, "key": key},
            )
        scope[key] = value
    return scope


def normalize_scope(value: Any) -> dict[str, str]:
    """Read a stored scope mapping defensively, from a request record or a sidecar.

    Non-conforming entries are dropped rather than raising: this reads data another
    writer produced, and a malformed sidecar key must not crash a fulfil that has
    already passed its own gates. Callers that need strictness compare what survives.
    """
    if not isinstance(value, dict):
        return {}
    scope: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not key or SCOPE_KEY_RE.fullmatch(key) is None:
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
            continue
        text = str(raw_value).strip()
        if not text:
            continue
        scope[key] = text
    return scope


def scope_equal(left: Any, right: Any) -> bool:
    """True when two stored scopes declare exactly the same keys and values."""
    return normalize_scope(left) == normalize_scope(right)


def scope_match(request_scope: Any, source_scope: Any) -> tuple[list[str], list[str]]:
    """Compare a request's scope against a delivered source's scope.

    Returns ``(conflicts, absences)``:

    - ``conflicts`` — keys both sides declare with different values. Always fatal.
    - ``absences`` — keys the request declares that the source never states. Fatal
      only under an explicit strict mode.

    Both lists are sorted so error messages and tests are deterministic.
    """
    request = normalize_scope(request_scope)
    source = normalize_scope(source_scope)
    conflicts = sorted(key for key, value in request.items() if key in source and source[key] != value)
    absences = sorted(key for key in request if key not in source)
    return conflicts, absences


def conflict_details(
    request_scope: Any,
    source_scope: Any,
    keys: list[str],
) -> list[dict[str, str]]:
    """Describe each conflicting key with both values, for a machine-readable envelope."""
    request = normalize_scope(request_scope)
    source = normalize_scope(source_scope)
    return [
        {"key": key, "request_value": request.get(key, ""), "source_value": source.get(key, "")}
        for key in keys
    ]


def format_scope(value: Any) -> str:
    """Render a scope mapping compactly for text output; empty string when unscoped."""
    scope = normalize_scope(value)
    return ", ".join(f"{key}={scope[key]}" for key in sorted(scope))
