#!/usr/bin/env python3
"""Structured-view sidecars: pointer resolution and canonical equality for grounding anchors.

Grounding by quote proves a record contains a sentence. For structured evidence — a JSON
price series, a CSV price history — that is provenance without relevance: a containment
check is satisfied by any line of the cited section, whatever value the claim asserts.
Anchor-form grounding closes that gap by naming one field and one value::

    grounding:
      - claim: "Current supplier price is 23.99 EUR"
        source_id: data--keepa--b0abc123
        anchor:
          pointer: "supplier_quote/price"
          expected: "23.99 EUR"

Anchors resolve against the **structured-view sidecar** — ``sources/normalized/<safe
source id>.structured.json``, the complete uncapped JSON rendering the normalizer emits
beside the ``.md`` record and binds to it through the record's ``structured_view: {path,
content_hash}`` frontmatter block. Resolving there rather than against the raw payload
keeps pointers facet-shaped instead of provider-payload-shaped, and keeps this module
format-free: it always reads exactly one JSON object, whatever the source kind was.

Three deterministic steps decide an anchor, and this module owns all three and nothing
else: load the sidecar under its hash binding, resolve an RFC 6901 pointer to one value,
and compare that value with ``expected`` by canonical **equality** — never containment,
which is the weakness anchors exist to remove.

Failures are returned as typed results rather than raised. Each maps to one stable
per-entry ``result`` in the verifier's report, because a bad anchor is a finding about
one grounding entry, not a failure of the run that found it.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_module_loader import load_workspace_module

# `_normalized_contract` does not import this module, so binding it at import time is
# safe and keeps the filesystem-safe id rule in exactly one place: a sidecar that did
# not sit beside its record under the same safe id could not be found by source id.
_normalized_contract = load_workspace_module(_SCRIPT_DIR, "_normalized_contract")
safe_source_id = _normalized_contract.safe_source_id

# `verify_quotes` imports this module to verify anchor entries, so importing it back at
# module scope would close an import cycle and break both. It is loaded lazily instead,
# inside the one function that needs its text normalization; by the time any caller
# reaches that function `verify_quotes` is fully loaded, so the cycle never forms.
# Do not lift this to the top of the file.
_SIBLING_CACHE: dict[str, ModuleType] = {}

# Per-entry verification results. These strings are machine-readable API — hosts switch
# on them — so they are never renamed, only added to.
RESULT_STRUCTURED_VIEW_MISSING = "structured_view_missing"
RESULT_STRUCTURED_VIEW_CORRUPT = "structured_view_corrupt"
RESULT_ANCHOR_POINTER_NOT_FOUND = "anchor_pointer_not_found"
RESULT_ANCHOR_TARGET_NOT_SCALAR = "anchor_target_not_scalar"
RESULT_ANCHOR_VALUE_MISMATCH = "anchor_value_mismatch"

ANCHOR_RESULTS = (
    RESULT_STRUCTURED_VIEW_MISSING,
    RESULT_STRUCTURED_VIEW_CORRUPT,
    RESULT_ANCHOR_POINTER_NOT_FOUND,
    RESULT_ANCHOR_TARGET_NOT_SCALAR,
    RESULT_ANCHOR_VALUE_MISMATCH,
)

STRUCTURED_VIEW_FIELD = "structured_view"
SIDECAR_SUFFIX = ".structured.json"
# Bound from the contract, not restated: it owns the digest format a binding is written
# in, and a second copy here is a second place for the writer's idea of a valid digest to
# drift from the reader's.
CONTENT_HASH_RE = _normalized_contract.CONTENT_HASH_RE

# A plain decimal literal, deliberately narrower than what `Decimal` itself parses.
_DECIMAL_LITERAL_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
# RFC 6901 array steps are decimal indices with no leading zeros; `-` (the append
# token) and signed forms have no referent and are rejected with them.
_ARRAY_INDEX_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
# In a reference token `~` is legal only as `~0` or `~1`; anything else is malformed.
_INVALID_ESCAPE_RE = re.compile(r"~(?![01])")

_JSON_TYPE_NAMES = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "number",
    float: "number",
    type(None): "null",
}


@dataclass(frozen=True)
class SidecarLoad:
    """A structured view that loaded, or the reason it did not."""

    ok: bool
    result: str | None = None
    detail: str | None = None
    document: dict[str, Any] | None = None


@dataclass(frozen=True)
class PointerResolution:
    """A pointer that reached a value, or the reason it reached none.

    ``pointer`` is always the normalized form of what was asked for, so a report can
    echo the pointer that was actually walked rather than the author's shorthand.
    ``value`` is meaningful only when ``ok``: ``None`` is itself a resolvable value.
    """

    ok: bool
    pointer: str
    value: Any = None
    result: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class AnchorResolution:
    """The whole verdict on one anchor: load, resolve, compare.

    ``result`` is one of the module's ``RESULT_*`` constants when ``ok`` is false and
    ``None`` when it is true. ``detail`` is written for a human and is populated on
    every path, so a caller can use it as its report message either way. ``pointer``
    carries the normalized pointer; ``resolved`` the canonical rendering of the target,
    when one was reached.
    """

    ok: bool
    result: str | None = None
    detail: str | None = None
    pointer: str | None = None
    resolved: str | None = None


def _sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def _normalize_text(value: str) -> str:
    """Fold a string with the codebase's one deterministic text normalization.

    Deferred import on purpose — see the `_SIBLING_CACHE` note at the top of the module.
    """
    return _sibling_module("verify_quotes").normalize_quote_text(value)


def _json_type_name(value: Any) -> str:
    """Name a value the way the JSON document that carried it would."""
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


# Same reason as `CONTENT_HASH_RE`: the contract computes the digest a record binds, and
# this module verifies it, so both have to be the same line of code.
content_hash = _normalized_contract.content_hash


def sidecar_path(normalized_root: Path, source_id: str) -> Path:
    """Where the structured view for ``source_id`` must live to be resolvable by id."""
    return normalized_root / f"{safe_source_id(source_id)}{SIDECAR_SUFFIX}"


def structured_view_binding(frontmatter: Any) -> tuple[str, str] | None:
    """The record's declared ``(path, content_hash)`` binding, or ``None`` when it declares none.

    A record that declares nothing and a record that declares a malformed block are the
    same case for a reader: neither offers a structured view to resolve against. Whether
    a malformed declaration is *also* a contract violation is `_normalized_contract`'s
    question, reported by `normalize_verify.py`, not this reader's.
    """
    if not isinstance(frontmatter, dict):
        return None
    block = frontmatter.get(STRUCTURED_VIEW_FIELD)
    if not isinstance(block, dict):
        return None
    declared_path = block.get("path")
    declared_hash = block.get("content_hash")
    if not isinstance(declared_path, str) or not declared_path.strip():
        return None
    if not isinstance(declared_hash, str) or not declared_hash.strip():
        return None
    return declared_path.strip(), declared_hash.strip()


def load_sidecar(frontmatter: dict[str, Any], path: Path) -> SidecarLoad:
    """Read the structured view bound to a normalized record, or say why it cannot be trusted.

    Checks run in the order a reader would ask them, each with its own reason: is a view
    declared at all, is the file there, do its bytes match the digest the record binds
    them to, and is what it holds exactly one JSON object. The hash check is what makes
    an anchor's evidence the record's own — without it the sidecar is an unattested file
    that happens to sit next to one.

    The caller chooses which file to read (normally `sidecar_path`); agreement between
    that location and the declared ``path`` is a contract question `normalize_verify.py`
    answers over the whole workspace, not a per-anchor one.
    """
    binding = structured_view_binding(frontmatter)
    if binding is None:
        return SidecarLoad(
            False,
            RESULT_STRUCTURED_VIEW_MISSING,
            "The normalized record declares no structured_view block with a path and a content_hash, "
            "so it carries no structured view an anchor can resolve against.",
        )
    declared_path, declared_hash = binding
    if not path.is_file():
        return SidecarLoad(
            False,
            RESULT_STRUCTURED_VIEW_MISSING,
            f"The record declares a structured view at {declared_path}, "
            "but no sidecar file is present at the record's structured-view location.",
        )
    if CONTENT_HASH_RE.fullmatch(declared_hash) is None:
        return SidecarLoad(
            False,
            RESULT_STRUCTURED_VIEW_CORRUPT,
            "The record's structured_view.content_hash is not a sha256:<64 lowercase hex digits> "
            f"digest: {declared_hash!r}.",
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        return SidecarLoad(
            False,
            RESULT_STRUCTURED_VIEW_CORRUPT,
            f"The structured view at {declared_path} could not be read: {exc.strerror or exc}.",
        )
    actual_hash = content_hash(data)
    if actual_hash != declared_hash:
        return SidecarLoad(
            False,
            RESULT_STRUCTURED_VIEW_CORRUPT,
            f"The structured view at {declared_path} hashes to {actual_hash}, "
            f"not the {declared_hash} its record binds it to.",
        )
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return SidecarLoad(
            False,
            RESULT_STRUCTURED_VIEW_CORRUPT,
            f"The structured view at {declared_path} is not valid UTF-8 JSON: {exc}.",
        )
    if not isinstance(document, dict):
        return SidecarLoad(
            False,
            RESULT_STRUCTURED_VIEW_CORRUPT,
            f"The structured view at {declared_path} must hold exactly one JSON object; "
            f"it holds a JSON {_json_type_name(document)}.",
        )
    return SidecarLoad(True, document=document)


def normalize_pointer(pointer: Any) -> str:
    """The RFC 6901 form of an author's pointer.

    The leading ``/`` is optional because the CR's own example writes
    ``supplier_quote/price``; it is prepended when absent. The empty pointer is left
    empty: under the RFC it refers to the whole document, while ``/`` refers to the
    member whose key is the empty string, so the two must not be conflated.
    """
    text = pointer if isinstance(pointer, str) else ""
    if text == "" or text.startswith("/"):
        return text
    return "/" + text


def unescape_token(token: str) -> str | None:
    """Decode one reference token, or ``None`` when its ``~`` escaping is invalid.

    ``~1`` becomes ``/`` before ``~0`` becomes ``~``. The reverse order would decode
    ``~01`` to ``/`` instead of the literal ``~1`` the RFC requires, which is the
    classic way to build a pointer that silently addresses the wrong field.
    """
    if _INVALID_ESCAPE_RE.search(token) is not None:
        return None
    return token.replace("~1", "/").replace("~0", "~")


def _pointer_prefix(tokens: list[str], depth: int) -> str:
    """How far the walk got, named the way the pointer named it."""
    if depth == 0:
        return "the document root"
    return "/" + "/".join(tokens[:depth])


def _pointer_not_found(pointer: str, detail: str) -> PointerResolution:
    return PointerResolution(
        ok=False,
        pointer=pointer,
        result=RESULT_ANCHOR_POINTER_NOT_FOUND,
        detail=detail,
    )


def resolve_pointer(obj: Any, pointer: str) -> PointerResolution:
    """Walk an RFC 6901 pointer into a parsed structured view.

    Object steps match a key exactly: no case folding, no prefix or fuzzy matching. An
    anchor is a *reference*, and a reference that can select a neighbouring field is the
    coincidental-match failure anchors exist to remove. Array steps are decimal indices
    only, so ``01``, ``+1``, ``-1`` and the RFC's ``-`` append token — which by
    definition has no referent — are all rejected rather than guessed at.
    """
    normalized = normalize_pointer(pointer)
    if not isinstance(pointer, str):
        # Entry parsing rejects a non-string pointer outright; reaching here means a
        # caller skipped it. Say so rather than silently reading it as the root pointer.
        return _pointer_not_found(normalized, f"A pointer must be a string; got a {_json_type_name(pointer)}.")
    if normalized == "":
        return PointerResolution(ok=True, pointer=normalized, value=obj)
    tokens = normalized.split("/")[1:]
    current = obj
    for depth, raw_token in enumerate(tokens):
        token = unescape_token(raw_token)
        location = _pointer_prefix(tokens, depth)
        if token is None:
            return _pointer_not_found(
                normalized,
                f"Reference token {raw_token!r} is not valid RFC 6901 escaping: "
                "inside a token, ~ is written ~0 and / is written ~1.",
            )
        if isinstance(current, dict):
            if token not in current:
                return _pointer_not_found(
                    normalized,
                    f"The structured view has no key {token!r} at {location}.",
                )
            current = current[token]
            continue
        if isinstance(current, list):
            if _ARRAY_INDEX_RE.fullmatch(token) is None:
                return _pointer_not_found(
                    normalized,
                    f"{location} is a JSON array, so step {token!r} must be a decimal index "
                    "without a sign or leading zeros.",
                )
            index = int(token)
            if index >= len(current):
                return _pointer_not_found(
                    normalized,
                    f"{location} is a JSON array of {len(current)} entries, so index {index} has no referent.",
                )
            current = current[index]
            continue
        return _pointer_not_found(
            normalized,
            f"{location} resolves to a JSON {_json_type_name(current)}, "
            f"so step {token!r} has nothing to resolve against.",
        )
    return PointerResolution(ok=True, pointer=normalized, value=current)


def is_scalar(value: Any) -> bool:
    """True for the JSON values an anchor may cite: string, number, boolean, null."""
    return value is None or isinstance(value, (str, int, float, bool))


def canonical_scalar(value: Any) -> str | None:
    """Canonical rendering of a scalar, or ``None`` when the value is not one.

    ``bool`` is tested before the number branch deliberately: Python makes ``bool`` a
    subclass of ``int``, so a number-first branch renders ``True`` as ``"1"`` and would
    let an anchor expecting the number one match a boolean field.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float, str)):
        return str(value)
    return None


def parse_decimal(text: Any) -> Decimal | None:
    """Read a plain decimal literal, or ``None`` when the text is not one.

    The literal gate is narrower than ``Decimal``'s own parser, which also accepts
    ``NaN``, ``sNaN``, ``Infinity`` and ``_`` digit grouping. None of those is a value a
    source record states, and admitting them would let an anchor claim to have compared
    something the evidence never said. ``Decimal("NaN") == Decimal("NaN")`` is already
    false, so this changes no verdict — it just refuses to depend on that accident.
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if _DECIMAL_LITERAL_RE.fullmatch(candidate) is None:
        return None
    try:
        value = Decimal(candidate)
    except InvalidOperation:  # pragma: no cover - the literal gate already excludes these
        return None
    return value if value.is_finite() else None


def _decimal_matches(target: int | float, expected_text: str) -> bool:
    expected_decimal = parse_decimal(expected_text)
    if expected_decimal is None:
        return False
    try:
        # `Decimal(str(target))`, never `Decimal(target)`: the float constructor would
        # compare against 0.1's full binary expansion rather than the 0.1 the record
        # says and every reader sees.
        target_decimal = Decimal(str(target))
    except InvalidOperation:
        return False
    if not target_decimal.is_finite():
        # `json.loads` accepts JavaScript's `NaN`/`Infinity` extensions, so a non-finite
        # target can reach here from a real file. No expected value equals it.
        return False
    return target_decimal == expected_decimal


def expected_matches(target: Any, expected: Any) -> bool:
    """Whether a resolved target equals an anchor's ``expected``, after canonicalization.

    Equality, never containment. The rule is chosen by the *target*'s type, because the
    target is the evidence and ``expected`` is the assertion about it:

    - string → both sides through the codebase's one text normalization (NFKC, quote and
      dash folding, whitespace collapse, case folding), so a curly apostrophe in a
      record does not defeat a plain one in a claim;
    - number → both sides as ``Decimal``, so ``23.99``, ``"23.99"`` and ``"23.990"``
      agree while ``"23.99 EUR"`` correctly does not;
    - boolean / null → ``expected`` must canonicalize to ``true``, ``false`` or ``null``.

    A non-scalar target never matches; callers report it as its own result, since "the
    pointer cites a subtree" and "the value differs" are different mistakes.
    """
    target_canonical = canonical_scalar(target)
    if target_canonical is None:
        return False
    expected_text = canonical_scalar(expected)
    if expected_text is None:
        return False
    if isinstance(target, bool) or target is None:
        return expected_text.strip().casefold() == target_canonical
    if isinstance(target, (int, float)):
        return _decimal_matches(target, expected_text)
    return _normalize_text(target) == _normalize_text(expected_text)


def resolve_anchor(
    frontmatter: dict[str, Any],
    sidecar: Path,
    pointer: str,
    expected: Any,
    *,
    loaded: SidecarLoad | None = None,
) -> AnchorResolution:
    """Decide one anchor end to end: load the structured view, resolve, compare.

    Returns the first failure, so a report names the earliest thing that was wrong
    rather than a symptom of it, or ``ok=True`` when the pointer reached a scalar equal
    to ``expected``.

    ``loaded`` lets a caller supply a sidecar this function would otherwise read itself.
    Reading and hashing are per *source*, not per claim, so a verifier checking several
    anchors against one record can bind the file once and pass the same result in — the
    binding is enforced exactly as strictly either way, just not re-enforced per entry.
    The parameter is named ``sidecar`` rather than ``sidecar_path`` because this module
    also exports a ``sidecar_path()`` function, which the old name shadowed.
    """
    normalized_pointer = normalize_pointer(pointer)
    if loaded is None:
        loaded = load_sidecar(frontmatter, sidecar)
    if not loaded.ok:
        return AnchorResolution(False, loaded.result, loaded.detail, normalized_pointer, None)
    resolution = resolve_pointer(loaded.document, pointer)
    if not resolution.ok:
        return AnchorResolution(False, resolution.result, resolution.detail, resolution.pointer, None)
    target = resolution.value
    rendered = canonical_scalar(target)
    if rendered is None:
        return AnchorResolution(
            False,
            RESULT_ANCHOR_TARGET_NOT_SCALAR,
            f"Pointer {resolution.pointer} resolves to a JSON {_json_type_name(target)}; "
            "an anchor must cite one scalar field, not a subtree.",
            resolution.pointer,
            None,
        )
    if not expected_matches(target, expected):
        expected_text = canonical_scalar(expected)
        if expected_text is None:
            detail = (
                f"Pointer {resolution.pointer} resolves to {rendered!r}, but the anchor's expected value "
                f"is a JSON {_json_type_name(expected)}, which no scalar field can equal."
            )
        else:
            detail = (
                f"Pointer {resolution.pointer} resolves to {rendered!r}, "
                f"which is not equal to the expected {expected_text!r}."
            )
        return AnchorResolution(
            False,
            RESULT_ANCHOR_VALUE_MISMATCH,
            detail,
            resolution.pointer,
            rendered,
        )
    return AnchorResolution(
        True,
        None,
        f"Pointer {resolution.pointer} resolves to {rendered!r} in the record's structured view, "
        "equal to the anchor's expected value.",
        resolution.pointer,
        rendered,
    )
