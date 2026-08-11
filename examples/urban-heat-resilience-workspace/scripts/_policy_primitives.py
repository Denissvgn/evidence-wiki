#!/usr/bin/env python3
"""Declarative evidence-policy primitives: what a pack may assert, and how it is decided.

A domain pack may name its own evidence policies, but until now it had no vocabulary in
which to *express* one, so every ``pack:<pack-name>/<policy-id>`` policy fell to
``manual_review`` — including checks that are entirely deterministic over data the
workspace already holds. "A supplier quote must be at most 48 hours old" is a
subtraction, not a judgement call. This module gives a pack a small closed set of
declarative primitives for exactly those checks, and decides them here:

.. code-block:: yaml

    domain_pack:
      name: market-data
      policy_vocabularies:
        freshness_policy:
          pack:market-data/quote-48h: A supplier quote must be at most 48 hours old.
        identity_policy:
          pack:market-data/sku-matches-candidate: The quoted SKU must match the candidate.
      policy_rules:
        pack:market-data/quote-48h:
          all_of:
            - max_age: {field: provenance/retrieved_at, hours: 48}
        pack:market-data/sku-matches-candidate:
          manual_review_required: false
          all_of:
            - equals: {field: record/supplier_quote/sku, question_field: metadata/candidate_sku}
            - one_of_provenance: {providers: [aliexpress-ds, partner-catalog]}

**Declarations only — a pack never ships code that runs.** A rule is data: a fixed set of
primitive names, each with a fixed set of arguments, parsed once and evaluated by this
package. There is no expression language, no callable, no import hook. A pack that could
execute would make "what does this workspace do?" unanswerable from the pack's own text,
which is the property the whole evidence chain rests on.

The declaration is validated in one place and read by two consumers that must never
disagree: ``evidence-wiki pack validate`` (before a pack ships) through
:func:`declaration_errors`, and the evidence-policy evaluator (at answer time) through
:func:`pack_policy_rules` and :func:`evaluate_rule`. A malformed declaration raises
rather than degrading to "this pack declares no rules": silently dropping a pack's
automation would send every one of its policies back to the review queue this module
exists to drain, and would do it without saying so.

Evaluation is **fail-closed and performs no filesystem I/O whatsoever**. Callers assemble
a :class:`RuleContext` from the workspace — the structured-view sidecar, the merged
provenance, the question's frontmatter, the clock — and this module only reads it. Every
resolution failure (no structured view, pointer not found, target is a subtree, timestamp
unparseable) evaluates to ``fail`` with a typed reason, never to ``manual_review``: a rule
that could degrade to review under adverse conditions would recreate the queue on exactly
the sources least likely to deserve the benefit of the doubt.
"""

from __future__ import annotations

import decimal
import math
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_module_loader import load_workspace_module

# `_structured_view` does not import this module, so binding it at import time closes no
# cycle and needs no lazy indirection. Everything this module knows about pointers,
# scalars and equality comes from there on purpose: a rule that decided equality its own
# way would be a second, quietly divergent definition of "equal" sitting beside the one
# grounding anchors already use.
_structured_view = load_workspace_module(_SCRIPT_DIR, "_structured_view")

# Same shape as PACK_POLICY_ID_RE in _evidence_policies.py:131. Copied rather than
# imported: that module reads manifests, coverage and jurisdiction files, and importing
# it would drag a filesystem-shaped dependency into a module whose entire contract is
# that it touches no files. Both segments already admit dots and dashes, so an id like
# pack:market-data/quote-48h matches unchanged.
PACK_POLICY_ID_RE = re.compile(r"^pack:[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$")

# The `policy_vocabularies` sections whose policies a rule may decide. `evidence_paths`
# is deliberately absent: an evidence path says *which facet* must be covered, which the
# coverage manifest resolves structurally before any policy runs, so a rule there would
# assert over something this module cannot see.
RULE_TARGET_SECTIONS = ("source_policy", "freshness_policy", "identity_policy")
# Named separately so a rule aimed at an evidence path fails with the section it was
# actually declared in, rather than the misleading "you never declared it".
EVIDENCE_PATHS_SECTION = "evidence_paths"

# The two documents a `field` reference may be rooted in, both caller-supplied. `record`
# is the parsed structured-view sidecar; `provenance` the merged provenance mapping. A
# `question_field` carries no root at all, because it addresses the question's whole
# frontmatter and there is only one of those.
FIELD_ROOTS = ("record", "provenance")

LEAF_PRIMITIVES = ("max_age", "equals", "numeric_range", "regex", "one_of_provenance")
COMPOSITION_PRIMITIVES = ("all_of", "any_of")
#: Primitive set v1. Closed on purpose: every name here is a check this package can
#: decide deterministically and explain in one sentence, and a pack cannot add to it.
PRIMITIVE_NAMES = LEAF_PRIMITIVES + COMPOSITION_PRIMITIVES

# A rule's top level is one composition plus an optional review flag. Anything else is a
# typo, and a typo the author wants named rather than ignored.
RULE_KEYS = ("all_of", "any_of", "manual_review_required")

# Compositions nest at most three deep, which is enough for `all_of(any_of(all_of(...)))`
# — the deepest shape a policy has needed. The cap keeps a declaration readable and its
# evaluation cost bounded by the declaration itself rather than by the data.
MAX_COMPOSITION_DEPTH = 3
# Keeps a declaration readable for the reviewer approving the pack. It is NOT what bounds
# backtracking — `(a+)+` is six characters — so catastrophic constructs are refused
# separately by `_nested_quantifier_span`.
MAX_REGEX_PATTERN_LENGTH = 512
# Deliberately NOT pack-configurable: a pack that could widen this would be loosening a
# fail-closed bound from inside the thing being bounded. Five minutes absorbs ordinary NTP
# drift between the host that stamped the timestamp and the machine evaluating it. Zero
# tolerance was rejected because that drift is real and would fail honest sources;
# clamping a future timestamp to age zero was rejected because it is fail-open — one fast
# clock would make arbitrarily stale evidence look fresh.
MAX_AGE_FUTURE_SKEW_MINUTES = 5

# Stable snake_case prefixes on every per-source failure reason, in the style of the
# `standard_reference_missing:` codes _evidence_policies.py already emits. Hosts switch on
# them, so they are never renamed, only added to.
REASON_FIELD_UNRESOLVED = "rule_field_unresolved"
REASON_VALUE_MISMATCH = "rule_value_mismatch"
REASON_OUT_OF_RANGE = "rule_out_of_range"
REASON_STALE = "rule_stale"
REASON_FUTURE_TIMESTAMP = "rule_future_timestamp"
REASON_REGEX_MISMATCH = "rule_regex_mismatch"
REASON_PROVENANCE_NOT_ALLOWED = "rule_provenance_not_allowed"

# Bound from `_structured_view`, not restated: when the structured view itself is what
# went wrong, a rule reports the same code an anchor would, so a host that already
# handles one handles the other.
RESULT_STRUCTURED_VIEW_MISSING = _structured_view.RESULT_STRUCTURED_VIEW_MISSING
RESULT_STRUCTURED_VIEW_CORRUPT = _structured_view.RESULT_STRUCTURED_VIEW_CORRUPT

#: Every prefix a *failure* reason can start with. A satisfied reason never carries one,
#: which is what lets a caller sort a mixed list without re-running anything.
RULE_REASON_PREFIXES = (
    REASON_FIELD_UNRESOLVED,
    REASON_VALUE_MISMATCH,
    REASON_OUT_OF_RANGE,
    REASON_STALE,
    REASON_FUTURE_TIMESTAMP,
    REASON_REGEX_MISMATCH,
    REASON_PROVENANCE_NOT_ALLOWED,
    RESULT_STRUCTURED_VIEW_MISSING,
    RESULT_STRUCTURED_VIEW_CORRUPT,
)

POLICY_RULE_REMEDIATION = (
    "Declare the rule under domain_pack.policy_rules as documented in docs/research-yml.md, "
    "or remove it so the policy falls back to recorded manual review."
)

# The document each field root names, for reasons a pack author has to act on.
_ROOT_DOCUMENT_LABELS: dict[str | None, str] = {
    "record": "structured view",
    "provenance": "merged provenance",
    None: "question frontmatter",
}
# `resolve_pointer` was written for sidecars and says "structured view" in its
# missing-key sentence. The same walker resolves provenance and question frontmatter
# here, so that one sentence is re-pointed at the document actually in hand.
_MISSING_KEY_PHRASE = "The structured view has no key"

_SECONDS_PER_HOUR = Decimal(3600)
_MINUTES_PER_HOUR = Decimal(60)

_JSON_KINDS = {dict: "object", list: "array"}


class PolicyRuleError(Exception):
    """Structured policy-rule failure carrying a stable machine-readable code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.remediation = remediation or POLICY_RULE_REMEDIATION
        self.details = details or {}


@dataclass(frozen=True)
class FieldRef:
    """One field reference: which document, and where in it.

    Field addressing reuses CR-7's scheme rather than inventing a second one. ``pointer``
    is an RFC 6901 JSON pointer in the form `_structured_view.normalize_pointer` returns,
    and ``root`` is the namespace segment the declaration wrote in front of it. ``root``
    is ``None`` for a ``question_field``, which resolves against the question's whole
    frontmatter mapping and therefore has no root segment to carry.
    """

    root: str | None
    pointer: str

    @property
    def display(self) -> str:
        """The reference as the pack author wrote it, for reasons and findings."""
        return f"{self.root or 'question'}{self.pointer}"

    @property
    def document_label(self) -> str:
        """Human name of the document this reference resolves against."""
        return _ROOT_DOCUMENT_LABELS[self.root]


@dataclass(frozen=True)
class Operand:
    """One side of a comparison: a literal written in the rule, or a question field.

    Exactly one of ``literal`` and ``ref`` is meaningful; ``ref is None`` selects the
    literal, because ``None`` is itself a comparable literal value and cannot be used as
    the "unset" signal on that side.
    """

    literal: Any = None
    ref: FieldRef | None = None

    @property
    def description(self) -> str:
        """How a reason names where this operand came from."""
        if self.ref is None:
            return "declared in the rule"
        return f"read from {self.ref.display}"


@dataclass(frozen=True)
class Primitive:
    """One parsed primitive, or one composition of them.

    Deliberately one dataclass rather than a class per primitive: the set is closed and
    small, evaluation dispatches on ``name`` once, and a single shape means a caller can
    walk a rule tree without knowing the whole taxonomy. Fields not used by ``name`` are
    left at their defaults, and validation guarantees the ones it does use are populated.
    """

    name: str
    field: FieldRef | None = None
    hours: Decimal | None = None
    operand: Operand | None = None
    minimum: Operand | None = None
    maximum: Operand | None = None
    pattern: str | None = None
    compiled: re.Pattern[str] | None = None
    providers: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    children: tuple[Primitive, ...] = ()


@dataclass(frozen=True)
class Rule:
    """One policy id's deterministic rule: a single composition, plus a review flag.

    ``manual_review_required`` is carried, never acted on here. Whether a policy that
    *passes* its rule still needs a human is the evaluator's verdict to render;
    :func:`evaluate_rule` answers only "does the evidence satisfy the declaration".

    ``section`` is the ``policy_vocabularies`` section the id was declared under, kept
    rather than discarded because a facet names one policy per section and the same id
    may be declared under more than one. Without it a consumer looking a rule up by id
    alone would let a rule written for one section decide another section's field.
    """

    policy_id: str
    composition: Primitive
    manual_review_required: bool = False
    #: Empty only for a Rule built outside :func:`pack_policy_rules`; it then matches no
    #: section, so an unbound rule falls back to review rather than deciding anything.
    section: str = ""


@dataclass(frozen=True)
class ResolvedValue:
    """A field reference that reached exactly one scalar."""

    ref: FieldRef
    value: Any


@dataclass(frozen=True)
class ResolutionFailure:
    """A field reference that reached nothing usable, and why.

    ``code`` is ``rule_field_unresolved`` for an ordinary miss, or the structured view's
    own ``structured_view_missing`` / ``structured_view_corrupt`` when the sidecar itself
    is the problem — so the reason names the real cause instead of blaming the pointer.
    """

    ref: FieldRef
    code: str
    detail: str

    def reason(self, source_id: str) -> str:
        return f"{self.code}: {source_id} {self.ref.display} does not resolve: {self.detail}"


@dataclass
class RuleContext:
    """Everything one source contributes to one rule evaluation, assembled by the caller.

    This module opens no files, so every document here arrives already loaded.
    ``structured_view`` is the parsed sidecar, or ``None`` when it could not be loaded;
    ``structured_view_error`` is the ``(result_code, detail)`` the caller got back from
    ``_structured_view.load_sidecar``, so a record-rooted failure echoes the real cause
    rather than a generic miss.

    ``provider_ids`` is precomputed by the caller from the three delivery-sidecar fields
    that actually carry a provider identity — ``provider_registration.id``,
    ``academic_provider`` and ``standards.registry_provider``. ``retrieved_by`` is **not**
    one of them: it identifies the *agent* that performed the fetch and holds path-shaped
    values such as ``fetch_sources.py/arxiv`` or ``fixture-agent/keepa``, so treating it
    as a provider id would let a rule match on the fetcher rather than on the source.

    ``now`` is injected so evaluation is reproducible, and ``domain_matches`` is injected
    from ``_evidence_policies`` so host matching has one implementation in the codebase
    rather than a second one here that drifts.
    """

    source_id: str
    structured_view: dict[str, Any] | None
    structured_view_error: tuple[str, str] | None
    provenance: dict[str, Any]
    question_frontmatter: dict[str, Any] | None
    origin_host: str | None
    provider_ids: tuple[str, ...]
    now: datetime
    domain_matches: Callable[[str, str], bool]

    def __post_init__(self) -> None:
        # Normalize the injected clock once so every evaluator can subtract from it
        # without repeating the tz-awareness dance. A naive clock reads as UTC, matching
        # `datetime_from_value`; anything that is not a datetime is a caller bug and is
        # better raised at construction than turned into a confusing per-source reason.
        if not isinstance(self.now, datetime):
            raise TypeError("RuleContext.now must be a datetime")
        self.now = _as_utc(self.now)
        self.provider_ids = tuple(self.provider_ids or ())


@dataclass(frozen=True)
class RuleEvaluation:
    """The verdict on one rule for one source, with the reasons that decided it.

    Every failure reason begins with one of :data:`RULE_REASON_PREFIXES`; a reason
    recorded on a pass never does. ``all_of`` reports *every* failing leaf rather than
    stopping at the first, because a pack author fixing a source wants the whole list in
    one pass; ``any_of`` short-circuits and reports the branch that carried it.
    """

    passed: bool
    reasons: list[str]


#: The largest UTC offset any zone uses (+14:00, Kiritimati). A timestamp that names no
#: offset is read as the *earliest* instant it could denote — its local time at +14:00 —
#: so an unknown zone can only make evidence look older than it is. Reading it as UTC
#: instead would make a delivery from any zone east of UTC look fresher, which is a false
#: pass, and this module's whole posture is that a gate may only fail closed.
MAX_UTC_OFFSET = timedelta(hours=14)


def _as_utc(value: datetime) -> datetime:
    """Plain normalization: a naive value is the caller's own UTC clock or literal text.

    Used where the value is *not* evidence whose zone is in question — the injected
    ``now``, and the canonical rendering an ``equals`` operand is compared as. Shifting
    either would be wrong: it would age every source by the offset, and it would stop a
    question's frontmatter timestamp comparing equal to the same instant in a record.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc_earliest(value: datetime) -> datetime:
    """The earliest instant a value could denote, for reading delivered evidence.

    Only :func:`datetime_from_value` uses this. A delivered timestamp that names no
    offset was written by a host whose zone we do not know, so it is read at the extreme
    that can only make the evidence look older — never fresher, which would be a pass the
    gate should have refused.

    A value within the offset of ``datetime.min`` cannot be shifted at all, so it is left
    where it is rather than raising: at that distance every ``max_age`` bound has failed
    by millions of years, and no bound exists that fourteen hours could flip.
    """
    if value.tzinfo is None:
        stamped = value.replace(tzinfo=timezone.utc)
        try:
            return stamped - MAX_UTC_OFFSET
        except OverflowError:
            return stamped
    return value.astimezone(timezone.utc)


#: A trailing UTC designator. RFC 3339 §5.6 spells its ABNF case-insensitively, so a
#: lowercase `z` is a valid timestamp that CPython's parser rejects on every version.
_ISO_ZULU_RE = re.compile(r"[Zz]$")
#: A trailing numeric offset written without its colon (`+0000`), which `fromisoformat`
#: did not accept before Python 3.11.
_ISO_BARE_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")
#: Fractional seconds. Before 3.11 `fromisoformat` accepted exactly 3 or 6 digits, so a
#: nanosecond timestamp — what Go-backed vendor APIs emit — parses on 3.11+ and not on
#: 3.10. Truncating to microseconds is what the later parser does anyway, which keeps one
#: pack's verdict from depending on the interpreter underneath it.
_ISO_FRACTION_RE = re.compile(r"\.(\d+)")


#: Characters that repeat whatever precedes them without an upper bound. `?` is absent on
#: purpose: it repeats at most once, so it cannot drive the exponential blowup this check
#: exists to refuse. `{` is absent too — it is a quantifier only in some spellings, and
#: `_opens_unbounded_repeat` decides which.
_UNBOUNDED_QUANTIFIERS = frozenset("*+")
#: `{n,}` repeats without an upper bound; `{n}` and `{n,m}` do not, and a bounded inner
#: repeat gives an outer quantifier nothing to explore. A `{` that spells none of the
#: three is a literal brace to `re`, and to this scanner.
_OPEN_ENDED_REPEAT_RE = re.compile(r"\{\d*,\}")
_ANY_REPEAT_RE = re.compile(r"\{\d+(,\d*)?\}")


def _regex_syntax_positions(pattern: str) -> list[tuple[int, str]]:
    """Every ``(index, char)`` in ``pattern`` that `re` reads as syntax, not as data.

    One scanner for both checks below, so there is a single definition of what regex
    syntax this module understands. Escapes consume the character after them, and a
    character class swallows everything to its close — including the quantifiers and
    parentheses inside it, which are literal members there.

    A ``]`` in the first position of a class (or first after ``[^``) is itself a literal
    member rather than the close, which is why the class scan starts past it: reading
    ``[]+]`` as the class ``[]`` followed by a stray ``+`` would both refuse valid
    patterns and mis-read what follows as syntax.
    """
    positions: list[tuple[int, str]] = []
    index = 0
    length = len(pattern)
    while index < length:
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            index += 1
            if index < length and pattern[index] == "^":
                index += 1
            if index < length and pattern[index] == "]":
                index += 1
            while index < length and pattern[index] != "]":
                index += 2 if pattern[index] == "\\" else 1
            index += 1
            continue
        positions.append((index, char))
        index += 1
    return positions


def _opens_unbounded_repeat(pattern: str, index: int) -> bool:
    """Whether the quantifier at ``index`` may repeat without an upper bound."""
    char = pattern[index]
    if char in _UNBOUNDED_QUANTIFIERS:
        return True
    return char == "{" and _OPEN_ENDED_REPEAT_RE.match(pattern, index) is not None


def _quantifier_at(pattern: str, index: int) -> bool:
    """Whether a quantifier of any kind sits at ``index``."""
    char = pattern[index]
    if char in _UNBOUNDED_QUANTIFIERS:
        return True
    return char == "{" and _ANY_REPEAT_RE.match(pattern, index) is not None


def _nested_quantifier_span(pattern: str) -> str | None:
    """Return the quantified group whose body can also blow up, or ``None`` when none can.

    `(a+)+` and `(a|a)+` are six characters, so a length cap cannot refuse them, and `re`
    offers no step budget or timeout to bound them at match time. The input that triggers
    the blowup is the *source's* text, not the pack's, so a verbose upstream record would
    hang evaluation with the gate neither open nor closed. Refusing the construct where
    the pack author can see the message is the only place the cost is bounded.

    Two families reach exponential time, and both are refused here: a group repeated
    without bound whose body also repeats without bound (`(a+)+`), and one whose
    alternatives can match the same text so the engine must try each ordering
    (`(a|a)+`, `(a|ab)*`). A bounded inner repeat is not either of them — `(\\d{2}-)+`
    gives the outer quantifier one way to match and is left alone.

    Deliberately syntactic and conservative: it reads shape, not language emptiness, so
    it can refuse a pattern that would have been safe. Rewriting such a pattern is always
    possible; hanging on real evidence is not always recoverable.
    """
    positions = _regex_syntax_positions(pattern)
    opens: list[int] = []
    for cursor, (index, char) in enumerate(positions):
        if char == "(":
            opens.append(cursor)
        elif char == ")" and opens:
            open_cursor = opens.pop()
            start = positions[open_cursor][0]
            after = index + 1
            if after >= len(pattern) or not _opens_unbounded_repeat(pattern, after):
                continue
            inner = positions[open_cursor + 1 : cursor]
            if _repeats_within(pattern, inner) or _alternatives_overlap(pattern, inner):
                return pattern[start : after + 1]
    return None


def _repeats_within(pattern: str, inner: list[tuple[int, str]]) -> bool:
    """Whether a group body repeats without an upper bound of its own."""
    return any(_opens_unbounded_repeat(pattern, index) for index, _ in inner)


def _alternatives_overlap(pattern: str, inner: list[tuple[int, str]]) -> bool:
    """Whether two of a group's alternatives can begin with the same text.

    Disjoint alternatives (`(foo|bar)+`) give the engine one way to match each input and
    are safe; alternatives sharing a first character (`(a|a)+`, `(a|ab)*`) give it two,
    and a repeated group multiplies that choice by its length. Comparing only the leading
    token keeps this cheap and errs toward refusing: anything not a plain literal — a
    class, a nested group, a dot, an escape — is treated as able to overlap everything.
    """
    depth = 0
    leads: list[str | None] = []
    expect_lead = True
    for index, char in inner:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            expect_lead = True
            continue
        if depth == 0 and expect_lead:
            # `?:` and friends are group syntax, not the alternative's first token.
            if char == "?" and leads and leads[-1] is None:
                continue
            leads.append(None if char in "[(.\\^" else pattern[index])
            expect_lead = False
    if expect_lead:
        leads.append(None)
    if len(leads) < 2:
        return False
    if any(lead is None for lead in leads):
        return True
    return len(set(leads)) != len(leads)


def _normalize_iso_offset(text: str) -> str:
    """Spell an RFC 3339 timestamp the way every supported `fromisoformat` accepts it."""
    text = _ISO_ZULU_RE.sub("+00:00", text)
    text = _ISO_BARE_OFFSET_RE.sub(r"\1:\2", text)
    # Padded as well as truncated: `.12` is two digits, which 3.10 also rejects, and
    # `.120000` is the same instant.
    return _ISO_FRACTION_RE.sub(lambda match: "." + match.group(1)[:6].ljust(6, "0"), text, count=1)


def datetime_from_value(value: Any) -> datetime | None:
    """Read a value as a tz-aware UTC instant, or ``None`` when it is not one.

    ``_evidence_policies.date_from_value`` exists already, but it truncates to a calendar
    date and so cannot express "48 hours" at all — under it a quote fetched this morning
    and one fetched at 00:01 yesterday are the same age. This is the instant-precision
    reader ``max_age`` needs; the two are deliberately separate rather than one widened
    function, because every existing caller of the date reader wants a date.

    A value that names **no UTC offset** — a bare date, a naive datetime, an offset-less
    ISO string — is read as the earliest instant it could denote, by way of
    :data:`MAX_UTC_OFFSET`. That is the conservative direction on purpose: under
    ``max_age`` it can only make a source look *older* than it is, never fresher, so the
    choice can produce a false ``fail`` (which a human sees and can correct) but never a
    false ``pass`` (which nobody sees).
    """
    if isinstance(value, datetime):
        return _as_utc_earliest(value)
    if isinstance(value, date):
        return _as_utc_earliest(datetime(value.year, value.month, value.day))
    if not isinstance(value, str) or not value.strip():
        return None
    text = _normalize_iso_offset(value.strip())
    try:
        return _as_utc_earliest(datetime.fromisoformat(text))
    except ValueError:
        pass
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc_earliest(datetime(parsed.year, parsed.month, parsed.day))


def _canonical_operand(value: Any) -> Any:
    """Render YAML's own date types the way the JSON side of the comparison writes them.

    PyYAML parses ``2026-08-01`` into a ``date`` and ``2026-08-01T09:00:00Z`` into a
    ``datetime``, so a question's frontmatter can hand us a value that is not a JSON
    scalar at all. A structured view, being JSON, always writes those as strings. Folding
    the YAML side to the same ISO text is what lets the two compare equal instead of
    failing as "not a scalar".
    """
    if isinstance(value, datetime):
        return _as_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _value_kind(value: Any) -> str:
    """Name a non-scalar the way the document that carried it would."""
    return _JSON_KINDS.get(type(value), type(value).__name__)


def _format_number(value: Decimal) -> str:
    """Render a declared bound the way its author wrote it: 48, not 48.0 or 4.8E+1.

    ``normalize`` is a context operation, so a bound whose exponent exceeds the decimal
    context's ``Emax`` raises rather than returning a string. That bound parsed cleanly
    at declaration time, so the raise would land at answer time, inside the reason text
    built *before* the comparison — turning a rule that was about to pass into a
    traceback. An exponent that extreme has no readable plain form anyway, so it falls
    back to the value's own spelling.
    """
    try:
        return format(value.normalize(), "f")
    except decimal.DecimalException:
        return str(value)


def _reworded(detail: str, document_label: str) -> str:
    if document_label == _ROOT_DOCUMENT_LABELS["record"] or not detail.startswith(_MISSING_KEY_PHRASE):
        return detail
    return f"The {document_label} has no key{detail[len(_MISSING_KEY_PHRASE):]}"


def _reason(prefix: str, source_id: str, rest: str, ref: FieldRef | None = None) -> str:
    location = f" {ref.display}" if ref is not None else ""
    return f"{prefix}: {source_id}{location} {rest}"


def _satisfied(source_id: str, rest: str, ref: FieldRef | None = None) -> str:
    location = f" {ref.display}" if ref is not None else ""
    return f"{source_id}{location} {rest}"


def _parse_field_ref(value: Any, *, rooted: bool, label: str) -> tuple[FieldRef | None, str | None]:
    """Parse one field reference into ``(ref, error)``, with exactly one populated."""
    if not isinstance(value, str) or not value.strip():
        return None, f"{label} must be a non-empty pointer string"
    text = value.strip()
    root: str | None = None
    remainder = text
    if rooted:
        head, separator, rest = text.partition("/")
        if head not in FIELD_ROOTS:
            return None, (
                f"{label} must start with one of {', '.join(FIELD_ROOTS)}; got {head!r}"
            )
        if not separator or not rest.strip():
            return None, f"{label} must name a field below {head}, like {head}/some/field"
        root = head
        remainder = rest
    elif text.partition("/")[0] in FIELD_ROOTS:
        return None, (
            f"{label} resolves against the question's frontmatter and must be a bare pointer "
            f"with no root segment; drop the leading {text.partition('/')[0]!r}"
        )
    pointer = _structured_view.normalize_pointer(remainder)
    if pointer == "":
        return None, f"{label} must name a field, not the whole document"
    for token in pointer.split("/")[1:]:
        if _structured_view.unescape_token(token) is None:
            return None, (
                f"{label} contains reference token {token!r}, which is not valid RFC 6901 escaping: "
                "inside a token ~ is written ~0 and / is written ~1"
            )
    return FieldRef(root, pointer), None


def _key_errors(
    body: dict[str, Any],
    label: str,
    *,
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    missing = [key for key in required if key not in body]
    if missing:
        errors.append(f"{label} is missing required key(s): {', '.join(missing)}")
    allowed = set(required) | set(optional)
    unknown = sorted(str(key) for key in body if str(key) not in allowed)
    if unknown:
        errors.append(f"{label} has unknown key(s): {', '.join(unknown)}")
    return errors


def _parse_operand(
    body: dict[str, Any],
    label: str,
    literal_key: str,
    question_key: str,
    *,
    required: bool,
    numeric: bool = False,
) -> tuple[Operand | None, list[str]]:
    has_literal = literal_key in body
    has_question = question_key in body
    if has_literal and has_question:
        return None, [f"{label} declares both {literal_key} and {question_key}; exactly one is allowed"]
    if not has_literal and not has_question:
        if required:
            return None, [f"{label} must declare exactly one of {literal_key} or {question_key}"]
        return None, []
    if has_question:
        ref, error = _parse_field_ref(body.get(question_key), rooted=False, label=f"{label}.{question_key}")
        if error is not None:
            return None, [error]
        return Operand(ref=ref), []
    literal = _canonical_operand(body.get(literal_key))
    rendered = _structured_view.canonical_scalar(literal)
    if rendered is None:
        return None, [
            f"{label}.{literal_key} must be a scalar (string, number, boolean or null); "
            f"got a JSON {_value_kind(literal)}"
        ]
    if numeric and _structured_view.parse_decimal(rendered) is None:
        return None, [f"{label}.{literal_key} must be a decimal number; got {rendered!r}"]
    return Operand(literal=literal), []


def _parse_max_age(body: dict[str, Any], label: str) -> tuple[Primitive | None, list[str]]:
    errors = _key_errors(body, label, required=("field", "hours"))
    if errors:
        return None, errors
    ref, error = _parse_field_ref(body.get("field"), rooted=True, label=f"{label}.field")
    if error is not None:
        return None, [error]
    hours = body.get("hours")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return None, [f"{label}.hours must be a positive number of hours; got {hours!r}"]
    if isinstance(hours, float) and not math.isfinite(hours):
        return None, [f"{label}.hours must be a finite number of hours; got {hours!r}"]
    bound = Decimal(str(hours))
    if bound <= 0:
        return None, [f"{label}.hours must be greater than zero; got {hours!r}"]
    return Primitive(name="max_age", field=ref, hours=bound), []


def _parse_equals(body: dict[str, Any], label: str) -> tuple[Primitive | None, list[str]]:
    errors = _key_errors(body, label, required=("field",), optional=("value", "question_field"))
    if errors:
        return None, errors
    ref, error = _parse_field_ref(body.get("field"), rooted=True, label=f"{label}.field")
    if error is not None:
        return None, [error]
    operand, operand_errors = _parse_operand(body, label, "value", "question_field", required=True)
    if operand_errors:
        return None, operand_errors
    return Primitive(name="equals", field=ref, operand=operand), []


def _parse_numeric_range(body: dict[str, Any], label: str) -> tuple[Primitive | None, list[str]]:
    errors = _key_errors(
        body,
        label,
        required=("field",),
        optional=("min", "max", "min_question_field", "max_question_field"),
    )
    if errors:
        return None, errors
    ref, error = _parse_field_ref(body.get("field"), rooted=True, label=f"{label}.field")
    if error is not None:
        return None, [error]
    minimum, min_errors = _parse_operand(
        body, label, "min", "min_question_field", required=False, numeric=True
    )
    if min_errors:
        return None, min_errors
    maximum, max_errors = _parse_operand(
        body, label, "max", "max_question_field", required=False, numeric=True
    )
    if max_errors:
        return None, max_errors
    if minimum is None and maximum is None:
        return None, [
            f"{label} must declare at least one bound: min, max, min_question_field or max_question_field"
        ]
    return Primitive(name="numeric_range", field=ref, minimum=minimum, maximum=maximum), []


def _parse_regex(body: dict[str, Any], label: str) -> tuple[Primitive | None, list[str]]:
    errors = _key_errors(body, label, required=("field", "pattern"))
    if errors:
        return None, errors
    ref, error = _parse_field_ref(body.get("field"), rooted=True, label=f"{label}.field")
    if error is not None:
        return None, [error]
    pattern = body.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return None, [f"{label}.pattern must be a non-empty regular expression string"]
    if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
        return None, [
            f"{label}.pattern is {len(pattern)} characters; the limit is {MAX_REGEX_PATTERN_LENGTH}"
        ]
    span = _nested_quantifier_span(pattern)
    if span is not None:
        return None, [
            f"{label}.pattern repeats a group that already repeats ({span!r}), which can "
            "backtrack catastrophically on ordinary source text; rewrite it without the "
            "nested quantifier"
        ]
    try:
        # Compiled here, at declaration time, so a pack ships a pattern that is known to
        # compile rather than one that raises on the first source it is asked about.
        # `re.compile` signals an oversized repetition count with OverflowError and a
        # deeply nested one with RecursionError, neither of which is an `re.error`; they
        # are caught here because `declaration_errors` promises never to raise.
        compiled = re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as exc:
        return None, [f"{label}.pattern is not a valid regular expression: {exc}"]
    return Primitive(name="regex", field=ref, pattern=pattern, compiled=compiled), []


def _parse_one_of_provenance(body: dict[str, Any], label: str) -> tuple[Primitive | None, list[str]]:
    errors = _key_errors(body, label, optional=("providers", "domains"))
    if errors:
        return None, errors
    collected: dict[str, tuple[str, ...]] = {}
    for key in ("providers", "domains"):
        if key not in body:
            collected[key] = ()
            continue
        raw = body[key]
        if not isinstance(raw, list) or not raw:
            return None, [f"{label}.{key} must be a non-empty list of strings"]
        values: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item.strip():
                return None, [f"{label}.{key} must contain only non-empty strings; got {item!r}"]
            values.append(item.strip())
        collected[key] = tuple(values)
    if not collected["providers"] and not collected["domains"]:
        return None, [f"{label} must declare a non-empty providers list, domains list, or both"]
    return (
        Primitive(
            name="one_of_provenance",
            providers=collected["providers"],
            domains=collected["domains"],
        ),
        [],
    )


_LEAF_PARSERS = {
    "max_age": _parse_max_age,
    "equals": _parse_equals,
    "numeric_range": _parse_numeric_range,
    "regex": _parse_regex,
    "one_of_provenance": _parse_one_of_provenance,
}


def _parse_composition(
    name: str, body: Any, *, label: str, depth: int
) -> tuple[Primitive | None, list[str]]:
    if depth > MAX_COMPOSITION_DEPTH:
        return None, [
            f"{label} nests compositions {depth} deep; the limit is {MAX_COMPOSITION_DEPTH}"
        ]
    if not isinstance(body, list) or not body:
        return None, [f"{label} must be a non-empty list of primitives"]
    children: list[Primitive] = []
    errors: list[str] = []
    for index, entry in enumerate(body):
        child, child_errors = _parse_primitive(entry, label=f"{label}[{index}]", depth=depth + 1)
        errors.extend(child_errors)
        if child is not None:
            children.append(child)
    if errors:
        return None, errors
    return Primitive(name=name, children=tuple(children)), []


def _parse_primitive(entry: Any, *, label: str, depth: int) -> tuple[Primitive | None, list[str]]:
    allowed = ", ".join(PRIMITIVE_NAMES)
    if not isinstance(entry, dict) or len(entry) != 1:
        return None, [
            f"{label} must be a mapping naming exactly one primitive; allowed primitives are: {allowed}"
        ]
    name = next(iter(entry))
    if not isinstance(name, str) or name not in PRIMITIVE_NAMES:
        return None, [f"{label} names unknown primitive {name!r}; allowed primitives are: {allowed}"]
    body = entry[name]
    scoped = f"{label}.{name}"
    if name in COMPOSITION_PRIMITIVES:
        return _parse_composition(name, body, label=scoped, depth=depth)
    if not isinstance(body, dict):
        return None, [f"{scoped} must be a mapping of primitive arguments"]
    return _LEAF_PARSERS[name](body, scoped)


def _parse_rule(policy_id: str, declaration: Any, label: str, section: str = "") -> tuple[Rule | None, list[str]]:
    if not isinstance(declaration, dict):
        return None, [f"{label} must be a mapping declaring exactly one of all_of or any_of"]
    present = [key for key in COMPOSITION_PRIMITIVES if key in declaration]
    if len(present) != 1:
        found = ", ".join(present) if present else "neither"
        return None, [f"{label} must declare exactly one of all_of or any_of; it declares {found}"]
    unknown = sorted(str(key) for key in declaration if str(key) not in RULE_KEYS)
    if unknown:
        return None, [f"{label} has unknown key(s): {', '.join(unknown)}"]
    manual_review = declaration.get("manual_review_required", False)
    if not isinstance(manual_review, bool):
        return None, [f"{label}.manual_review_required must be true or false; got {manual_review!r}"]
    name = present[0]
    composition, errors = _parse_composition(
        name, declaration[name], label=f"{label}.{name}", depth=1
    )
    if composition is None:
        return None, errors
    return (
        Rule(
            policy_id=policy_id,
            composition=composition,
            manual_review_required=manual_review,
            section=section,
        ),
        [],
    )


def _declared_vocabulary(domain_pack: dict[str, Any]) -> dict[str, list[str]]:
    """Map every policy id the pack declares to the vocabulary sections it sits in.

    A list rather than one section: an id may be declared under several, and which one a
    rule decides is then genuinely ambiguous. Recording all of them lets
    :func:`_collect_declarations` refuse that where it is written instead of leaving every
    consumer to guess.
    """
    vocabularies = domain_pack.get("policy_vocabularies")
    if not isinstance(vocabularies, dict):
        return {}
    declared: dict[str, list[str]] = {}
    for section in (*RULE_TARGET_SECTIONS, EVIDENCE_PATHS_SECTION):
        definitions = vocabularies.get(section)
        if not isinstance(definitions, dict):
            continue
        for policy_id in definitions:
            if not isinstance(policy_id, str) or not policy_id.strip():
                continue
            declared.setdefault(policy_id.strip(), []).append(section)
    return declared


def _collect_declarations(domain_pack: Any) -> tuple[dict[str, Rule], list[str]]:
    """Parse ``domain_pack.policy_rules`` into (parsed rules, human-readable errors).

    Single source of truth for both consumers: :func:`declaration_errors` surfaces the
    error list as pack-validate findings, :func:`pack_policy_rules` raises on it. Rules
    come back as dataclasses so the evaluator never re-reads the declaration — a second
    parse is a second chance for the two to disagree about what the pack said.
    """
    raw = domain_pack.get("policy_rules") if isinstance(domain_pack, dict) else None
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["domain_pack.policy_rules must be a mapping of policy id to rule declaration"]

    pack_name = domain_pack.get("name")
    pack_name = pack_name.strip() if isinstance(pack_name, str) and pack_name.strip() else None
    vocabulary = _declared_vocabulary(domain_pack)

    rules: dict[str, Rule] = {}
    errors: list[str] = []
    for key, declaration in raw.items():
        if not isinstance(key, str) or not key.strip():
            errors.append(f"domain_pack.policy_rules[{key!r}] must be a namespaced policy id string")
            continue
        policy_id = key.strip()
        label = f"domain_pack.policy_rules[{policy_id}]"
        if PACK_POLICY_ID_RE.fullmatch(policy_id) is None:
            errors.append(f"{label} must be namespaced like pack:<pack-name>/<policy-id>")
            continue
        namespace = policy_id.split("/", 1)[0][len("pack:") :]
        if pack_name is not None and namespace != pack_name:
            errors.append(
                f"{label} declares namespace {namespace!r} but the pack is named {pack_name!r}"
            )
            continue
        sections = vocabulary.get(policy_id)
        if not sections:
            errors.append(
                f"{label} is not declared under domain_pack.policy_vocabularies; "
                f"declare it under one of: {', '.join(RULE_TARGET_SECTIONS)}"
            )
            continue
        rule_sections = [name for name in sections if name in RULE_TARGET_SECTIONS]
        if len(rule_sections) > 1:
            # Refused where it is written. A facet names one policy per section, so a rule
            # on an id declared under several cannot say which field it decides — and
            # guessing would let a rule written for one section decide another's evidence.
            errors.append(
                f"{label} is declared under more than one rule-carrying vocabulary section "
                f"({', '.join(rule_sections)}), so the rule cannot say which one it decides; "
                "give each section its own policy id"
            )
            continue
        section = rule_sections[0] if rule_sections else sections[0]
        if section not in RULE_TARGET_SECTIONS:
            errors.append(
                f"{label} is declared under domain_pack.policy_vocabularies.{section}, which carries "
                f"no rules; only {', '.join(RULE_TARGET_SECTIONS)} policies can be decided by a rule"
            )
            continue
        if policy_id in rules:
            errors.append(f"{label} is declared more than once")
            continue
        rule, rule_errors = _parse_rule(policy_id, declaration, label, section)
        errors.extend(rule_errors)
        if rule is not None:
            rules[policy_id] = rule
    return rules, errors


def rule_summary(rule: Rule) -> dict[str, Any]:
    """The JSON-safe description of one rule that every published surface reports.

    `evidence-wiki pack validate` and `evidence-wiki contract` both answer "what does this
    pack decide itself", and they must answer it identically. Built here, beside the
    ``Rule`` and ``Primitive`` shapes it walks, so a new field lands in both surfaces at
    once rather than in whichever one its author remembered.
    """
    return {
        "primitives": sorted(_primitive_names(rule.composition)),
        "manual_review_required": rule.manual_review_required,
        # The vocabulary section the rule decides. A consumer reasoning about which fields
        # a pack automates cannot infer it from the policy id alone.
        "section": rule.section,
    }


def _primitive_names(primitive: Primitive) -> set[str]:
    """Every primitive name one rule's composition tree uses, compositions included."""
    names = {primitive.name}
    for child in primitive.children:
        names |= _primitive_names(child)
    return names


def declaration_errors(domain_pack: Any) -> list[str]:
    """Return every problem with a pack's ``policy_rules`` block; empty when valid.

    Never raises, whatever it is handed: this is the function a pack validator calls on
    unvalidated author input, and a traceback there would report a broken tool rather
    than a broken pack.
    """
    _, errors = _collect_declarations(domain_pack)
    return errors


def pack_policy_rules(config: dict[str, Any] | None) -> dict[str, Rule]:
    """Return the active domain pack's parsed rules, keyed by policy id.

    Raises :class:`PolicyRuleError` when the declaration is malformed, so a broken pack
    fails the command instead of silently losing its automation and sending every policy
    back to manual review. ``{}`` means the pack declared no rules, which is different
    and is not an error.
    """
    domain_pack = config.get("domain_pack") if isinstance(config, dict) else None
    rules, errors = _collect_declarations(domain_pack)
    if errors:
        raise PolicyRuleError(
            "CONFIG_INVALID",
            f"research.yml domain_pack.policy_rules is invalid: {errors[0]}",
            remediation="Fix domain_pack.policy_rules as documented in docs/research-yml.md.",
            details={"errors": errors},
        )
    return rules


def resolve_field(context: RuleContext, ref: FieldRef) -> ResolvedValue | ResolutionFailure:
    """Resolve one field reference against the document its root names.

    A reference that reaches a subtree rather than a scalar is a failure here rather than
    at each primitive, because "the pointer cites an object" is one mistake with one fix
    however the value was going to be compared.
    """
    if ref.root == "record":
        if not isinstance(context.structured_view, dict):
            code, detail = context.structured_view_error or (
                RESULT_STRUCTURED_VIEW_MISSING,
                "the source carries no structured view a rule can resolve against",
            )
            return ResolutionFailure(ref, code, detail)
        document: Any = context.structured_view
    elif ref.root == "provenance":
        document = context.provenance if isinstance(context.provenance, dict) else {}
    else:
        if not isinstance(context.question_frontmatter, dict):
            return ResolutionFailure(
                ref, REASON_FIELD_UNRESOLVED, "the question carries no frontmatter mapping"
            )
        document = context.question_frontmatter

    resolution = _structured_view.resolve_pointer(document, ref.pointer)
    if not resolution.ok:
        return ResolutionFailure(
            ref, REASON_FIELD_UNRESOLVED, _reworded(resolution.detail, ref.document_label)
        )
    value = _canonical_operand(resolution.value)
    if not _structured_view.is_scalar(value):
        return ResolutionFailure(
            ref,
            REASON_FIELD_UNRESOLVED,
            f"it reaches a JSON {_value_kind(value)}, and a rule decides over one scalar field",
        )
    return ResolvedValue(ref, value)


def _operand_value(operand: Operand, context: RuleContext) -> tuple[Any, ResolutionFailure | None]:
    if operand.ref is None:
        return operand.literal, None
    resolved = resolve_field(context, operand.ref)
    if isinstance(resolved, ResolutionFailure):
        return None, resolved
    return resolved.value, None


def _evaluate_max_age(primitive: Primitive, context: RuleContext) -> tuple[bool, str]:
    resolved = resolve_field(context, primitive.field)
    if isinstance(resolved, ResolutionFailure):
        return False, resolved.reason(context.source_id)
    rendered = _structured_view.canonical_scalar(resolved.value)
    moment = datetime_from_value(resolved.value)
    if moment is None:
        return False, _reason(
            REASON_FIELD_UNRESOLVED,
            context.source_id,
            f"{rendered!r} is not an ISO 8601 timestamp, so max_age cannot decide it.",
            primitive.field,
        )
    bound = _format_number(primitive.hours)
    age = Decimal(str((context.now - moment).total_seconds())) / _SECONDS_PER_HOUR
    skew = Decimal(MAX_AGE_FUTURE_SKEW_MINUTES) / _MINUTES_PER_HOUR
    if age < -skew:
        return False, _reason(
            REASON_FUTURE_TIMESTAMP,
            context.source_id,
            f"{rendered} is {format(-age, '.1f')}h in the future; max_age tolerates at most "
            f"{MAX_AGE_FUTURE_SKEW_MINUTES} minutes of clock skew.",
            primitive.field,
        )
    if age > primitive.hours:
        return False, _reason(
            REASON_STALE,
            context.source_id,
            f"{rendered} is {format(age, '.1f')}h old; max_age allows {bound}h.",
            primitive.field,
        )
    return True, _satisfied(
        context.source_id,
        f"{rendered} is {format(age, '.1f')}h old; max_age allows {bound}h.",
        primitive.field,
    )


def _evaluate_equals(primitive: Primitive, context: RuleContext) -> tuple[bool, str]:
    resolved = resolve_field(context, primitive.field)
    if isinstance(resolved, ResolutionFailure):
        return False, resolved.reason(context.source_id)
    expected, failure = _operand_value(primitive.operand, context)
    if failure is not None:
        return False, failure.reason(context.source_id)
    target_text = _structured_view.canonical_scalar(resolved.value)
    expected_text = _structured_view.canonical_scalar(expected)
    if not _structured_view.expected_matches(resolved.value, expected):
        return False, _reason(
            REASON_VALUE_MISMATCH,
            context.source_id,
            f"resolves to {target_text!r}, which does not equal the expected {expected_text!r} "
            f"({primitive.operand.description}).",
            primitive.field,
        )
    return True, _satisfied(
        context.source_id,
        f"resolves to {target_text!r}, equal to the expected {expected_text!r} "
        f"({primitive.operand.description}).",
        primitive.field,
    )


def _evaluate_numeric_range(primitive: Primitive, context: RuleContext) -> tuple[bool, str]:
    resolved = resolve_field(context, primitive.field)
    if isinstance(resolved, ResolutionFailure):
        return False, resolved.reason(context.source_id)
    target_text = _structured_view.canonical_scalar(resolved.value)
    target = _structured_view.parse_decimal(target_text)
    if target is None:
        return False, _reason(
            REASON_OUT_OF_RANGE,
            context.source_id,
            f"resolves to {target_text!r}, which is not a decimal number numeric_range can compare.",
            primitive.field,
        )
    bounds: list[tuple[str, Decimal]] = []
    for name, operand in (("min", primitive.minimum), ("max", primitive.maximum)):
        if operand is None:
            continue
        value, failure = _operand_value(operand, context)
        if failure is not None:
            return False, failure.reason(context.source_id)
        rendered = _structured_view.canonical_scalar(value)
        parsed = _structured_view.parse_decimal(rendered)
        if parsed is None:
            return False, _reason(
                REASON_OUT_OF_RANGE,
                context.source_id,
                f"has a {name} bound of {rendered!r} ({operand.description}), "
                "which is not a decimal number.",
                primitive.field,
            )
        bounds.append((name, parsed))
    described = ", ".join(f"{name} {_format_number(value)}" for name, value in bounds)
    for name, bound in bounds:
        if (name == "min" and target < bound) or (name == "max" and target > bound):
            return False, _reason(
                REASON_OUT_OF_RANGE,
                context.source_id,
                f"resolves to {target_text}, outside the inclusive range ({described}).",
                primitive.field,
            )
    return True, _satisfied(
        context.source_id,
        f"resolves to {target_text}, inside the inclusive range ({described}).",
        primitive.field,
    )


def _evaluate_regex(primitive: Primitive, context: RuleContext) -> tuple[bool, str]:
    resolved = resolve_field(context, primitive.field)
    if isinstance(resolved, ResolutionFailure):
        return False, resolved.reason(context.source_id)
    # A present-but-null field resolves successfully — `is_scalar(None)` is true — and
    # canonicalizes to the text "null", which any permissive pattern matches. `null` is
    # what a normalizer writes when it could not extract a value, so matching its
    # rendering would pass an identity check on precisely the evidence that is missing.
    # Booleans render the same way and are never an identity either.
    if resolved.value is None or isinstance(resolved.value, bool):
        return False, _reason(
            REASON_REGEX_MISMATCH,
            context.source_id,
            f"holds {_structured_view.canonical_scalar(resolved.value)}, not text a pattern can identify.",
            primitive.field,
        )
    target_text = _structured_view.canonical_scalar(resolved.value)
    # `fullmatch`, never `search`: `search` would reintroduce implicit containment, which
    # is precisely the weakness structured-view equality exists to remove. An author who
    # wants substring behaviour writes `.*B0.*` — permissiveness has to be declared.
    if primitive.compiled.fullmatch(target_text) is None:
        return False, _reason(
            REASON_REGEX_MISMATCH,
            context.source_id,
            f"resolves to {target_text!r}, which is not a full match for pattern {primitive.pattern!r}.",
            primitive.field,
        )
    return True, _satisfied(
        context.source_id,
        f"resolves to {target_text!r}, a full match for pattern {primitive.pattern!r}.",
        primitive.field,
    )


def _evaluate_one_of_provenance(primitive: Primitive, context: RuleContext) -> tuple[bool, str]:
    for allowed in primitive.providers:
        for observed in context.provider_ids:
            # Exact but for case, not `expected_matches`. That helper also folds NFKC,
            # dashes and whitespace, which is right for grounding prose against a record
            # and wrong for an identity allowlist: it would admit a fullwidth spelling and
            # an en-dash lookalike as the id the pack allowed. Case is the one fold worth
            # keeping — registry metadata spells the same provider `ISO` or `iso`
            # depending on who wrote the sidecar, and a pack cannot know which.
            if observed.casefold() == allowed.casefold():
                return True, _satisfied(
                    context.source_id,
                    f"was delivered by provider {observed!r}, which one_of_provenance allows.",
                )
    host = context.origin_host
    if host:
        for domain in primitive.domains:
            if context.domain_matches(host, domain):
                return True, _satisfied(
                    context.source_id,
                    f"has origin host {host!r}, which matches allowed domain {domain!r}.",
                )
    checked: list[str] = []
    if primitive.providers:
        checked.append(f"allowed providers [{', '.join(primitive.providers)}]")
    if primitive.domains:
        checked.append(f"allowed domains [{', '.join(primitive.domains)}]")
    observed_ids = ", ".join(repr(value) for value in context.provider_ids) or "none"
    return False, _reason(
        REASON_PROVENANCE_NOT_ALLOWED,
        context.source_id,
        f"has provider ids ({observed_ids}) and origin host {host!r}, "
        f"matching none of: {'; '.join(checked)}.",
    )


_LEAF_EVALUATORS = {
    "max_age": _evaluate_max_age,
    "equals": _evaluate_equals,
    "numeric_range": _evaluate_numeric_range,
    "regex": _evaluate_regex,
    "one_of_provenance": _evaluate_one_of_provenance,
}


def _evaluate_primitive(primitive: Primitive, context: RuleContext) -> tuple[bool, list[str]]:
    if primitive.name == "all_of":
        passed = True
        failures: list[str] = []
        satisfied: list[str] = []
        for child in primitive.children:
            child_passed, child_reasons = _evaluate_primitive(child, context)
            if child_passed:
                satisfied.extend(child_reasons)
            else:
                passed = False
                failures.extend(child_reasons)
        return passed, (satisfied if passed else failures)
    if primitive.name == "any_of":
        collected: list[str] = []
        for child in primitive.children:
            child_passed, child_reasons = _evaluate_primitive(child, context)
            if child_passed:
                return True, child_reasons
            collected.extend(child_reasons)
        return False, collected
    child_passed, reason = _LEAF_EVALUATORS[primitive.name](primitive, context)
    return child_passed, [reason]


def evaluate_rule(rule: Rule, context: RuleContext) -> RuleEvaluation:
    """Decide one parsed rule against one source's context.

    Pure: it reads the context and nothing else, so the same context always yields the
    same verdict and the same reasons. ``rule.manual_review_required`` is not consulted
    here — it is the evaluator's business what a passing rule means for a policy that
    also wants a human.
    """
    passed, reasons = _evaluate_primitive(rule.composition, context)
    return RuleEvaluation(passed=passed, reasons=reasons)
