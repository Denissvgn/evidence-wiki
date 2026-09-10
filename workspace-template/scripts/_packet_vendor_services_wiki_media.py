# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 1.8.0: llm_wiki_cli.services.wiki_media.

Original source SHA-256: e566f218467ce2f483cce0ee7a7f1b0e304760b4b8bdc957561943736749e48b
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
from urllib.parse import urlsplit


_AUTHORITY_USERINFO_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^/?#\s<>'\"]*@")


_URI_AUTHORITY_PREFIX_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//")


_URI_TOKEN_START_RE = re.compile(
    r"(?:^|[^A-Za-z0-9._~/%+-])"
    r"(?P<uri><?(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)"
)


def contains_uri_authority_userinfo(value: str) -> bool:
    """Detect authority userinfo without scanning URI query/fragment values.

    The first Markdown destination token is inspected as one URI. Any
    whitespace-delimited trailing text is inspected as separate URI tokens,
    covering supported titles and malformed scanner tails without interpreting
    nested URI-looking query or fragment data as another authority.
    """

    text = value.strip()
    if text.startswith("<") and ">" in text:
        destination_end = text.index(">") + 1
        destination = text[:destination_end]
        tail = text[destination_end:].strip()
    else:
        parts = text.split(maxsplit=1)
        destination = parts[0] if parts else ""
        tail = parts[1] if len(parts) == 2 else ""
    if destination.startswith("<"):
        destination = destination[1:]
    if destination.endswith(">"):
        destination = destination[:-1]
    destination = destination.strip()
    if _uri_candidate_contains_authority_userinfo(destination):
        return True

    for token in tail.split():
        candidate = token.lstrip("\"'(<[")
        if _URI_AUTHORITY_PREFIX_RE.match(candidate):
            if _uri_candidate_contains_authority_userinfo(candidate):
                return True
            continue
        match = _URI_TOKEN_START_RE.search(candidate)
        if match is not None:
            uri_candidate = candidate[match.start("uri") :].lstrip("<")
            if _uri_candidate_contains_authority_userinfo(uri_candidate):
                return True
    return False


def _uri_candidate_contains_authority_userinfo(candidate: str) -> bool:
    if _AUTHORITY_USERINFO_RE.match(candidate):
        return True
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    return bool(
        parsed.netloc and (parsed.username is not None or parsed.password is not None)
    )
