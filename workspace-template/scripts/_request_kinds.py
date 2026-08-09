#!/usr/bin/env python3
"""Shared registry for source-request kinds, including domain-pack extensions.

A documentary research workspace needs five request kinds; a workspace whose
evidence is market data, sensor series, or instrument output needs its own. So
the valid set is *built-ins plus whatever the active domain pack declares*,
resolved from the merged ``research.yml`` on every invocation:

.. code-block:: yaml

    domain_pack:
      name: market-data
      request_kinds:
        - id: pack:market-data/supplier_quote
          label: Supplier quote
          description: Live SKU price + shipping + MOQ from a named supplier.

Pack ids are namespaced exactly like pack evidence policies
(``pack:<pack-name>/<kind-id>``) and the namespace must match the declaring
pack's own ``name``, so one pack can never define kinds in another's namespace.
Built-in ids stay reserved and unprefixed, which makes collision impossible in
either direction.

The declaration is validated in one place and read by two consumers that must
never disagree: ``evidence-wiki pack validate`` (before a pack ships) through
:func:`declaration_errors`, and ``source_requests.py add`` (at request time)
through :func:`validate_kind`. A malformed declaration raises rather than
degrading to "no kinds declared" — silently dropping a pack's vocabulary would
refuse the pack's own requests with a misleading "undeclared kind" message.
"""

from __future__ import annotations

import re
from typing import Any

# ``structured_data`` is the generic non-documentary bucket, matching the manifest
# source kind that source_inventory.py already classifies. It is deliberately a
# built-in rather than a pack kind: every structured-evidence domain needs it, and
# pairing it with a normalizer adapter is a separate, optional concern.
BUILTIN_REQUEST_KINDS = ("paper", "dataset", "web", "code", "structured_data", "other")

# Same shape as PACK_POLICY_ID_RE in _evidence_policies.py. Both segments already
# admit underscores, so ids like pack:market-data/supplier_quote match unchanged.
PACK_REQUEST_KIND_RE = re.compile(r"^pack:[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$")
# The same id written without the reserved prefix. Only used to turn a near-miss
# into a message naming the id the caller actually wants.
BARE_REQUEST_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$")

REQUEST_KIND_DECLARATION_KEYS = ("id", "label", "description")
REQUEST_KIND_REMEDIATION = (
    "Declare the kind under domain_pack.request_kinds as documented in docs/research-yml.md, "
    "or use one of the built-in kinds."
)


class RequestKindError(Exception):
    """Structured request-kind failure carrying a stable machine-readable code."""

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
        self.remediation = remediation or REQUEST_KIND_REMEDIATION
        self.details = details or {}


def builtin_kinds() -> tuple[str, ...]:
    """Return the reserved, always-valid request kinds."""
    return BUILTIN_REQUEST_KINDS


def _raw_declarations(domain_pack: Any) -> Any:
    if not isinstance(domain_pack, dict):
        return None
    return domain_pack.get("request_kinds")


def _collect_declarations(domain_pack: Any) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Parse ``domain_pack.request_kinds`` into (declared kinds, human-readable errors).

    Single source of truth for both consumers: :func:`declaration_errors` surfaces the
    error list as pack-validate findings, :func:`declared_pack_kinds` raises on it.
    """
    raw = _raw_declarations(domain_pack)
    if raw is None:
        return {}, []
    if not isinstance(raw, list):
        return {}, ["domain_pack.request_kinds must be a list of kind declarations"]

    pack_name = domain_pack.get("name") if isinstance(domain_pack, dict) else None
    pack_name = pack_name.strip() if isinstance(pack_name, str) else None

    declared: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    for index, entry in enumerate(raw):
        label = f"domain_pack.request_kinds[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be a mapping with id, label, and description")
            continue
        unknown = sorted(str(key) for key in entry if str(key) not in REQUEST_KIND_DECLARATION_KEYS)
        if unknown:
            errors.append(f"{label} has unknown keys: {', '.join(unknown)}")
        fields: dict[str, str] = {}
        for key in REQUEST_KIND_DECLARATION_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}.{key} must be a non-empty string")
                continue
            fields[key] = value.strip()
        if len(fields) != len(REQUEST_KIND_DECLARATION_KEYS):
            continue

        kind_id = fields["id"]
        if kind_id in BUILTIN_REQUEST_KINDS:
            errors.append(f"{label}.id {kind_id} is a reserved built-in kind and cannot be redeclared")
            continue
        if PACK_REQUEST_KIND_RE.fullmatch(kind_id) is None:
            hint = f" (did you mean pack:{kind_id}?)" if BARE_REQUEST_KIND_RE.fullmatch(kind_id) else ""
            errors.append(
                f"{label}.id {kind_id} must be namespaced like pack:<pack-name>/<kind-id>{hint}"
            )
            continue
        namespace = kind_id.split("/", 1)[0][len("pack:") :]
        if pack_name is not None and namespace != pack_name:
            errors.append(
                f"{label}.id {kind_id} declares namespace {namespace!r} but the pack is named {pack_name!r}"
            )
            continue
        if kind_id in declared:
            errors.append(f"{label}.id {kind_id} is declared more than once")
            continue
        declared[kind_id] = {"label": fields["label"], "description": fields["description"]}
    return declared, errors


def declaration_errors(domain_pack: Any) -> list[str]:
    """Return every problem with a pack's ``request_kinds`` block; empty when valid."""
    _, errors = _collect_declarations(domain_pack)
    return errors


def declared_pack_kinds(config: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Return kinds declared by the workspace's active domain pack, keyed by id.

    Raises :class:`RequestKindError` when the declaration is malformed, so a broken
    pack fails the command instead of silently narrowing the valid set.
    """
    domain_pack = config.get("domain_pack") if isinstance(config, dict) else None
    declared, errors = _collect_declarations(domain_pack)
    if errors:
        raise RequestKindError(
            "CONFIG_INVALID",
            f"research.yml domain_pack.request_kinds is invalid: {errors[0]}",
            remediation="Fix domain_pack.request_kinds as documented in docs/research-yml.md.",
            details={"errors": errors},
        )
    return declared


def valid_kinds(config: dict[str, Any] | None) -> tuple[str, ...]:
    """Return built-in kinds plus the active pack's declared kinds, in stable order."""
    return BUILTIN_REQUEST_KINDS + tuple(sorted(declared_pack_kinds(config)))


def validate_kind(kind: Any, config: dict[str, Any] | None) -> str:
    """Return the normalized kind id, or raise :class:`RequestKindError`.

    ``REQUEST_KIND_INVALID`` means the id is malformed; ``REQUEST_KIND_UNDECLARED``
    means it is well-formed but this workspace's pack does not declare it. The two
    are distinct because they need different fixes: fix the spelling, or declare it.
    """
    if not isinstance(kind, str) or not kind.strip():
        raise RequestKindError("REQUEST_KIND_INVALID", "--kind must be a non-empty request kind id")
    value = kind.strip()
    if value in BUILTIN_REQUEST_KINDS:
        return value

    declared = declared_pack_kinds(config)
    if PACK_REQUEST_KIND_RE.fullmatch(value) is not None:
        if value in declared:
            return value
        raise RequestKindError(
            "REQUEST_KIND_UNDECLARED",
            (
                f"Undeclared source-request kind: {value}. "
                f"This workspace accepts: {', '.join(valid_kinds(config))}."
            ),
            details={"kind": value, "valid_kinds": list(valid_kinds(config))},
        )

    # The change request that introduced pack kinds wrote its examples without the
    # reserved prefix, so a bare id is the predictable first mistake. Name the exact
    # id the caller wants rather than only restating the rule.
    hint = ""
    if BARE_REQUEST_KIND_RE.fullmatch(value) is not None:
        prefixed = f"pack:{value}"
        hint = (
            f" Did you mean {prefixed}?"
            if prefixed in declared
            else f" Pack kinds are namespaced like {prefixed}."
        )
    raise RequestKindError(
        "REQUEST_KIND_INVALID",
        (
            f"Invalid source-request kind: {value}.{hint} "
            f"This workspace accepts: {', '.join(valid_kinds(config))}."
        ),
        details={"kind": value, "valid_kinds": list(valid_kinds(config))},
    )
