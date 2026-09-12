#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.knowledge_envelope.

Original source SHA-256: bd9c2ffa838637c3ee64bcf4701fce4de101252054914acdd8db2bf4fa572aea
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

from _packet_vendor_services_knowledge_model import REPOSITORY_IDENTITY_PATTERN
import re


_REPOSITORY_IDENTITY_RE = re.compile(REPOSITORY_IDENTITY_PATTERN)


class KnowledgeEnvelopeError(ValueError):
    """Field-specific validation failure while constructing an envelope."""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def validate_configured_public_identity(value: object) -> str:
    """Validate one explicitly configured public repository identity."""

    if not isinstance(value, str) or value == "unknown":
        raise KnowledgeEnvelopeError(
            "configured_public_identity",
            "must be a qualified public namespace path",
        )
    if (
        value != value.strip()
        or _REPOSITORY_IDENTITY_RE.fullmatch(value) is None
        or value.casefold().endswith(".git")
    ):
        raise KnowledgeEnvelopeError(
            "configured_public_identity",
            "must be a normalized public namespace path without scheme, "
            "credentials, port, query, fragment, dot segment, or '.git' suffix",
        )
    return value
