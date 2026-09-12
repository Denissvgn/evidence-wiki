#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.knowledge_evidence.

Original source SHA-256: d635e3bf0355413a89190af0c572671ea025ec3170794d7759a824757b1fb7c0
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
import hashlib
import json
import re


SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


_SHA256_RE = re.compile(SHA256_PATTERN)


def is_valid_sha256(value: object) -> bool:
    """Return whether *value* is a canonical ``sha256:<lowercase-hex>`` string."""

    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def canonical_json_text(value: Any) -> str:
    """Encode *value* as compact canonical JSON for hashing and ordering."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return the UTF-8 bytes of :func:`canonical_json_text`."""

    return canonical_json_text(value).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """Return the canonical SHA-256 wire value for *value*."""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"
