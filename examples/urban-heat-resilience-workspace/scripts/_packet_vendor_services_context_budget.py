#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 4.0.0: llm_wiki_cli.services.context_budget.

Original source SHA-256: b89ec3626046b95a04581d933f3ef21e46b97554fc6f4a45a53b7bbcc635e6c8
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

from typing import Any
from _packet_vendor_services_contracts import CONTEXT_BUDGET_PROTOCOL_VERSION
from typing import Mapping
import _packet_vendor_services_context_service as context
from _packet_vendor_services_change_selection import validate_changes


def validate_request(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping) or any(not isinstance(key, str) for key in data):
        raise context.ProtocolRequestError("Request must be an object with string keys", "request")
    if data.get("protocol", CONTEXT_BUDGET_PROTOCOL_VERSION) != CONTEXT_BUDGET_PROTOCOL_VERSION:
        raise context.ProtocolRequestError("Unsupported context budget protocol", "protocol")
    allowed = context._V2_REQUEST_KEYS | {"budget_mode", "counter_id", "changes"}
    unknown = set(data) - allowed
    if unknown:
        raise context.ProtocolRequestError(
            f"Unknown request field: {sorted(unknown)[0]}", sorted(unknown)[0]
        )
    legacy = dict(data)
    legacy["protocol"] = context.KNOWLEDGE_PROTOCOL_VERSION
    legacy["knowledge_mode"] = data.get("knowledge_mode", "off")
    legacy["format"] = (
        "json" if data.get("format") == "packet" else data.get("format", "json")
    )
    legacy.pop("budget_mode", None)
    legacy.pop("counter_id", None)
    legacy.pop("changes", None)
    normalized = context._validate_protocol_request(legacy)
    mode = data.get("budget_mode", "exact")
    if not isinstance(mode, str) or mode not in {"exact", "estimated"}:
        raise context.ProtocolRequestError(
            "budget_mode must be exact or estimated", "budget_mode"
        )
    identity = data.get("counter_id")
    if identity is not None and (not isinstance(identity, str) or not identity):
        raise context.ProtocolRequestError(
            "counter_id must be a nonempty string", "counter_id"
        )
    changes = None
    if "changes" in data:
        try:
            changes = validate_changes(data["changes"])
        except ValueError as exc:
            raise context.ProtocolRequestError(str(exc), "changes") from exc
    return {
        **normalized,
        **({"changes": changes} if changes is not None else {}),
        "protocol": CONTEXT_BUDGET_PROTOCOL_VERSION,
        "format": data.get("format", "json"),
        "budget_mode": mode,
        "counter_id": identity,
    }
