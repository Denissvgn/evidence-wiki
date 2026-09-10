# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.contracts.

Original source SHA-256: 2832b372f0ed78089e2746fa9b27cd16b6690920214cdd026f6e3cb3a42c6151
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




CONTEXT_PROTOCOL_VERSION = "llm-wiki-context/v1"


CONTEXT_KNOWLEDGE_PROTOCOL_VERSION = "llm-wiki-context/v2"


QUALIFIED_CONTEXT_PACKET_SCHEMA_VERSION = "llm-wiki-qualified-context-packet/v1"


QUALIFIED_CONTEXT_PACKET_KNOWLEDGE_SCHEMA_VERSION = (
    "llm-wiki-qualified-context-packet/v2"
)


TYPED_GRAPH_SCHEMA_VERSION = "llm-wiki-typed-graph/v1"
