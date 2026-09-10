#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.knowledge_graph.

Original source SHA-256: 57e83b49303a376168f2e93e475a2e63178a975aec7c25772077a8304ac28840
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

import re


CORE_RELATIONSHIP_KINDS = (
    "contains",
    "imports",
    "calls",
    "entrypoint_for",
    "reads",
    "writes",
    "depends_on",
    "supersedes",
)


GRAPH_ORIGINS = ("extracted", "inferred", "markdown", "governance")


GRAPH_RESOLUTIONS = ("resolved", "ambiguous", "external", "unresolved")


GRAPH_EVIDENCE_STATES = ("present", "unknown", "missing", "invalid")


ENDPOINT_KINDS = ("concept", "source-symbol", "external-resource", "unresolved")


GRAPH_COVERAGE_ANALYZERS = (
    "calls",
    "concept-map",
    "data-flows",
    "dependencies",
    "entrypoints",
    "external-dependencies",
    "flows",
)


_QUALIFIED_NAME_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9._-]*/[A-Za-z][A-Za-z0-9._-]*$"
)


def is_supported_relationship_kind(value: object) -> bool:
    """Return whether *value* is a core or qualified relationship kind."""

    return isinstance(value, str) and (
        value in CORE_RELATIONSHIP_KINDS
        or _QUALIFIED_NAME_RE.fullmatch(value) is not None
    )
