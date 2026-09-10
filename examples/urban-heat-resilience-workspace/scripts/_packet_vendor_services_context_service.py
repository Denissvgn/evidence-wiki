#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.context_service.

Original source SHA-256: e9c8c2bfbf0534dd112d2825cb7d4b76cf855abe5a64178869608c2d414263da
Only the validation dependency closure is included; source discovery, producer
execution, persistence, plugins and live reconciliation are excluded.

MIT License

Copyright (c) 2026 Denis Sivagin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

"""

from __future__ import annotations

from _packet_vendor_services_contracts import CONTEXT_KNOWLEDGE_PROTOCOL_VERSION
from _packet_vendor_services_contracts import CONTEXT_PROTOCOL_VERSION
from _packet_vendor_services_knowledge_graph import CORE_RELATIONSHIP_KINDS
from _packet_vendor_services_knowledge_model import ComputedFreshness
from _packet_vendor_services_knowledge_model import EvidenceState
from _packet_vendor_services_knowledge_graph import GRAPH_ORIGINS
from _packet_vendor_services_knowledge_graph import GRAPH_RESOLUTIONS
from _packet_vendor_services_context_knowledge_contract import KNOWLEDGE_MODE_VALUES
import re
import _packet_vendor_services_wiki_surface as wiki_surface


PROTOCOL_VERSION = CONTEXT_PROTOCOL_VERSION


KNOWLEDGE_PROTOCOL_VERSION = CONTEXT_KNOWLEDGE_PROTOCOL_VERSION


CONTEXT_KNOWLEDGE_CONCEPT_LIMIT = 20


CONTEXT_KNOWLEDGE_PAGE_LIMIT = 20


CONTEXT_KNOWLEDGE_RELATIONSHIP_LIMIT = 40


_V1_REQUEST_KEYS = {
    "protocol",
    "budget_tokens",
    "focus",
    "format",
    "filters",
    "prefer_fresh",
}


_V2_REQUEST_KEYS = {*_V1_REQUEST_KEYS, "knowledge_mode"}


_FILTER_KEYS = {
    "language",
    "module",
    "symbol",
    "entrypoint",
    "surface",
    "freshness",
    "evidence",
    "relationship_kind",
    "relationship_origin",
    "relationship_resolution",
    "relationship_direction",
}


_FOCUS_VALUES = {"changed", "neighbors", "all"}


_FORMATS = {"json", "markdown"}


_CONTEXT_QUERY_LIMIT = 20


_CONCEPT_FILTER_KEYS = {"surface", "symbol"}


_KNOWLEDGE_REFINEMENT_KEYS = {"freshness", "evidence"}


_RELATIONSHIP_REFINEMENT_KEYS = {
    "relationship_kind",
    "relationship_origin",
    "relationship_resolution",
    "relationship_direction",
}


_RELATIONSHIP_DIRECTIONS = ("incoming", "outgoing", "both")


_QUALIFIED_RELATIONSHIP_KIND_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9._-]*/[A-Za-z][A-Za-z0-9._-]*$"
)


_FRESHNESS_FILTER_VALUES = {item.value for item in ComputedFreshness}


_EVIDENCE_FILTER_VALUES = {item.value for item in EvidenceState}


class ProtocolRequestError(ValueError):
    """Validation error for Wiki-as-Context protocol requests."""

    def __init__(
        self,
        message: str,
        field: str | None = None,
        *,
        protocol: str = PROTOCOL_VERSION,
    ):
        super().__init__(message)
        self.field = field
        self.protocol = protocol


def _validate_protocol_request(data: object) -> dict:
    """Return a normalised protocol request or raise ``ProtocolRequestError``."""

    protocol = data.get("protocol") if isinstance(data, dict) else PROTOCOL_VERSION
    try:
        return _validate_protocol_request_impl(data)
    except ProtocolRequestError as exc:
        if isinstance(protocol, str) and protocol in {
            PROTOCOL_VERSION,
            KNOWLEDGE_PROTOCOL_VERSION,
        }:
            exc.protocol = protocol
        raise


def _validate_protocol_request_impl(data: object) -> dict:
    if not isinstance(data, dict):
        raise ProtocolRequestError("Request must be a JSON object.", "request")
    if any(not isinstance(key, str) for key in data):
        raise ProtocolRequestError(
            "Request field names must be strings.",
            "request",
        )

    protocol = data.get("protocol")
    if protocol == PROTOCOL_VERSION:
        request_keys = _V1_REQUEST_KEYS
    elif protocol == KNOWLEDGE_PROTOCOL_VERSION:
        request_keys = _V2_REQUEST_KEYS
    else:
        raise ProtocolRequestError(
            f"Unsupported protocol: {protocol!r}. Expected {PROTOCOL_VERSION!r} "
            f"or {KNOWLEDGE_PROTOCOL_VERSION!r}.",
            "protocol",
        )

    unknown = sorted(set(data) - request_keys)
    if unknown:
        raise ProtocolRequestError(
            f"Unknown request field: {unknown[0]}",
            unknown[0],
            protocol=protocol,
        )

    if "budget_tokens" not in data:
        raise ProtocolRequestError(
            "Missing required field: budget_tokens", "budget_tokens"
        )
    budget = data["budget_tokens"]
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ProtocolRequestError(
            "budget_tokens must be a positive integer.", "budget_tokens"
        )

    fmt = data.get("format", "json")
    if not isinstance(fmt, str) or fmt not in _FORMATS:
        raise ProtocolRequestError("format must be 'json' or 'markdown'.", "format")

    focus = _normalise_protocol_focus(data.get("focus", ["changed", "neighbors"]))
    filters = _normalise_protocol_filters(data.get("filters", {}))
    prefer_fresh = data.get("prefer_fresh", False)
    if not isinstance(prefer_fresh, bool):
        raise ProtocolRequestError(
            "prefer_fresh must be a boolean.",
            "prefer_fresh",
        )

    normalized = {
        "protocol": protocol,
        "budget_tokens": budget,
        "focus": focus,
        "format": fmt,
        "filters": filters,
        "prefer_fresh": prefer_fresh,
    }
    if protocol == KNOWLEDGE_PROTOCOL_VERSION:
        if "knowledge_mode" not in data:
            raise ProtocolRequestError(
                "Missing required field: knowledge_mode",
                "knowledge_mode",
                protocol=protocol,
            )
        knowledge_mode = data["knowledge_mode"]
        if not isinstance(knowledge_mode, str) or knowledge_mode not in (
            KNOWLEDGE_MODE_VALUES
        ):
            choices = ", ".join(repr(value) for value in KNOWLEDGE_MODE_VALUES)
            raise ProtocolRequestError(
                f"knowledge_mode must be one of {choices}.",
                "knowledge_mode",
                protocol=protocol,
            )
        normalized["knowledge_mode"] = knowledge_mode
    return normalized


def _normalise_protocol_focus(raw_focus: object) -> list[str]:
    if not isinstance(raw_focus, list) or not raw_focus:
        raise ProtocolRequestError("focus must be a non-empty list.", "focus")

    if any(not isinstance(item, str) for item in raw_focus):
        raise ProtocolRequestError("focus values must be strings.", "focus")

    unknown = sorted(set(raw_focus) - _FOCUS_VALUES)
    if unknown:
        raise ProtocolRequestError(f"Unknown focus value: {unknown[0]}", "focus")

    if len(set(raw_focus)) != len(raw_focus):
        raise ProtocolRequestError("focus values must not be duplicated.", "focus")

    focus_set = set(raw_focus)
    if "all" in focus_set and len(focus_set) > 1:
        raise ProtocolRequestError(
            "focus 'all' cannot be combined with other values.", "focus"
        )

    if "neighbors" in focus_set and "changed" not in focus_set:
        raise ProtocolRequestError("focus 'neighbors' requires 'changed'.", "focus")

    if "all" in focus_set:
        return ["all"]

    ordered = ["changed"] if "changed" in focus_set else []
    if "neighbors" in focus_set:
        ordered.append("neighbors")
    return ordered


def _normalise_protocol_filters(raw_filters: object) -> dict:
    if raw_filters is None:
        return {}
    if not isinstance(raw_filters, dict):
        raise ProtocolRequestError("filters must be a JSON object.", "filters")
    if any(not isinstance(key, str) for key in raw_filters):
        raise ProtocolRequestError(
            "filter field names must be strings.",
            "filters",
        )

    unknown = sorted(set(raw_filters) - _FILTER_KEYS)
    if unknown:
        raise ProtocolRequestError(
            f"Unknown filter field: {unknown[0]}", f"filters.{unknown[0]}"
        )

    filters: dict[str, str] = {}
    for key in (
        "language",
        "module",
        "symbol",
        "entrypoint",
        "surface",
        "freshness",
        "evidence",
        "relationship_kind",
        "relationship_origin",
        "relationship_resolution",
        "relationship_direction",
    ):
        if key not in raw_filters:
            continue
        value = raw_filters[key]
        if not isinstance(value, str) or not value:
            raise ProtocolRequestError(
                f"filters.{key} must be a non-empty string.", f"filters.{key}"
            )
        if key == "surface":
            _validate_surface_filter(value)
        elif key == "freshness":
            _validate_enum_filter(
                key,
                value,
                _FRESHNESS_FILTER_VALUES,
            )
        elif key == "evidence":
            _validate_enum_filter(
                key,
                value,
                _EVIDENCE_FILTER_VALUES,
            )
        elif key == "relationship_kind":
            _validate_relationship_kind_filter(value)
        elif key == "relationship_origin":
            _validate_enum_filter(key, value, set(GRAPH_ORIGINS))
        elif key == "relationship_resolution":
            _validate_enum_filter(key, value, set(GRAPH_RESOLUTIONS))
        elif key == "relationship_direction":
            _validate_enum_filter(
                key,
                value,
                set(_RELATIONSHIP_DIRECTIONS),
            )
        filters[key] = value

    filter_keys = set(filters)
    refinements = _KNOWLEDGE_REFINEMENT_KEYS & filter_keys
    if refinements and not (_CONCEPT_FILTER_KEYS & filter_keys):
        field = next(key for key in ("freshness", "evidence") if key in refinements)
        raise ProtocolRequestError(
            f"filters.{field} requires filters.surface or filters.symbol.",
            f"filters.{field}",
        )
    relationship_refinements = _RELATIONSHIP_REFINEMENT_KEYS & filter_keys
    if relationship_refinements and not (_CONCEPT_FILTER_KEYS & filter_keys):
        field = next(
            key
            for key in (
                "relationship_kind",
                "relationship_origin",
                "relationship_resolution",
                "relationship_direction",
            )
            if key in relationship_refinements
        )
        raise ProtocolRequestError(
            f"filters.{field} requires filters.surface or filters.symbol.",
            f"filters.{field}",
        )
    return filters


def _validate_surface_filter(value: str) -> None:
    known = {entry.kind.value for entry in wiki_surface.iter_page_kinds()}
    if value not in known:
        raise ProtocolRequestError(
            f"filters.surface must be one of: {', '.join(sorted(known))}.",
            "filters.surface",
        )


def _validate_enum_filter(key: str, value: str, known: set[str]) -> None:
    if value not in known:
        raise ProtocolRequestError(
            f"filters.{key} must be one of: {', '.join(sorted(known))}.",
            f"filters.{key}",
        )


def _validate_relationship_kind_filter(value: str) -> None:
    if (
        value not in CORE_RELATIONSHIP_KINDS
        and _QUALIFIED_RELATIONSHIP_KIND_RE.fullmatch(value) is None
    ):
        raise ProtocolRequestError(
            "filters.relationship_kind must be a core relationship kind or "
            "a qualified plugin kind such as 'vendor.plugin/relationship'.",
            "filters.relationship_kind",
        )
