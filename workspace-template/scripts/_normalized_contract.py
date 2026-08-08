#!/usr/bin/env python3
"""Validator for the public normalized-record contract in docs/normalized-source-format.md.

A normalized record is first-class evidence regardless of which tool wrote it, which is
what lets an external normalizer serve source kinds this package does not extract
itself. That promise only holds if "conforms to the contract" is a decidable question,
so this module is the single place that decides it: ``normalize_verify.py`` reports the
answer, lint gates its acceptance of foreign records on it, and the normalizer checks
its own adapter output against it. One code path, so a record can never be accepted by
one consumer and rejected by another.

Violations are returned, not raised. A record that breaks the contract is a reportable
finding about workspace data, not a failure of the run that found it: a directory walk
must be able to report every bad record rather than abort on the first. Callers decide
severity — the contract states what is wrong, not what it costs.

Every check is expressed against what the format document already promises a reader.
Where this module is deliberately lenient, the reason is recorded at the check, because
a lenient contract check is a gap a wrong record can travel through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read normalized records") from exc


# Contract version stamped into records and accepted when reading them. `normalizer`
# identifies the producing tool; `normalized_format` identifies the contract shape, so
# an external normalizer can target the format without impersonating this package.
NORMALIZED_FORMAT_VERSION = 1
ACCEPTED_NORMALIZED_FORMATS = frozenset({NORMALIZED_FORMAT_VERSION})
LEGACY_NORMALIZED_FORMAT_VERSION = 0
# Records these names produced predate the versioned contract, so an absent
# `normalized_format` reads as legacy for them and only for them.
NATIVE_NORMALIZER_NAMES = frozenset({"normalize_sources.py", "manual"})

RECORD_TYPE = "normalized_source"
ALLOWED_STATUSES = frozenset({"stubbed", "content_extracted", "partial", "failed"})
REQUIRED_SECTIONS = (
    "Citation Metadata",
    "Abstract",
    "Outline",
    "Extracted Text",
    "Figures and Tables",
    "Links",
    "Raw Source Paths",
    "Parse Warnings",
)
PARSE_WARNINGS_SECTION = "Parse Warnings"

FRONTMATTER_MISSING = "NORMALIZED_CONTRACT_FRONTMATTER_MISSING"
FRONTMATTER_INVALID = "NORMALIZED_CONTRACT_FRONTMATTER_INVALID"
FORMAT_VERSION_UNSUPPORTED = "NORMALIZED_CONTRACT_FORMAT_VERSION_UNSUPPORTED"
SECTIONS_INVALID = "NORMALIZED_CONTRACT_SECTIONS_INVALID"
MANIFEST_MISMATCH = "NORMALIZED_CONTRACT_MANIFEST_MISMATCH"
WARNINGS_INCONSISTENT = "NORMALIZED_CONTRACT_WARNINGS_INCONSISTENT"
RENDERED_COVERAGE_INVALID = "NORMALIZED_CONTRACT_RENDERED_COVERAGE_INVALID"

VIOLATION_CODES = (
    FRONTMATTER_MISSING,
    FRONTMATTER_INVALID,
    FORMAT_VERSION_UNSUPPORTED,
    SECTIONS_INVALID,
    MANIFEST_MISMATCH,
    WARNINGS_INCONSISTENT,
    RENDERED_COVERAGE_INVALID,
)

# A rendering that caps content is honest only if it says so. `extraction_method:
# adapter` marks a record this package produced through the adapter protocol, where the
# stats can be demanded; records from any other writer may declare them and are checked
# when they do.
ADAPTER_EXTRACTION_METHOD = "adapter"
RENDERED_COVERAGE_KEYS = frozenset({"total_values", "rendered_values", "ratio", "sections"})
RENDERED_COVERAGE_SECTION_KEYS = frozenset({"heading", "total", "rendered", "note"})
# Ratio is reported to a couple of decimal places, so compare with a tolerance rather
# than demanding an exact float.
RENDERED_COVERAGE_RATIO_TOLERANCE = 0.01

CONTRACT_DOCUMENT = "docs/normalized-source-format.md"
DEFAULT_REMEDIATION = f"Correct the normalized record so it matches {CONTRACT_DOCUMENT}."

_SECTION_HEADING_RE = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
# Facet headings live below the record's own level-two sections, so the presence check
# accepts a heading at any depth.
_ANY_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Violation:
    """One contract breach, shaped so lint findings and JSON reports carry the same facts."""

    code: str
    message: str
    field: str | None = None
    expected: str | None = None
    actual: str | None = None
    remediation: str = DEFAULT_REMEDIATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "remediation": self.remediation,
        }


def safe_source_id(source_id: str) -> str:
    """Filesystem-safe form of a manifest id, per the contract's File Path rule."""
    value = source_id.lower().replace(":", "__colon__")
    value = re.sub(r"[/\s]+", "-", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    value = value.replace("__colon__", "--")
    value = value.replace("-.", ".").strip("-")
    return value or "source"


def expected_record_path(normalized_root: Path, source_id: str) -> Path:
    """Where a record for ``source_id`` must live for tooling to resolve it by id."""
    return normalized_root / f"{safe_source_id(source_id)}.md"


def split_record(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Split a record into frontmatter and body, or explain why it cannot be read."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, normalized, "missing YAML frontmatter"
    # Close on the first line that is exactly `---` so a horizontal rule or a
    # `---`-prefixed value inside the block does not truncate parsing early.
    closing_index = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing_index is None:
        return None, normalized, "unterminated YAML frontmatter"
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing_index]))
    except yaml.YAMLError as exc:
        return None, "\n".join(lines[closing_index + 1 :]), f"invalid YAML frontmatter: {exc}"
    body = "\n".join(lines[closing_index + 1 :])
    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        return None, body, "frontmatter must be a mapping"
    return frontmatter, body, None


def effective_format_version(frontmatter: dict[str, Any]) -> int | None:
    """Contract version a record effectively claims.

    An absent field reads as ``LEGACY_NORMALIZED_FORMAT_VERSION``, which only this
    package's own normalizer may claim. ``None`` means the field is present but is not
    an integer, which no record may claim.
    """
    if "normalized_format" not in frontmatter:
        return LEGACY_NORMALIZED_FORMAT_VERSION
    value = frontmatter.get("normalized_format")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def normalizer_name(frontmatter: dict[str, Any]) -> str | None:
    normalizer = frontmatter.get("normalizer")
    if not isinstance(normalizer, dict):
        return None
    name = normalizer.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def is_native_record(frontmatter: dict[str, Any]) -> bool:
    """True when this package's own normalizer produced the record."""
    return normalizer_name(frontmatter) in NATIVE_NORMALIZER_NAMES


def declares_foreign_normalizer(frontmatter: dict[str, Any]) -> bool:
    """True when a record names a producing tool that is not this package's.

    Narrower than ``not is_native_record(...)``: a record that names no tool at all is
    not claiming foreign origin, it is failing to identify itself. Lint uses this to
    decide which records it holds to the contract automatically, so that a legacy or
    unidentified record is not newly policed by a check it predates. ``normalize
    verify`` validates records regardless of what they claim, so the stricter reading
    stays available on demand.
    """
    name = normalizer_name(frontmatter)
    return name is not None and name not in NATIVE_NORMALIZER_NAMES


def _is_date_like(value: Any) -> bool:
    # PyYAML resolves unquoted `2026-05-09` to a date, quoted to a string; both are
    # written by conforming tools, so the contract accepts either spelling.
    if isinstance(value, (date, datetime)):
        return True
    return isinstance(value, str) and bool(value.strip())


def _unknown_keys(mapping: dict[str, Any], allowed: frozenset[str]) -> list[str]:
    """Keys outside the contract, ignoring the `x-` prefix reserved for experiments."""
    return sorted(key for key in mapping if key not in allowed and not str(key).startswith("x-"))


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def _invalid(message: str, *, field: str, expected: str, actual: Any) -> Violation:
    return Violation(
        FRONTMATTER_INVALID,
        message,
        field=field,
        expected=expected,
        actual="absent" if actual is None else repr(actual),
    )


def check_frontmatter(frontmatter: dict[str, Any]) -> list[Violation]:
    """Required fields exist and carry the types the contract documents."""
    violations: list[Violation] = []

    record_type = frontmatter.get("type")
    if record_type != RECORD_TYPE:
        violations.append(
            _invalid(
                f"Record type must be `{RECORD_TYPE}`.",
                field="type",
                expected=RECORD_TYPE,
                actual=record_type,
            )
        )

    for name in ("source_id", "source_kind", "manifest_path"):
        value = frontmatter.get(name)
        if not isinstance(value, str) or not value.strip():
            violations.append(
                _invalid(
                    f"`{name}` must be a non-empty string.",
                    field=name,
                    expected="non-empty string",
                    actual=value,
                )
            )

    status = frontmatter.get("status")
    if status not in ALLOWED_STATUSES:
        violations.append(
            _invalid(
                "`status` must be one of the documented lifecycle values.",
                field="status",
                expected=", ".join(sorted(ALLOWED_STATUSES)),
                actual=status,
            )
        )

    evidence_usable = frontmatter.get("evidence_usable")
    if not isinstance(evidence_usable, bool):
        # Required rather than defaulted: coverage policies reject unusable evidence,
        # so an omission that read as usable would fail open.
        violations.append(
            _invalid(
                "`evidence_usable` must be an explicit boolean.",
                field="evidence_usable",
                expected="true or false",
                actual=evidence_usable,
            )
        )

    for name in ("created", "updated"):
        value = frontmatter.get(name)
        if not _is_date_like(value):
            violations.append(
                _invalid(
                    f"`{name}` must be a `YYYY-MM-DD` date.",
                    field=name,
                    expected="YYYY-MM-DD date",
                    actual=value,
                )
            )

    for name in ("raw_paths", "parse_warnings"):
        if _string_list(frontmatter.get(name)) is None:
            violations.append(
                _invalid(
                    f"`{name}` must be a list of strings.",
                    field=name,
                    expected="list of strings",
                    actual=frontmatter.get(name),
                )
            )

    violations.extend(_check_normalizer(frontmatter))
    return violations


def _check_normalizer(frontmatter: dict[str, Any]) -> list[Violation]:
    normalizer = frontmatter.get("normalizer")
    if not isinstance(normalizer, dict):
        return [
            _invalid(
                "`normalizer` must be a mapping naming the tool that produced the record.",
                field="normalizer",
                expected="mapping with name and version",
                actual=normalizer,
            )
        ]

    violations: list[Violation] = []
    name = normalizer.get("name")
    if not isinstance(name, str) or not name.strip():
        violations.append(
            _invalid(
                "`normalizer.name` must name the producing tool.",
                field="normalizer.name",
                expected="non-empty string",
                actual=name,
            )
        )
    version = normalizer.get("version")
    # An external tool versions itself however it likes (`3`, `"1.4.0"`); the package
    # stores the value and never reads it as a version of itself.
    version_ok = (isinstance(version, int) and not isinstance(version, bool)) or (
        isinstance(version, str) and bool(version.strip())
    )
    if not version_ok:
        violations.append(
            _invalid(
                "`normalizer.version` must be an integer or a non-empty version string.",
                field="normalizer.version",
                expected="integer or non-empty string",
                actual=version,
            )
        )
    return violations


def check_format_version(frontmatter: dict[str, Any]) -> list[Violation]:
    """The record declares a contract version this package accepts."""
    version = effective_format_version(frontmatter)
    if version in ACCEPTED_NORMALIZED_FORMATS:
        return []
    if version == LEGACY_NORMALIZED_FORMAT_VERSION and is_native_record(frontmatter):
        # Legacy record from this package's own normalizer; re-normalization backfills
        # the field. The allowance exists to carry those forward, not to let a foreign
        # record decline to state which contract it targets.
        return []

    accepted = ", ".join(str(value) for value in sorted(ACCEPTED_NORMALIZED_FORMATS))
    if "normalized_format" not in frontmatter:
        return [
            Violation(
                FORMAT_VERSION_UNSUPPORTED,
                "Record written outside this package must declare `normalized_format`.",
                field="normalized_format",
                expected=str(NORMALIZED_FORMAT_VERSION),
                actual="absent",
                remediation=(
                    f"Add `normalized_format: {NORMALIZED_FORMAT_VERSION}` to the record, "
                    f"as documented in {CONTRACT_DOCUMENT}."
                ),
            )
        ]
    return [
        Violation(
            FORMAT_VERSION_UNSUPPORTED,
            "Record declares a contract version this package does not accept.",
            field="normalized_format",
            expected=accepted,
            actual=repr(frontmatter.get("normalized_format")),
            remediation=(
                "Write the record against an accepted contract version, or upgrade "
                "evidence-wiki to one that accepts this version."
            ),
        )
    ]


def section_order(body: str) -> list[str]:
    """Level-two headings in document order, as written."""
    return [match.group("title").strip() for match in _SECTION_HEADING_RE.finditer(body)]


def check_sections(body: str) -> list[Violation]:
    """The documented sections all exist, in the documented order."""
    # Extracted text legitimately contains its own level-two headings — a LaTeX
    # `\section{Links}` renders as `## Links` inside Extracted Text — so the required
    # sections are matched as an ordered subsequence rather than by position. A heading
    # that duplicates a section name early is skipped as body content; only a record
    # that cannot supply all eight in order is in breach.
    written = section_order(body)
    headings = [_normalize_heading(title) for title in written]
    required = [_normalize_heading(name) for name in REQUIRED_SECTIONS]

    present = set(headings)
    missing = [name for name, key in zip(REQUIRED_SECTIONS, required, strict=True) if key not in present]
    if missing:
        return [
            Violation(
                SECTIONS_INVALID,
                f"Record is missing required section(s): {', '.join(missing)}.",
                field="sections",
                expected=" -> ".join(REQUIRED_SECTIONS),
                actual=", ".join(written) or "no level-two headings",
                remediation=(
                    "Add every documented section, in order, writing `None extracted.` or "
                    "`None recorded.` when a section has no content."
                ),
            )
        ]

    matched = 0
    for heading in headings:
        if matched < len(required) and heading == required[matched]:
            matched += 1
    if matched != len(required):
        return [
            Violation(
                SECTIONS_INVALID,
                f"Required sections are present but out of order, starting at `{REQUIRED_SECTIONS[matched]}`.",
                field="sections",
                expected=" -> ".join(REQUIRED_SECTIONS),
                actual=" -> ".join(written),
                remediation="Reorder the record's sections to match the documented order.",
            )
        ]
    return []


def _normalize_heading(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip().casefold()


def section_body(body: str, heading: str) -> str | None:
    """Text under the first level-two heading matching ``heading``."""
    target = _normalize_heading(heading)
    matches = list(_SECTION_HEADING_RE.finditer(body))
    for index, match in enumerate(matches):
        if _normalize_heading(match.group("title")) != target:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[match.end() : end]
    return None


def check_parse_warnings(frontmatter: dict[str, Any], body: str) -> list[Violation]:
    """Every warning the frontmatter declares is also readable by a human."""
    warnings = _string_list(frontmatter.get("parse_warnings"))
    if not warnings:
        # Empty is unconstrained: the contract lets a clean record write any
        # placeholder ("None.", "None recorded.") under the heading.
        return []
    section = section_body(body, PARSE_WARNINGS_SECTION)
    if section is None:
        # A missing section is reported once, by the section check.
        return []
    hidden = [warning for warning in warnings if warning.strip() and warning.strip() not in section]
    if not hidden:
        return []
    return [
        Violation(
            WARNINGS_INCONSISTENT,
            f"{len(hidden)} frontmatter parse warning(s) are missing from the `{PARSE_WARNINGS_SECTION}` section.",
            field="parse_warnings",
            expected=f"every frontmatter warning restated under `## {PARSE_WARNINGS_SECTION}`",
            actual=hidden[0],
            remediation=(
                f"Restate each `parse_warnings` entry under `## {PARSE_WARNINGS_SECTION}` so a "
                "reviewer sees the same extraction caveats as the tooling."
            ),
        )
    ]


def _coverage_violation(message: str, *, field: str, expected: str, actual: Any) -> Violation:
    return Violation(
        RENDERED_COVERAGE_INVALID,
        message,
        field=field,
        expected=expected,
        actual="absent" if actual is None else repr(actual),
        remediation=(
            "Correct the record's `rendered_coverage` counts so they describe the body that "
            f"was actually rendered, as documented in {CONTRACT_DOCUMENT}."
        ),
    )


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _headings_in(body: str) -> set[str]:
    return {_normalize_heading(match.group("title")) for match in _ANY_HEADING_RE.finditer(body)}


def validate_rendered_coverage_block(block: Any, body: str, *, field: str = "rendered_coverage") -> list[Violation]:
    """Check a `rendered_coverage` declaration against the body it describes.

    The counts are the renderer's own claim — this package cannot recompute them for a
    foreign renderer whose capping rules it does not know (that was the point of having
    the renderer stamp them). What it can check is that the claim is internally coherent
    and that every section it names is really in the body, so a host reading a coverage
    ratio is reading something about this record rather than a leftover from another.
    """
    if not isinstance(block, dict):
        return [_coverage_violation("`rendered_coverage` must be a mapping.", field=field,
                                    expected="mapping", actual=block)]

    violations: list[Violation] = []
    unknown = _unknown_keys(block, RENDERED_COVERAGE_KEYS)
    if unknown:
        violations.append(
            _coverage_violation(
                f"`rendered_coverage` has unknown keys: {', '.join(unknown)}.",
                field=field,
                expected=", ".join(sorted(RENDERED_COVERAGE_KEYS)),
                actual=", ".join(unknown),
            )
        )

    total = _non_negative_int(block.get("total_values"))
    rendered = _non_negative_int(block.get("rendered_values"))
    for name, value in (("total_values", total), ("rendered_values", rendered)):
        if value is None:
            violations.append(
                _coverage_violation(
                    f"`rendered_coverage.{name}` must be a non-negative integer.",
                    field=f"{field}.{name}",
                    expected="non-negative integer",
                    actual=block.get(name),
                )
            )

    if total is not None and rendered is not None:
        if rendered > total:
            violations.append(
                _coverage_violation(
                    "`rendered_coverage` renders more values than it considered.",
                    field=f"{field}.rendered_values",
                    expected=f"<= total_values ({total})",
                    actual=rendered,
                )
            )
        else:
            violations.extend(_check_ratio(block, total, rendered, field))

    violations.extend(_check_coverage_sections(block, body, total, rendered, field))
    return violations


def _check_ratio(block: dict[str, Any], total: int, rendered: int, field: str) -> list[Violation]:
    ratio = block.get("ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)):
        return [
            _coverage_violation(
                "`rendered_coverage.ratio` must be a number between 0 and 1.",
                field=f"{field}.ratio",
                expected="number in [0, 1]",
                actual=ratio,
            )
        ]
    # Nothing to lose is full coverage: a renderer that considered no values has not
    # dropped any, so the ratio is 1.0 rather than an undefined division.
    expected_ratio = 1.0 if total == 0 else rendered / total
    if abs(float(ratio) - expected_ratio) > RENDERED_COVERAGE_RATIO_TOLERANCE:
        return [
            _coverage_violation(
                "`rendered_coverage.ratio` does not match its own counts.",
                field=f"{field}.ratio",
                expected=f"{expected_ratio:.4f} (rendered_values / total_values)",
                actual=ratio,
            )
        ]
    return []


def _check_coverage_sections(
    block: dict[str, Any],
    body: str,
    total: int | None,
    rendered: int | None,
    field: str,
) -> list[Violation]:
    sections = block.get("sections")
    if sections is None:
        return []
    if not isinstance(sections, list):
        return [
            _coverage_violation(
                "`rendered_coverage.sections` must be a list.",
                field=f"{field}.sections",
                expected="list of section mappings",
                actual=sections,
            )
        ]

    violations: list[Violation] = []
    headings = _headings_in(body)
    section_total = 0
    section_rendered = 0
    for index, section in enumerate(sections):
        label = f"{field}.sections[{index}]"
        if not isinstance(section, dict):
            violations.append(
                _coverage_violation(f"{label} must be a mapping.", field=label,
                                    expected="mapping", actual=section)
            )
            continue
        unknown = _unknown_keys(section, RENDERED_COVERAGE_SECTION_KEYS)
        if unknown:
            violations.append(
                _coverage_violation(
                    f"{label} has unknown keys: {', '.join(unknown)}.",
                    field=label,
                    expected=", ".join(sorted(RENDERED_COVERAGE_SECTION_KEYS)),
                    actual=", ".join(unknown),
                )
            )
        section_total_value = _non_negative_int(section.get("total"))
        section_rendered_value = _non_negative_int(section.get("rendered"))
        heading = section.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            violations.append(
                _coverage_violation(f"{label}.heading must name a section of the body.",
                                    field=f"{label}.heading", expected="non-empty string", actual=heading)
            )
        elif section_rendered_value and _normalize_heading(heading) not in headings:
            # Claiming rendered values under a heading the body does not have describes
            # some other rendering, and a reader would trust it for this one. A section
            # that rendered nothing is exempt: a facet dropped in full has no heading to
            # point at, and its entry exists precisely to account for that loss.
            violations.append(
                _coverage_violation(
                    f"{label}.heading claims rendered values but is not a heading in the record body.",
                    field=f"{label}.heading",
                    expected="a heading present in the body",
                    actual=heading,
                )
            )
        for name, value in (("total", section_total_value), ("rendered", section_rendered_value)):
            if value is None:
                violations.append(
                    _coverage_violation(
                        f"{label}.{name} must be a non-negative integer.",
                        field=f"{label}.{name}",
                        expected="non-negative integer",
                        actual=section.get(name),
                    )
                )
        if section_total_value is not None and section_rendered_value is not None:
            if section_rendered_value > section_total_value:
                violations.append(
                    _coverage_violation(
                        f"{label} renders more values than it considered.",
                        field=f"{label}.rendered",
                        expected=f"<= total ({section_total_value})",
                        actual=section_rendered_value,
                    )
                )
            section_total += section_total_value
            section_rendered += section_rendered_value
        note = section.get("note")
        if note is not None and (not isinstance(note, str) or not note.strip()):
            violations.append(
                _coverage_violation(f"{label}.note must be a non-empty string when present.",
                                    field=f"{label}.note", expected="non-empty string", actual=note)
            )

    # Sections describe part of the payload, never more than the whole of it.
    if total is not None and section_total > total:
        violations.append(
            _coverage_violation(
                "`rendered_coverage.sections` account for more values than the record considered.",
                field=f"{field}.sections",
                expected=f"section totals <= total_values ({total})",
                actual=section_total,
            )
        )
    if rendered is not None and section_rendered > rendered:
        violations.append(
            _coverage_violation(
                "`rendered_coverage.sections` render more values than the record rendered.",
                field=f"{field}.sections",
                expected=f"section rendered <= rendered_values ({rendered})",
                actual=section_rendered,
            )
        )
    return violations


def check_rendered_coverage(frontmatter: dict[str, Any], body: str) -> list[Violation]:
    """Records this package rendered through an adapter must declare their coverage."""
    block = frontmatter.get("rendered_coverage")
    if block is None:
        if frontmatter.get("extraction_method") == ADAPTER_EXTRACTION_METHOD:
            return [
                _coverage_violation(
                    "An adapter-rendered record must declare `rendered_coverage`.",
                    field="rendered_coverage",
                    expected="rendered_coverage block",
                    actual=None,
                )
            ]
        return []
    return validate_rendered_coverage_block(block, body)


def check_manifest_agreement(
    path: Path,
    frontmatter: dict[str, Any],
    *,
    manifest_by_id: dict[str, dict[str, Any]],
    normalized_root: Path,
) -> list[Violation]:
    """The record agrees with the manifest about what evidence it normalizes."""
    source_id = frontmatter.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        # Reported by the frontmatter check; nothing here can be resolved without it.
        return []
    source_id = source_id.strip()
    violations: list[Violation] = []

    expected_path = expected_record_path(normalized_root, source_id)
    if _resolved(path) != _resolved(expected_path):
        violations.append(
            Violation(
                MANIFEST_MISMATCH,
                "Record does not live at the path its `source_id` resolves to.",
                field="source_id",
                expected=expected_path.as_posix(),
                actual=path.as_posix(),
                remediation=(
                    "Move the record to the path its source id resolves to, or correct "
                    "`source_id`, so tooling can find it by id."
                ),
            )
        )

    record = manifest_by_id.get(source_id)
    if record is None:
        if not source_id.startswith("manual:"):
            violations.append(
                Violation(
                    MANIFEST_MISMATCH,
                    f"Record cites a source id that is not in the manifest: {source_id}.",
                    field="source_id",
                    expected="a manifest source id, or a `manual:` id",
                    actual=source_id,
                    remediation=(
                        "Run scripts/source_inventory.py --report so the source is inventoried, "
                        "or correct the record's `source_id`."
                    ),
                )
            )
        return violations

    violations.extend(_check_raw_paths(frontmatter, record))
    violations.extend(_check_fingerprint(frontmatter, record))
    return violations


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _check_raw_paths(frontmatter: dict[str, Any], record: dict[str, Any]) -> list[Violation]:
    declared = _string_list(frontmatter.get("raw_paths"))
    if declared is None:
        # Reported by the frontmatter check.
        return []
    manifest_paths = _string_list(record.get("raw_paths")) or []
    # A record may list more than the manifest does — LaTeX includes and the paired PDF
    # are evidence too — but every path the manifest names must be accounted for, or the
    # record is describing different bytes than the manifest recorded.
    missing = [value for value in manifest_paths if value not in declared]
    if not missing:
        return []
    return [
        Violation(
            MANIFEST_MISMATCH,
            f"Record omits raw path(s) the manifest records for this source: {', '.join(missing)}.",
            field="raw_paths",
            expected=", ".join(manifest_paths),
            actual=", ".join(declared) or "none",
            remediation="List every manifest raw path for the source in the record's `raw_paths`.",
        )
    ]


def _check_fingerprint(frontmatter: dict[str, Any], record: dict[str, Any]) -> list[Violation]:
    declared = frontmatter.get("raw_fingerprint")
    manifest_fingerprint = record.get("raw_fingerprint")
    if not isinstance(declared, str) or not isinstance(manifest_fingerprint, str):
        # Links and codebase records carry no fingerprint, and a record may predate the
        # signal; only a disagreement between two present values is a violation.
        return []
    if declared == manifest_fingerprint:
        return []
    return [
        Violation(
            MANIFEST_MISMATCH,
            "Record was normalized from different raw bytes than the manifest now records.",
            field="raw_fingerprint",
            expected=manifest_fingerprint,
            actual=declared,
            remediation="Re-normalize the source so the record matches the delivered raw evidence.",
        )
    ]


def validate_document(
    path: Path,
    frontmatter: dict[str, Any],
    body: str,
    *,
    manifest_by_id: dict[str, dict[str, Any]],
    normalized_root: Path,
) -> list[Violation]:
    """Validate an already-parsed record, for callers that read it themselves."""
    violations = check_frontmatter(frontmatter)
    violations.extend(check_format_version(frontmatter))
    violations.extend(check_sections(body))
    violations.extend(check_parse_warnings(frontmatter, body))
    violations.extend(check_rendered_coverage(frontmatter, body))
    violations.extend(
        check_manifest_agreement(
            path,
            frontmatter,
            manifest_by_id=manifest_by_id,
            normalized_root=normalized_root,
        )
    )
    return violations


def validate_record(
    path: Path,
    *,
    manifest_by_id: dict[str, dict[str, Any]],
    normalized_root: Path,
) -> list[Violation]:
    """Validate one normalized record file against the contract."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [
            Violation(
                FRONTMATTER_MISSING,
                f"Normalized record cannot be read: {exc}",
                field="file",
                expected="readable UTF-8 Markdown record",
                actual=path.as_posix(),
                remediation="Restore the record as readable UTF-8 Markdown, or re-normalize the source.",
            )
        ]

    frontmatter, body, error = split_record(text)
    if frontmatter is None:
        return [
            Violation(
                FRONTMATTER_MISSING,
                f"Normalized record has no usable YAML frontmatter: {error}",
                field="frontmatter",
                expected="YAML frontmatter mapping delimited by `---`",
                actual=error,
                remediation=(
                    "Start the record with a `---` delimited YAML mapping, as documented in "
                    f"{CONTRACT_DOCUMENT}."
                ),
            )
        ]

    return validate_document(
        path,
        frontmatter,
        body,
        manifest_by_id=manifest_by_id,
        normalized_root=normalized_root,
    )
