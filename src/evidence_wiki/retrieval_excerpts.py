"""Inert exact body excerpts; owner selection and permission remain separate."""

from __future__ import annotations

import hashlib
import re


def excerpt(body: str, query: str, *, width: int = 1200) -> dict:
    if (type(body) is not str or len(body.encode('utf-8')) > 16_777_216
            or type(query) is not str or not 1 <= len(query) <= 4096
            or type(width) is not int or not 128 <= width <= 4096):
        raise ValueError('excerpt_input_bound')
    terms = re.findall(r'\w+', query, re.UNICODE)[:128]
    matches = [match.start() for term in terms
        if (match := re.search(re.escape(term), body, re.IGNORECASE)) is not None]
    pivot = min(matches, default=0)
    start = max(0, pivot - width // 3)
    end = min(len(body), start + width)
    text = body[start:end]
    return {'text': text, 'start_char': start, 'end_char': end,
        'start_line': body.count('\n', 0, start) + 1,
        'end_line': body.count('\n', 0, max(start, end - 1)) + 1,
        'prefix_omitted': start > 0, 'suffix_omitted': end < len(body),
        'excerpt_sha256': 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'location_basis': 'exact_normalized_body', 'original_location': 'not_inferred',
        'semantic_adequacy': 'not_evaluated'}
