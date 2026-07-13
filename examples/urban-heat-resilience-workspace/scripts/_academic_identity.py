#!/usr/bin/env python3
"""Academic identity comparison helpers shared by acquisition and verification."""

from __future__ import annotations

import unicodedata
from typing import Any


def unicode_fold(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(without_marks.casefold().replace(".", "").split())


def canonical_person_name(value: str | None) -> dict[str, Any]:
    """Return a comparison form for a human name without weakening identity."""
    text = unicode_fold(value)
    if "," in text:
        family, given = text.split(",", 1)
        text = " ".join(part for part in (given.strip(), family.strip()) if part)
        text = " ".join(text.split())
    tokens = tuple(token for token in text.split() if token)
    family = tokens[-1] if tokens else ""
    given = tokens[:-1] if len(tokens) > 1 else tuple()
    return {
        "canonical": text,
        "tokens": tokens,
        "token_set": frozenset(tokens),
        "family": family,
        "given": given,
    }


def given_tokens_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) == 1 and right.startswith(left):
        return True
    if len(right) == 1 and left.startswith(right):
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= 4 and (longer.startswith(shorter) or longer.endswith(shorter))


def given_token_groups_compatible(local_given: tuple[str, ...], provider_given: tuple[str, ...]) -> bool:
    if not local_given or not provider_given:
        return not local_given and not provider_given
    return all(any(given_tokens_compatible(left, right) for right in provider_given) for left in local_given) and all(
        any(given_tokens_compatible(right, left) for left in local_given) for right in provider_given
    )


def person_names_match(local_name: str, provider_name: str) -> tuple[bool, str | None]:
    local = canonical_person_name(local_name)
    provider = canonical_person_name(provider_name)
    if not local["tokens"] or not provider["tokens"]:
        return False, None
    if local["token_set"] == provider["token_set"]:
        return True, "canonical_tokens"
    if local["family"] and local["family"] == provider["family"]:
        if given_token_groups_compatible(local["given"], provider["given"]):
            return True, "family_given_compatible"
    return False, None


def author_sets_match(local_authors: list[str], provider_authors: list[str]) -> dict[str, Any]:
    local_forms = [canonical_person_name(name) for name in local_authors]
    provider_forms = [canonical_person_name(name) for name in provider_authors]
    pairings: dict[int, list[tuple[int, str]]] = {}
    for local_index, local_name in enumerate(local_authors):
        for provider_index, provider_name in enumerate(provider_authors):
            matched, rule = person_names_match(local_name, provider_name)
            if matched and rule:
                pairings.setdefault(local_index, []).append((provider_index, rule))

    ordered_matches: list[dict[str, str]] = []
    used_provider_indexes: set[int] = set()

    def assign(local_index: int) -> bool:
        if local_index == len(local_authors):
            return True
        for provider_index, rule in pairings.get(local_index, []):
            if provider_index in used_provider_indexes:
                continue
            used_provider_indexes.add(provider_index)
            ordered_matches.append(
                {
                    "local": str(local_forms[local_index]["canonical"]),
                    "provider": str(provider_forms[provider_index]["canonical"]),
                    "rule": rule,
                }
            )
            if assign(local_index + 1):
                return True
            ordered_matches.pop()
            used_provider_indexes.remove(provider_index)
        return False

    matched = bool(local_authors) and len(local_authors) <= len(provider_authors) and assign(0)
    unmatched_local: list[str] = []
    unmatched_provider: list[str] = []
    if not matched:
        unmatched_local = [str(form["canonical"]) for form in local_forms]
        unmatched_provider = [str(form["canonical"]) for form in provider_forms]
    else:
        unmatched_provider = [
            str(form["canonical"]) for index, form in enumerate(provider_forms) if index not in used_provider_indexes
        ]

    return {
        "matched": matched,
        "matches": ordered_matches if matched else [],
        "unmatched_local": unmatched_local,
        "unmatched_provider": unmatched_provider,
    }
