#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 4.0.0: llm_wiki_cli.services.maintenance_status.

Original source SHA-256: 410a189114aab518c6fdaf960966bf671ad121aea2b03499e897f4f518b76f79
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

from collections.abc import Mapping
from copy import deepcopy


LIMITATIONS = (
    "ordinary-acknowledgment-is-not-human-governance-review",
    "captured-syntax-is-not-runtime-equivalence",
    "captured-prose-is-not-a-correctness-verdict",
    "snapshot-status-does-not-establish-current-acknowledgment",
)


ANALYSIS_SCOPES = frozenset(
    {"snapshot", "selected-source", "full-inventory", "selected-header"}
)


SUMMARY_FIELDS = frozenset(
    {
        "availability",
        "analysis_scope",
        "live_evaluation_performed",
        "native_prose_coherent",
        "requires_sync",
        "total",
        "pending",
        "acknowledged",
        "unavailable",
        "ambiguous",
        "deleted",
        "untracked",
        "truncated",
        "limitations",
    }
)


def validate_maintenance_summary(value) -> dict:
    if not isinstance(value, Mapping) or set(value) != SUMMARY_FIELDS:
        raise ValueError("Maintenance summary fields must match the closed contract")
    if (
        not isinstance(value["availability"], str)
        or value["availability"] not in {"ready", "partial", "unadopted", "unavailable"}
        or not isinstance(value["analysis_scope"], str)
        or value["analysis_scope"] not in ANALYSIS_SCOPES
    ):
        raise ValueError("Unsupported maintenance summary qualification")
    for key in (
        "live_evaluation_performed",
        "native_prose_coherent",
        "requires_sync",
        "truncated",
    ):
        if type(value[key]) is not bool:
            raise ValueError("Maintenance summary flags must be booleans")
    for key in (
        "total",
        "pending",
        "acknowledged",
        "unavailable",
        "ambiguous",
        "deleted",
        "untracked",
    ):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("Maintenance summary counts must be nonnegative integers")
    if (
        value["requires_sync"] != (not value["native_prose_coherent"])
        or not isinstance(value["limitations"], (list, tuple))
        or list(value["limitations"]) != list(LIMITATIONS)
    ):
        raise ValueError("Maintenance summary limitations/coherence disagree")
    if (
        value["analysis_scope"] in {"snapshot", "selected-header"}
        and value["live_evaluation_performed"]
    ):
        raise ValueError("Snapshot/header maintenance cannot claim live evaluation")
    if (
        any(
            value[key] > value["total"]
            for key in (
                "pending",
                "acknowledged",
                "unavailable",
                "ambiguous",
                "deleted",
            )
        )
        or value["pending"] + value["acknowledged"] > value["total"]
    ):
        raise ValueError("Maintenance summary subgroup counts exceed their total")
    if not value["live_evaluation_performed"] and value["acknowledged"]:
        raise ValueError("Snapshot maintenance cannot claim current acknowledgment")
    return deepcopy(dict(value))
