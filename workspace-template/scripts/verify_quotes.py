#!/usr/bin/env python3
"""Verify answer grounding against normalized source records.

The verifier is offline and deterministic. It reads question-page ``grounding``
frontmatter entries, resolves each ``source_id`` to its normalized record, and checks
each entry against retained evidence. It performs no network I/O.

A grounding entry carries exactly one of two forms, and each has its own check:

- **quote** — the quoted text must map to one retained occurrence at a declared title,
  page, or section anchor. Normalization is limited to deterministic Unicode,
  whitespace, punctuation, and line-break hyphenation artifacts; semantic substitution
  is never accepted.
- **anchor** — an RFC 6901 pointer into the record's structured-view sidecar must
  resolve to one scalar field whose canonical form equals the entry's ``expected``.

The two are not variations on one check. Containment proves a record contains a
sentence, which for structured evidence any line of the cited section satisfies whatever
value the claim asserts; equality against a named field proves the claim's own value is
what the evidence states. Anchors exist to close that gap, so the anchor path never falls
back to containment, and a record with no structured view refuses per-entry rather than
degrading to a weaker check that would report the same word, ``verified``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to verify grounding quotes") from exc


SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_NOT_VERIFIED = 1
EXIT_INVALID = 2
RESULT_VERIFIED = "verified"
RESULT_QUOTE_NOT_FOUND = "quote_not_found"
RESULT_SOURCE_NOT_NORMALIZED = "source_not_normalized"
RESULT_QUOTE_AMBIGUOUS = "quote_ambiguous"
RESULT_ANCHOR_NOT_FOUND = "anchor_not_found"
RESULT_QUOTE_NOT_AT_ANCHOR = "quote_not_at_anchor"

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _script_errors import ScriptRefusal, emit_refusal, is_refusal, json_mode_requested
from _workspace_module_loader import load_workspace_module

_SIBLING_CACHE: dict[str, ModuleType] = {}


def load_sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


# `_structured_view` owns anchor resolution, and loads this module lazily from inside one
# function precisely so the binding can be made here at import time. Its per-entry result
# strings are aliased rather than restated: they are machine-readable API that hosts switch
# on, and a second copy of an API string is a second place for it to drift.
_structured_view = load_sibling_module("_structured_view")

RESULT_STRUCTURED_VIEW_MISSING = _structured_view.RESULT_STRUCTURED_VIEW_MISSING
RESULT_STRUCTURED_VIEW_CORRUPT = _structured_view.RESULT_STRUCTURED_VIEW_CORRUPT
RESULT_ANCHOR_POINTER_NOT_FOUND = _structured_view.RESULT_ANCHOR_POINTER_NOT_FOUND
RESULT_ANCHOR_TARGET_NOT_SCALAR = _structured_view.RESULT_ANCHOR_TARGET_NOT_SCALAR
RESULT_ANCHOR_VALUE_MISMATCH = _structured_view.RESULT_ANCHOR_VALUE_MISMATCH

# A grounding entry carries exactly one form of evidence, and says which.
GROUNDING_FORM_QUOTE = "quote"
GROUNDING_FORM_ANCHOR = "anchor"
GROUNDING_FORMS = (GROUNDING_FORM_QUOTE, GROUNDING_FORM_ANCHOR)
ANCHOR_ENTRY_FIELDS = ("pointer", "expected")
ANCHOR_POLICY = "structured_anchor_evidence"

# Fatal codes for a refused write. `GROUNDING_QUOTE_INVALID` predates anchors and is what
# the filer's host switches on, so it stays the code for an all-quote failure set exactly
# as before; anchors get their own rather than being folded into a name that lies.
GROUNDING_QUOTE_INVALID = "GROUNDING_QUOTE_INVALID"
GROUNDING_ANCHOR_INVALID = "GROUNDING_ANCHOR_INVALID"

# One remediation per anchor failure, naming the edit that would fix that entry. Anchors
# fail for reasons a quote cannot, so none of the quote-path advice transfers.
ANCHOR_REMEDIATION = {
    RESULT_STRUCTURED_VIEW_MISSING: (
        "Re-normalize the cited source with a normalizer that emits a structured view, "
        "or ground this claim with a quote against the record body instead."
    ),
    RESULT_STRUCTURED_VIEW_CORRUPT: (
        "Re-normalize the cited source so its sidecar bytes and the record's "
        "structured_view.content_hash agree, then rerun grounding verification."
    ),
    RESULT_ANCHOR_POINTER_NOT_FOUND: (
        "Correct the pointer to name a field the record's structured view actually carries; "
        "do not point at a field the evidence would have to grow."
    ),
    RESULT_ANCHOR_TARGET_NOT_SCALAR: (
        "Extend the pointer to the single field being claimed; an anchor cites one scalar "
        "value, never a subtree that merely contains it."
    ),
    RESULT_ANCHOR_VALUE_MISMATCH: (
        "Correct the expected value to what the cited field states, or anchor the claim to "
        "the field that carries it; never restate a value the evidence does not."
    ),
}
# `_structured_view`'s result strings are documented as added to over time, so this table
# is read with a default rather than indexed: a result it has not learned yet must degrade
# to generic advice on that one entry, never raise a KeyError that escapes this module's
# error handling and takes every caller — resolution, export, the controller's
# recomputation — down with it.
ANCHOR_REMEDIATION_FALLBACK = (
    "Re-check this anchor against the cited record's structured view, then rerun grounding "
    "verification; see the entry's message for what the verifier found."
)


class VerifyQuotesError(ScriptRefusal):
    """Fatal grounding verifier error with a stable machine code.

    This is the refusal type ``question_resolve`` and ``export_answers`` already
    catch by name, so it keeps its name and its ``(error_code, message, *,
    details)`` constructor. Since CR-6 it is also a :class:`ScriptRefusal`, which
    is what lets ``run_verify`` raise it straight at an embedding host and lets
    ``main`` render it with the one shared catch arm.

    Everything the base type adds is defaulted to what ``main`` used to supply from
    the outside, so no envelope byte and no exit code moves:

    - ``exit_code`` defaults to ``EXIT_INVALID``, which ``main`` hardcoded for every
      ``VerifyQuotesError`` it caught.
    - ``recoverable`` and ``remediation`` default to ``None``, which is how the
      envelope was already built: the shared table answers both from the code.
    - ``text_line`` is fixed to the bare message, and is the one base-class option
      this type does not take. Coded refusals here reached ``emit_error``, which
      prints the message alone under ``--format text``; the base type's
      ``refused (CODE): message`` default would be a new line of output for a
      caller that reads text.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = EXIT_INVALID,
        recoverable: bool | None = None,
        remediation: str | None = None,
    ) -> None:
        super().__init__(
            error_code,
            message,
            exit_code=exit_code,
            recoverable=recoverable,
            remediation=remediation,
            details=details or {},
            text_line=message,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify grounded answer quotes against normalized source records.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument("--slug", action="append", required=True, help="Question slug to verify. Repeatable.")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Report format. Defaults to json.")
    parser.add_argument("--output", default=None, help="Write the report to this path instead of stdout.")
    parser.add_argument("--write", action="store_true", help="Record verifier metadata on fully verified questions.")
    parser.add_argument("--verified-by", default=None, help="Verifier agent id required with --write.")
    return parser.parse_args(argv)


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        raise SystemExit(f"Missing config: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise SystemExit(f"Invalid config: {path}")
    return document


def split_page(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return {}, text
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise VerifyQuotesError("PAGE_INVALID", f"Invalid frontmatter YAML: {exc}") from exc
    body = "\n".join(lines[closing + 1 :])
    return (frontmatter if isinstance(frontmatter, dict) else {}), body


def validate_slug(slug: str) -> str:
    clean_slug = slug.strip()
    if not clean_slug or "/" in clean_slug or "\\" in clean_slug or clean_slug.startswith("."):
        raise VerifyQuotesError("SLUG_INVALID", f"invalid question slug: {slug}", details={"slug": slug})
    return clean_slug


def question_path(project_root: Path, config: dict[str, Any], slug: str) -> Path:
    question_status = load_sibling_module("question_status")
    clean_slug = validate_slug(slug)
    return question_status.questions_directory(project_root, config) / f"{clean_slug}.md"


def workspace_relative(project_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def normalized_root(project_root: Path, config: dict[str, Any]) -> tuple[Path, str]:
    """The workspace's normalized-records directory, and the label artifacts under it carry."""
    normalize = load_sibling_module("normalize_sources")
    _, normalized_rel = normalize.source_paths(config)
    return project_root / normalized_rel, normalized_rel


def normalized_record_path(project_root: Path, config: dict[str, Any], source_id: str) -> tuple[Path, str]:
    normalize = load_sibling_module("normalize_sources")
    root, normalized_rel = normalized_root(project_root, config)
    path = root / f"{normalize.safe_source_id(source_id)}.md"
    return path, f"{normalized_rel}/{path.name}"


def structured_view_path(project_root: Path, config: dict[str, Any], source_id: str) -> tuple[Path, str]:
    """Where the structured-view sidecar for ``source_id`` must live, and its workspace label.

    The naming rule belongs to `_structured_view`, which is also what reads the file, so a
    sidecar can never be looked for in one place and validated in another.
    """
    root, normalized_rel = normalized_root(project_root, config)
    path = _structured_view.sidecar_path(root, source_id)
    return path, f"{normalized_rel}/{path.name}"


_QUOTE_NORMALIZATION_TRANSLATION = str.maketrans(
    {
        "‘": "'",  # left single quotation mark
        "’": "'",  # right single quotation mark / apostrophe
        "‚": "'",  # single low-9 quotation mark
        "‛": "'",  # single high-reversed-9 quotation mark
        "“": '"',  # left double quotation mark
        "”": '"',  # right double quotation mark
        "„": '"',  # double low-9 quotation mark
        "‟": '"',  # double high-reversed-9 quotation mark
        "­": "",  # soft hyphen
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
    }
)
_LINE_BREAK_HYPHEN_RE = re.compile(r"-[ \t]*\n[ \t]*")
_PAGE_ANCHOR_RE = re.compile(
    r"(?im)^[ \t]*(?:<!--[ \t]*)?(?:page|p\.)[ \t]*[:#-]?[ \t]*(\d+)(?:[ \t]*-->)?[ \t]*$"
)
_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$")


def normalize_quote_text(value: str, *, dehyphenate_line_breaks: bool = False) -> str:
    """Normalize whitespace, case, and common PDF/Unicode extraction artifacts.

    NFKC folds compatibility variants (ligatures, full-width forms); curly
    quotes/apostrophes, dashes, and the soft hyphen are mapped to plain ASCII.
    Callers evaluate both retained-hyphen and dehyphenated line-break variants so
    legitimate compound words and extraction-wrapped words remain distinguishable.
    """
    text = unicodedata.normalize("NFKC", value)
    text = text.translate(_QUOTE_NORMALIZATION_TRANSLATION)
    text = _LINE_BREAK_HYPHEN_RE.sub("" if dehyphenate_line_breaks else "-", text)
    return " ".join(text.split()).casefold()


def quote_match(text: str, quote: str) -> dict[str, Any]:
    exact_count = text.count(quote)
    if exact_count:
        return {"match_type": "exact", "occurrence_count": exact_count}
    for match_type, dehyphenate in (
        ("normalized", False),
        ("normalized_dehyphenated", True),
    ):
        normalized_text = normalize_quote_text(text, dehyphenate_line_breaks=dehyphenate)
        normalized_quote = normalize_quote_text(quote, dehyphenate_line_breaks=dehyphenate)
        count = normalized_text.count(normalized_quote) if normalized_quote else 0
        if count:
            return {"match_type": match_type, "occurrence_count": count}
    return {"match_type": None, "occurrence_count": 0}


def page_anchor(body: str, location_hint: str) -> tuple[str | None, dict[str, Any]] | None:
    requested = re.fullmatch(r"(?:page|p\.)[ \t]*[:#-]?[ \t]*(\d+)", location_hint.strip(), re.IGNORECASE)
    if requested is None:
        return None
    page_number = requested.group(1)
    markers = list(_PAGE_ANCHOR_RE.finditer(body))
    matching = [(index, marker) for index, marker in enumerate(markers) if marker.group(1) == page_number]
    if len(matching) != 1:
        status = "not_found" if not matching else "ambiguous"
        return None, {"type": "page", "label": f"page {page_number}", "status": status}
    index, marker = matching[0]
    end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
    return body[marker.end() : end], {"type": "page", "label": f"page {page_number}", "status": "matched"}


def section_anchor(body: str, location_hint: str) -> tuple[str | None, dict[str, Any]]:
    hint = normalize_quote_text(location_hint)
    headings = list(_HEADING_RE.finditer(body))
    matching = [
        (index, heading)
        for index, heading in enumerate(headings)
        if hint
        and (
            normalize_quote_text(heading.group(2)) == hint
            or hint in normalize_quote_text(heading.group(2))
            or normalize_quote_text(heading.group(2)) in hint
        )
    ]
    if len(matching) != 1:
        status = "not_found" if not matching else "ambiguous"
        return None, {"type": "section", "label": location_hint, "status": status}
    index, heading = matching[0]
    level = len(heading.group(1))
    end = len(body)
    for next_heading in headings[index + 1 :]:
        if len(next_heading.group(1)) <= level:
            end = next_heading.start()
            break
    return body[heading.start() : end], {
        "type": "section",
        "label": heading.group(2).strip(),
        "status": "matched",
    }


def resolve_anchor(
    frontmatter: dict[str, Any],
    body: str,
    location_hint: str | None,
) -> tuple[str | None, dict[str, Any]]:
    if not location_hint:
        return body, {"type": None, "label": None, "status": "not_requested"}
    normalized_hint = normalize_quote_text(location_hint)
    if normalized_hint in {"title", "normalized title", "document title"}:
        title = frontmatter.get("title")
        if isinstance(title, str) and title.strip():
            return title, {"type": "title", "label": "normalized title", "status": "matched"}
        return None, {"type": "title", "label": "normalized title", "status": "not_found"}
    page = page_anchor(body, location_hint)
    if page is not None:
        return page
    return section_anchor(body, location_hint)


def _grounding_invalid(slug: str, index: int, message: str, **details: Any) -> VerifyQuotesError:
    """A fatal entry-shape refusal naming the entry that caused it."""
    return VerifyQuotesError(
        "GROUNDING_INVALID",
        f"Question {slug} grounding[{index}] {message}",
        details={"slug": slug, "index": index, **details},
    )


def anchor_entry_fields(item: dict[str, Any], slug: str, index: int) -> dict[str, str]:
    """Validate one entry's ``anchor`` block and canonicalize it.

    ``expected`` is canonicalized to a string here, at load, because YAML types an
    unquoted ``expected: 23.99`` as a float and an unquoted ``expected: true`` as a bool.
    Downstream — comparison, reporting, and the frontmatter writer — then handles exactly
    one type, and the entry means the same thing however its author happened to quote it.

    ``pointer`` is stored as written. It is trimmed only for the emptiness check: RFC 6901
    reference tokens may legitimately begin or end with a space, so stripping the stored
    value could silently retarget the anchor at a neighbouring field.
    """
    anchor = item.get("anchor")
    if not isinstance(anchor, dict):
        raise _grounding_invalid(
            slug,
            index,
            f"anchor must be a mapping of pointer and expected, not a {type(anchor).__name__}.",
            field="anchor",
            actual=type(anchor).__name__,
        )
    if item.get("location_hint") is not None:
        # A location_hint anchors a quote inside the rendered body. Against a structured
        # view it locates nothing, so accepting it would record a claim about nothing.
        raise _grounding_invalid(
            slug,
            index,
            "cannot carry location_hint beside anchor; a location_hint locates a quote in the record body.",
            field="location_hint",
        )
    unknown = sorted(str(field) for field in anchor if field not in ANCHOR_ENTRY_FIELDS)
    if unknown:
        raise _grounding_invalid(
            slug,
            index,
            f"anchor has unsupported key(s): {', '.join(unknown)}.",
            field="anchor",
            unsupported_keys=unknown,
        )
    pointer = anchor.get("pointer")
    if not isinstance(pointer, str) or not pointer.strip():
        raise _grounding_invalid(
            slug,
            index,
            "anchor is missing a non-empty pointer.",
            field="anchor.pointer",
        )
    expected = anchor.get("expected")
    if expected is None or not isinstance(expected, (str, int, float, bool)):
        raise _grounding_invalid(
            slug,
            index,
            "anchor.expected must be a string, number, or boolean naming the value the cited field holds.",
            field="anchor.expected",
            actual=type(expected).__name__,
        )
    canonical = _structured_view.canonical_scalar(expected)
    if canonical is None:  # pragma: no cover - the scalar gate above already excludes these
        raise _grounding_invalid(
            slug,
            index,
            "anchor.expected must be a scalar value.",
            field="anchor.expected",
            actual=type(expected).__name__,
        )
    return {"pointer": pointer, "expected": canonical}


def quote_entry_fields(item: dict[str, Any], slug: str, index: int) -> dict[str, str]:
    """Validate one entry's quote form: the quoted text and its optional body locator."""
    fields: dict[str, str] = {}
    quote = item.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise _grounding_invalid(slug, index, "is missing non-empty quote.", field="quote")
    fields["quote"] = quote.strip()
    location_hint = item.get("location_hint")
    if location_hint is not None:
        if not isinstance(location_hint, str):
            raise _grounding_invalid(
                slug,
                index,
                "location_hint must be a string when present.",
                field="location_hint",
            )
        if location_hint.strip():
            fields["location_hint"] = location_hint.strip()
    return fields


def grounding_entries(frontmatter: dict[str, Any], slug: str) -> list[dict[str, Any]]:
    """Parse a question's ``grounding`` block into validated, form-tagged entries.

    Every entry names a claim, the source it cites, and exactly one form of evidence: a
    ``quote``, checked by containment against the normalized record's body, or an
    ``anchor``, checked by canonical equality against the record's structured view. The
    exclusivity is the point. An entry carrying both would leave "what did this prove?"
    to whichever check happened to run; an entry carrying neither would assert a claim
    with nothing behind it. Both are refused here, before any evidence file is opened.

    Shape violations raise the stable fatal ``GROUNDING_INVALID`` naming the entry index.
    Anything that can only be learned by reading evidence is a per-entry result instead —
    a bad anchor is a finding about one entry, not a malformed question page.
    """
    raw = frontmatter.get("grounding")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise VerifyQuotesError(
            "GROUNDING_INVALID",
            f"Question {slug} has invalid grounding: expected a list of claim/source/quote or claim/source/anchor entries.",
            details={"slug": slug, "field": "grounding", "actual": type(raw).__name__},
        )
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _grounding_invalid(slug, index, "must be a mapping.", actual=type(item).__name__)
        entry: dict[str, Any] = {}
        for field in ("claim", "source_id"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _grounding_invalid(slug, index, f"is missing non-empty {field}.", field=field)
            entry[field] = value.strip()
        # A key present with a null value counts as absent, matching the canonical
        # renderer: `quote:` with nothing after it is an unfinished edit, not a form.
        has_quote = item.get("quote") is not None
        has_anchor = item.get("anchor") is not None
        if has_quote == has_anchor:
            carried = "both quote and anchor" if has_quote else "neither quote nor anchor"
            raise _grounding_invalid(
                slug,
                index,
                f"carries {carried}; an entry must carry exactly one form.",
                field="grounding",
                forms=list(GROUNDING_FORMS),
            )
        if has_anchor:
            entry["form"] = GROUNDING_FORM_ANCHOR
            entry["anchor"] = anchor_entry_fields(item, slug, index)
        else:
            entry["form"] = GROUNDING_FORM_QUOTE
            entry.update(quote_entry_fields(item, slug, index))
        entries.append(entry)
    return entries


def count_by_form(results: Iterable[Any]) -> dict[str, int]:
    """How many grounding entries of each form, so a workspace can measure its own migration."""
    counts = dict.fromkeys(GROUNDING_FORMS, 0)
    for result in results:
        if isinstance(result, dict) and result.get("form") in counts:
            counts[result["form"]] += 1
    return counts


def report_results(questions: Iterable[Any]) -> list[dict[str, Any]]:
    """Every per-entry grounding result across a set of question reports."""
    return [
        result
        for question in questions
        if isinstance(question, dict)
        for result in question.get("grounding", [])
        if isinstance(result, dict)
    ]


def failed_results(results: Iterable[Any]) -> list[dict[str, Any]]:
    """The per-entry results that did not verify."""
    return [
        result
        for result in results
        if isinstance(result, dict) and result.get("result") != RESULT_VERIFIED
    ]


def grounding_failure_error_code(results: Iterable[Any]) -> str:
    """The fatal code that tops an envelope refusing a write over failed grounding.

    ``GROUNDING_QUOTE_INVALID`` when every failure is quote-form — bit-for-bit the code
    this refusal has always carried, which hosts switch on — and
    ``GROUNDING_ANCHOR_INVALID`` as soon as one anchor entry failed, because a caller
    told "a quote did not verify" about an anchor failure would look for a quote there is
    none of. Either way the envelope carries the full failure list, so a mixed set is
    fully enumerated whichever code names it.

    Accepts either the already-failed entries or a whole grounding result list; verified
    entries never choose the code.
    """
    for result in failed_results(results):
        if result.get("form") == GROUNDING_FORM_ANCHOR:
            return GROUNDING_ANCHOR_INVALID
    return GROUNDING_QUOTE_INVALID


def normalized_record_content(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_page(text)
    return frontmatter, body if frontmatter else text


class EvidenceCache:
    """Memo of the evidence files one verification run reads, keyed by path.

    Reading a record and reading — and hashing — its structured view are facts about a
    *source*, not about a claim. Without this, a question grounding ten claims in one
    record re-read and re-SHA256'd that record's sidecar ten times, and the controller
    repeated the whole thing for every answered question in the workspace on every run.
    Sidecars are the deliberately uncapped artifact, so that cost is unbounded in exactly
    the workspaces anchors exist for.

    Nothing is weakened by memoizing: the hash binding is still enforced, once per source
    per run instead of once per claim, and the run is short-lived — a later run re-reads
    everything. Load failures are cached with successes on purpose, so every entry citing
    one broken sidecar gets the same verdict rather than a verdict that depends on how
    many entries preceded it.
    """

    def __init__(self) -> None:
        self._records: dict[Path, tuple[dict[str, Any], str]] = {}
        self._sidecars: dict[Path, Any] = {}

    def record(self, path: Path) -> tuple[dict[str, Any], str]:
        if path not in self._records:
            self._records[path] = normalized_record_content(path)
        return self._records[path]

    def sidecar(self, frontmatter: dict[str, Any], path: Path) -> Any:
        if path not in self._sidecars:
            self._sidecars[path] = _structured_view.load_sidecar(frontmatter, path)
        return self._sidecars[path]


def verify_anchor_entry(
    project_root: Path,
    config: dict[str, Any],
    entry: dict[str, Any],
    cache: EvidenceCache | None = None,
) -> dict[str, Any]:
    """Verify one anchor entry: the cited field holds exactly the value the claim states.

    The record must exist first — an anchor against an unnormalized source is the same
    finding as a quote against one, and reuses the same result — and then the whole
    anchor verdict is `_structured_view`'s: load the sidecar under the record's hash
    binding, resolve the pointer, compare by canonical equality. Containment is never
    consulted, which is exactly what distinguishes this path from the quote path.
    """
    source_id = entry["source_id"]
    anchor = entry["anchor"]
    record_path, record_label = normalized_record_path(project_root, config, source_id)
    sidecar, sidecar_label = structured_view_path(project_root, config, source_id)
    # Every anchor result carries the same keys, whichever way it ends: what was asked
    # for, where it was asked, and what was found — `resolved` staying null when nothing
    # was reached. A consumer never has to ask whether a key is there before reading it.
    result: dict[str, Any] = {
        "claim": entry["claim"],
        "source_id": source_id,
        "form": GROUNDING_FORM_ANCHOR,
        "pointer": _structured_view.normalize_pointer(anchor["pointer"]),
        "expected": anchor["expected"],
        "resolved": None,
        "normalized_record": record_label,
        "structured_view": sidecar_label,
        "artifacts": [record_label, sidecar_label],
        "policy": ANCHOR_POLICY,
    }
    if not record_path.is_file():
        result["result"] = RESULT_SOURCE_NOT_NORMALIZED
        result["message"] = f"{source_id} has no normalized record at {record_label}."
        result["remediation"] = "Normalize the cited source, then rerun grounding verification."
        return result
    cache = cache or EvidenceCache()
    frontmatter, _ = cache.record(record_path)
    # `_structured_view.resolve_anchor` is the sidecar/pointer/equality verdict, unrelated
    # to this module's `resolve_anchor`, which locates a quote's page or section anchor.
    # The sidecar is bound once per source and handed in; the verdict is unchanged by that.
    resolution = _structured_view.resolve_anchor(
        frontmatter,
        sidecar,
        anchor["pointer"],
        anchor["expected"],
        loaded=cache.sidecar(frontmatter, sidecar),
    )
    result["pointer"] = resolution.pointer  # the pointer the resolver actually walked
    result["resolved"] = resolution.resolved
    result["message"] = resolution.detail
    if resolution.ok:
        result["result"] = RESULT_VERIFIED
        result["remediation"] = "No remediation required."
        return result
    result["result"] = resolution.result
    result["remediation"] = ANCHOR_REMEDIATION.get(resolution.result, ANCHOR_REMEDIATION_FALLBACK)
    return result


def verify_entry(
    project_root: Path,
    config: dict[str, Any],
    entry: dict[str, Any],
    cache: EvidenceCache | None = None,
) -> dict[str, Any]:
    cache = cache or EvidenceCache()
    if entry.get("form") == GROUNDING_FORM_ANCHOR:
        return verify_anchor_entry(project_root, config, entry, cache)
    source_id = entry["source_id"]
    record_path, record_label = normalized_record_path(project_root, config, source_id)
    result: dict[str, Any] = {
        "claim": entry["claim"],
        "source_id": source_id,
        "form": GROUNDING_FORM_QUOTE,
        "quote": entry["quote"],
        "location_hint": entry.get("location_hint"),
        "normalized_record": record_label,
        "artifacts": [record_label],
        "policy": "retained_quote_evidence",
    }
    if not record_path.is_file():
        result["result"] = RESULT_SOURCE_NOT_NORMALIZED
        result["message"] = f"{source_id} has no normalized record at {record_label}."
        result["remediation"] = "Normalize the cited source, then rerun quote verification."
        return result
    frontmatter, body = cache.record(record_path)
    anchor_text, anchor = resolve_anchor(frontmatter, body, entry.get("location_hint"))
    result["anchor"] = anchor
    global_match = quote_match(body, entry["quote"])
    result["global_occurrence_count"] = global_match["occurrence_count"]
    if anchor_text is None:
        result["result"] = RESULT_ANCHOR_NOT_FOUND
        result["match_type"] = global_match["match_type"]
        result["occurrence_count"] = 0
        result["message"] = "The requested page/section anchor was not uniquely resolved in the normalized record."
        result["remediation"] = "Correct the location_hint to a retained page marker, section heading, or normalized title."
        return result
    scoped_match = quote_match(anchor_text, entry["quote"])
    result.update(scoped_match)
    if scoped_match["occurrence_count"] == 1:
        result["result"] = RESULT_VERIFIED
        result["message"] = "Quote maps to one retained occurrence at the requested anchor."
        result["remediation"] = "No remediation required."
    elif scoped_match["occurrence_count"] > 1:
        result["result"] = RESULT_QUOTE_AMBIGUOUS
        result["message"] = "Quote occurs more than once within the selected evidence scope."
        result["remediation"] = "Add a more specific page/section anchor or lengthen the quote without changing its meaning."
    elif global_match["occurrence_count"]:
        result["result"] = RESULT_QUOTE_NOT_AT_ANCHOR
        result["message"] = "Quote exists in the normalized record but not at the requested anchor."
        result["remediation"] = "Correct the location_hint or quote so both identify the same retained evidence span."
    else:
        result["result"] = RESULT_QUOTE_NOT_FOUND
        result["message"] = "Quote was not found in the normalized record after whitespace/case normalization."
        result["remediation"] = "Use a verbatim retained quote or correct the cited source; do not paraphrase inside quote fields."
    return result


def verify_question(
    project_root: Path,
    config: dict[str, Any],
    slug: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    path: Path | None = None,
    cache: EvidenceCache | None = None,
) -> dict[str, Any]:
    question = path or question_path(project_root, config, slug)
    if not question.is_file():
        raise VerifyQuotesError(
            "QUESTION_UNKNOWN",
            f"Unknown question slug: {slug}",
            details={"slug": slug},
        )
    if frontmatter is None:
        frontmatter, _ = split_page(question.read_text(encoding="utf-8"))
    entries = grounding_entries(frontmatter, slug)
    # One cache for the whole question, or the caller's when it spans several.
    cache = cache or EvidenceCache()
    results = [verify_entry(project_root, config, entry, cache) for entry in entries]
    all_verified = bool(results) and all(result.get("result") == RESULT_VERIFIED for result in results)
    return {
        "slug": slug,
        "question_page": workspace_relative(project_root, question),
        "grounding_count": len(results),
        "by_form": count_by_form(results),
        "all_verified": all_verified,
        "grounding": results,
    }


def build_grounding_report(project_root: Path, slugs: Sequence[str] | None) -> dict[str, Any]:
    """Verify every named question and return the document this command reports.

    Takes the slugs themselves rather than a parsed namespace, because both callers
    that matter — ``main`` through :func:`build_report`, and the ``run_verify`` seam —
    have slugs and only one of them has ever had an ``argparse.Namespace``.
    """
    config = load_config(project_root)
    seen: set[str] = set()
    resolved: list[str] = []
    for value in slugs or []:
        slug = value.strip()
        if slug and slug not in seen:
            seen.add(slug)
            resolved.append(slug)
    if not resolved:
        raise VerifyQuotesError("SLUG_INVALID", "At least one --slug value is required.")
    # Shared across every question in the report: several questions commonly cite the
    # same record, and the controller verifies the whole workspace in one call.
    cache = EvidenceCache()
    questions = [verify_question(project_root, config, slug, cache=cache) for slug in resolved]
    total_entries = sum(int(question.get("grounding_count", 0) or 0) for question in questions)
    all_results = report_results(questions)
    failed_entries = failed_results(all_results)
    missing_grounding = [question["slug"] for question in questions if not question.get("grounding")]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_utc(),
        "network_io_executed": False,
        "questions": questions,
        "counts": {
            "questions": len(questions),
            "grounding_entries": total_entries,
            "verified": total_entries - len(failed_entries),
            "failed": len(failed_entries),
            "missing_grounding": len(missing_grounding),
            # Additive, and last: consumers that mirror this shape read the keys above by
            # name, and a migration measurement is not worth moving one of them.
            "by_form": count_by_form(all_results),
        },
        "overall_result": RESULT_VERIFIED if questions and not failed_entries and not missing_grounding else "not_verified",
    }


def build_report(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Namespace-shaped entry point kept for callers that already hold parsed args.

    ``orchestration_controller`` recomputes quote verification through this exact
    signature when it re-derives a run's verification bundle, so it stays.
    """
    return build_grounding_report(project_root, args.slug)


def is_top_level_field(line: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_-]+:\s*", line))


def remove_frontmatter_field_block(lines: list[str], key: str) -> list[str]:
    prefix = f"{key}:"
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(prefix):
            index += 1
            while index < len(lines) and not is_top_level_field(lines[index]):
                index += 1
            continue
        output.append(line)
        index += 1
    return output


def set_frontmatter_scalar(lines: list[str], key: str, value: str) -> list[str]:
    lines = remove_frontmatter_field_block(lines, key)
    return [*lines, f"{key}: {json.dumps(value)}"]


def stamp_question_verification(project_root: Path, config: dict[str, Any], slug: str, verified_by: str) -> dict[str, Any]:
    question_claim = load_sibling_module("question_claim")
    question = question_path(project_root, config, slug)
    with question_claim.question_lock(question):
        text = question.read_text(encoding="utf-8")
        parts = question_claim.split_frontmatter_lines(text)
        if parts is None:
            raise VerifyQuotesError("PAGE_INVALID", f"Question {slug} has no frontmatter block.", details={"slug": slug})
        frontmatter = question_claim.frontmatter_mapping(parts[0])
        report = verify_question(project_root, config, slug, frontmatter=frontmatter, path=question)
        if not report["all_verified"]:
            failures = failed_results(report.get("grounding", []))
            raise VerifyQuotesError(
                grounding_failure_error_code(failures),
                f"Question {slug} has grounding entries that did not verify; refusing to stamp verifier metadata.",
                details={"slug": slug, "failures": failures},
            )
        frontmatter_lines, opening, rest = parts
        frontmatter_lines = set_frontmatter_scalar(frontmatter_lines, "verified_by", verified_by)
        frontmatter_lines = set_frontmatter_scalar(frontmatter_lines, "grounding_verified_at", timestamp_utc())
        question_claim.write_page_atomic(question, "\n".join([*opening, *frontmatter_lines, *rest]))
        return report


def write_verification_metadata(project_root: Path, verified_by: str | None, report: dict[str, Any]) -> None:
    """Stamp verifier metadata onto every question in a fully verified report.

    Takes the verifier id rather than the parsed namespace: the ``--write`` audit
    trail belongs to the operation, so the seam performs it too, and the seam has
    no ``argparse.Namespace``.
    """
    verified_by = verified_by.strip() if isinstance(verified_by, str) else ""
    if not verified_by:
        raise VerifyQuotesError(
            "GROUNDING_VERIFIER_REQUIRED",
            "--verified-by is required when --write is set.",
            details={"field": "verified_by"},
        )
    if report.get("overall_result") != RESULT_VERIFIED:
        # A question with no grounding at all also lands here, with nothing failed to
        # inspect; the all-quote code is what that has always reported and still is.
        failures = failed_results(report_results(report.get("questions", [])))
        raise VerifyQuotesError(
            grounding_failure_error_code(failures),
            "All grounding entries must verify before verifier metadata is written.",
            details={"failures": failures},
        )
    config = load_config(project_root)
    for question in report.get("questions", []):
        if isinstance(question, dict) and isinstance(question.get("slug"), str):
            stamp_question_verification(project_root, config, question["slug"], verified_by)


def render_text(report: dict[str, Any]) -> str:
    lines = ["Grounding Verification", "======================", ""]
    for question in report.get("questions", []):
        lines.append(f"- {question.get('slug')}: {'verified' if question.get('all_verified') else 'not_verified'}")
        for result in question.get("grounding", []):
            form = result.get("form") or GROUNDING_FORM_QUOTE
            lines.append(f"  - {result.get('source_id')} [{form}]: {result.get('result')} - {result.get('claim')}")
    return "\n".join(lines).rstrip() + "\n"


def render_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, indent=2, sort_keys=False) + "\n"
    return render_text(report)


def resolved_project_root(value: str | Path) -> Path:
    """Resolve a caller-supplied project root the way this command always has."""
    return Path(value).expanduser().resolve()


def run_verify(
    project_root: str | Path,
    slugs: Sequence[str],
    *,
    write: bool = False,
    verified_by: str | None = None,
) -> dict[str, Any]:
    """Return exactly the grounding report ``main`` prints under ``--format json``.

    This is the library seam: an embedding host calls it in-process instead of
    shelling out, and gets the document the CLI would have printed. ``write`` and
    ``verified_by`` mirror ``--write`` and ``--verified-by``, and the stamping they
    request happens here — a host that asks to verify-and-stamp must leave the same
    audit trail on the question pages as the CLI does, not a report about one.

    **A failed verification is not a refusal.** When the run completes and some
    claim does not verify, this returns the report saying so, exactly as the CLI
    prints it and then exits ``EXIT_NOT_VERIFIED``. Which claim failed, against
    which record, and what would fix it are the whole point of the document, and a
    host cannot read any of that off an exception. Only a genuine refusal —
    malformed grounding, an unknown slug, an unreadable workspace, or a ``--write``
    the verifier will not perform — raises ``ScriptRefusal``.

    ``--format`` and ``--output`` have no counterpart here: rendering a document
    and choosing where to put it belong to the CLI, not to producing it.
    """
    root = resolved_project_root(project_root)
    try:
        report = build_grounding_report(root, slugs)
        if write:
            write_verification_metadata(root, verified_by, report)
    except (Exception, SystemExit) as exc:
        if is_refusal(exc):
            # Already the shared refusal — VerifyQuotesError is one, and a sibling
            # seam may raise its own. Pass it on rather than re-wrapping, which
            # would keep the envelope and lose the `text_line` each one carries.
            #
            # Recognized by shape, not by `except ScriptRefusal`. Sibling isolation
            # gives each loaded script its own ScriptRefusal class, so naming the
            # class here would match this script's refusals and miss `question_claim`'s
            # entirely -- and a dual-inherited one would then fall to the SystemExit
            # branch below and be reclassified from its message text, discarding the
            # very error_code it arrived with.
            raise
        if isinstance(exc, SystemExit):
            # An unreadable workspace or a missing sibling script reaches here as
            # SystemExit(str); from_system_exit re-raises anything else untouched.
            raise ScriptRefusal.from_system_exit(exc, exit_code=EXIT_INVALID) from exc
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    try:
        report = run_verify(
            args.project_root,
            args.slug,
            write=args.write,
            verified_by=args.verified_by,
        )
    except ScriptRefusal as refusal:
        return emit_refusal(refusal, json_mode=json_mode)
    rendered = render_report(report, args.format)
    if args.output:
        Path(args.output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return EXIT_OK if report.get("overall_result") == RESULT_VERIFIED else EXIT_NOT_VERIFIED


if __name__ == "__main__":
    raise SystemExit(main())
