#!/usr/bin/env python3
"""Transport for external normalizer adapters configured in ``research.yml``.

An adapter turns evidence this package cannot read — a structured API payload, an
instrument dump — into the pieces of a normalized record. It never writes the record:
it returns content, and ``normalize_sources.py`` renders and writes that content
exactly as it writes its own, so frontmatter, section order, fingerprints, and content
hashing stay owned by one writer.

Everything here is fail-closed. A configured adapter is authorized code, but its output
is untrusted input: a run that cannot produce a result it fully understands raises
rather than writing a partial or stub record, because a record that exists is treated
as evidence by the reopen gate, by grounding, and by lint. Silence would be worse than
a failed action — a failed action is reported and exits non-zero.

The package grants the adapter nothing beyond the request on stdin. It is executed with
``shell=False`` from an argv list the operator wrote, with a bounded timeout, and its
stdout is parsed as exactly one JSON document. Whatever else it reaches, it reaches on
its own authority.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
# The contract module owns the `rendered_coverage` and structured-view shapes, so an
# adapter response and a written record are judged against one definition.
import _normalized_contract  # noqa: E402

REQUEST_SCHEMA_VERSION = "1.0"
REQUEST_DOCUMENT_TYPE = "normalizer_adapter_request"
RESULT_SCHEMA_VERSION = "1.0"
RESULT_DOCUMENT_TYPE = "normalizer_adapter_result"

EXTRACTION_METHOD = "adapter"

STATUS_CONTENT_EXTRACTED = "content_extracted"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
RESULT_STATUSES = (STATUS_CONTENT_EXTRACTED, STATUS_PARTIAL, STATUS_FAILED)

RESULT_KEYS = frozenset(
    {
        "schema_version",
        "document_type",
        "adapter",
        "status",
        "title",
        "abstract",
        "outline",
        "body_markdown",
        "rendered_coverage",
        "structured",
        "warnings",
        "detail",
    }
)

# Adapter output is bounded for the same reason PDF extraction is: an adapter that
# streams without end would otherwise consume the run.
MAX_RESULT_BYTES = 20 * 1024 * 1024
# Enough of the adapter's own stderr to diagnose a failure, not enough to bury a report.
MAX_STDERR_CHARS = 2000
MAX_WARNINGS = 100
MAX_OUTLINE_ENTRIES = 200

# A level-two heading in the body would collide with the record's own section
# structure: the contract's eight sections are `##`, and a body heading named like one
# of them would capture readers, section anchors, and the parse-warning check. Facet
# headings belong at `###` or deeper.
BODY_SECTION_HEADING_RE = re.compile(r"^##[ \t]+\S", re.MULTILINE)


class AdapterError(RuntimeError):
    """An adapter run that cannot yield a record. Callers report it as a failed action."""


@dataclass(frozen=True)
class AdapterResult:
    """Validated content returned by an adapter."""

    name: str
    version: str
    status: str
    title: str | None
    abstract: str | None
    outline: tuple[tuple[int, str], ...]
    body_markdown: str
    warnings: tuple[str, ...]
    rendered_coverage: dict[str, Any] | None
    # The complete, uncapped structured rendering of the source, when the adapter has
    # one to offer. Written beside the record as its structured-view sidecar and bound
    # to it by hash; `None` simply means this source cannot be anchored.
    structured: dict[str, Any] | None = None


def build_request(
    project_root: Path,
    record: dict[str, Any],
    *,
    raw_paths: list[str],
    normalized_format: int,
) -> dict[str, Any]:
    """The JSON document handed to an adapter on stdin."""
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "document_type": REQUEST_DOCUMENT_TYPE,
        "normalized_format": normalized_format,
        "project_root": str(project_root),
        "manifest_record": record,
        "raw_paths": list(raw_paths),
    }


def _fail(source_id: str, adapter_name: str, detail: str) -> AdapterError:
    return AdapterError(f"{source_id}: normalizer adapter '{adapter_name}': {detail}")


def _bounded_stderr(text: str) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= MAX_STDERR_CHARS:
        return stripped
    return stripped[:MAX_STDERR_CHARS] + " …(truncated)"


def run_adapter(
    adapter: Any,
    request: dict[str, Any],
    *,
    source_id: str,
    project_root: Path,
) -> AdapterResult:
    """Execute one adapter and return the content it produced.

    ``adapter`` is a validated ``_normalization_config.NormalizerAdapter``.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv from reviewed research.yml, shell=False
            list(adapter.command),
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=adapter.timeout_seconds,
            cwd=str(project_root),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise _fail(
            source_id,
            adapter.name,
            f"timed out after {adapter.timeout_seconds}s",
        ) from None
    except OSError as exc:
        raise _fail(source_id, adapter.name, f"could not be executed: {exc}") from exc

    stderr = _bounded_stderr(completed.stderr)
    if completed.returncode != 0:
        detail = f"exited {completed.returncode}"
        raise _fail(source_id, adapter.name, f"{detail}: {stderr}" if stderr else detail)

    payload = parse_result_document(completed.stdout, source_id=source_id, adapter_name=adapter.name)
    return validate_result(payload, adapter=adapter, source_id=source_id, stderr=stderr)


def parse_result_document(stdout: str, *, source_id: str, adapter_name: str) -> dict[str, Any]:
    """Parse stdout as exactly one JSON document.

    Anything before or after that document is a protocol violation rather than something
    to scan past: an adapter that also logs to stdout would otherwise have its diagnostics
    silently folded into, or mistaken for, evidence.
    """
    if len(stdout.encode("utf-8", errors="replace")) > MAX_RESULT_BYTES:
        raise _fail(source_id, adapter_name, f"returned more than {MAX_RESULT_BYTES} bytes on stdout")

    text = stdout.strip()
    if not text:
        raise _fail(source_id, adapter_name, "returned nothing on stdout")

    decoder = json.JSONDecoder()
    try:
        payload, consumed = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise _fail(source_id, adapter_name, f"returned output that is not JSON: {exc}") from exc
    if consumed != len(text):
        raise _fail(
            source_id,
            adapter_name,
            "returned more than one document on stdout; diagnostics belong on stderr",
        )
    if not isinstance(payload, dict):
        raise _fail(source_id, adapter_name, "returned a JSON value that is not an object")
    return payload


def _require_optional_text(payload: dict[str, Any], key: str, source_id: str, adapter_name: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(source_id, adapter_name, f"returned a non-string `{key}`")
    stripped = value.strip()
    return stripped or None


def _validate_outline(value: Any, source_id: str, adapter_name: str) -> tuple[tuple[int, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _fail(source_id, adapter_name, "returned a non-list `outline`")
    if len(value) > MAX_OUTLINE_ENTRIES:
        raise _fail(source_id, adapter_name, f"returned more than {MAX_OUTLINE_ENTRIES} outline entries")
    entries: list[tuple[int, str]] = []
    for entry in value:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise _fail(source_id, adapter_name, "returned an outline entry that is not [level, text]")
        level, text = entry
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise _fail(source_id, adapter_name, f"returned an outline level outside 1-6: {level!r}")
        if not isinstance(text, str) or not text.strip():
            raise _fail(source_id, adapter_name, "returned an outline entry with no text")
        entries.append((level, text.strip()))
    return tuple(entries)


def _validate_warnings(value: Any, source_id: str, adapter_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _fail(source_id, adapter_name, "returned a non-list `warnings`")
    if len(value) > MAX_WARNINGS:
        raise _fail(source_id, adapter_name, f"returned more than {MAX_WARNINGS} warnings")
    warnings: list[str] = []
    for warning in value:
        if not isinstance(warning, str):
            raise _fail(source_id, adapter_name, "returned a non-string warning")
        stripped = warning.strip()
        if stripped:
            warnings.append(stripped)
    return tuple(warnings)


def validate_result(
    payload: dict[str, Any],
    *,
    adapter: Any,
    source_id: str,
    stderr: str = "",
) -> AdapterResult:
    """Check an adapter's document against the protocol before any of it is written."""
    adapter_name = adapter.name

    unknown = sorted(key for key in payload if key not in RESULT_KEYS and not str(key).startswith("x-"))
    if unknown:
        raise _fail(source_id, adapter_name, f"returned unknown keys: {', '.join(unknown)}")

    if payload.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise _fail(
            source_id,
            adapter_name,
            f"returned schema_version {payload.get('schema_version')!r}; expected {RESULT_SCHEMA_VERSION!r}",
        )
    if payload.get("document_type") != RESULT_DOCUMENT_TYPE:
        raise _fail(
            source_id,
            adapter_name,
            f"returned document_type {payload.get('document_type')!r}; expected {RESULT_DOCUMENT_TYPE!r}",
        )

    declared = payload.get("adapter")
    if not isinstance(declared, dict):
        raise _fail(source_id, adapter_name, "returned no `adapter` identity object")
    # The record will carry this identity as its producer, and staleness is decided by
    # comparing it to the configured one. An adapter that reports something else is
    # either misconfigured or not the tool the workspace authorized.
    if declared.get("name") != adapter.name or str(declared.get("version")) != adapter.version:
        raise _fail(
            source_id,
            adapter_name,
            (
                f"reported identity {declared.get('name')!r}/{declared.get('version')!r}, "
                f"but research.yml authorized {adapter.name!r}/{adapter.version!r}"
            ),
        )

    status = payload.get("status")
    if status not in RESULT_STATUSES:
        raise _fail(
            source_id,
            adapter_name,
            f"returned status {status!r}; expected one of: {', '.join(RESULT_STATUSES)}",
        )
    if status == STATUS_FAILED:
        detail = payload.get("detail")
        reported = detail.strip() if isinstance(detail, str) and detail.strip() else "no detail reported"
        raise _fail(source_id, adapter_name, f"reported failure: {reported}")

    body = payload.get("body_markdown")
    if not isinstance(body, str) or not body.strip():
        raise _fail(source_id, adapter_name, "returned no `body_markdown` content")
    if BODY_SECTION_HEADING_RE.search(body):
        raise _fail(
            source_id,
            adapter_name,
            (
                "returned a level-two heading in `body_markdown`, which would collide with the "
                "record's own sections; use `###` or deeper for facet headings"
            ),
        )

    # Checked here, against the body the adapter just returned, so a bad declaration
    # fails the action rather than reaching a record. The contract module owns the shape
    # so the adapter response and the written record are judged by one code path.
    coverage = payload.get("rendered_coverage")
    if coverage is None:
        raise _fail(
            source_id,
            adapter_name,
            (
                "returned no `rendered_coverage`; a rendering of structured evidence must "
                "state how much of the payload it rendered, so a host can see when its caps "
                "are eating its quotes"
            ),
        )
    coverage_violations = _normalized_contract.validate_rendered_coverage_block(coverage, body)
    if coverage_violations:
        first = coverage_violations[0]
        raise _fail(source_id, adapter_name, f"returned an invalid `rendered_coverage`: {first.message}")

    # Optional where `rendered_coverage` is required: an adapter whose source has no
    # addressable structure has nothing to anchor against, which is a fact about the
    # evidence rather than a protocol breach. Offered, it is checked here so a payload
    # that could never be written is refused before anything is. The contract module owns
    # the shape so the adapter response and the written record are judged by one code
    # path — `field="structured"` names the response's key rather than the record's.
    # Size needs no separate bound: `MAX_RESULT_BYTES` already caps the whole document
    # this rides in.
    structured = payload.get("structured")
    if structured is not None:
        structured_violations = _normalized_contract.validate_structured_payload(structured, field="structured")
        if structured_violations:
            first = structured_violations[0]
            raise _fail(source_id, adapter_name, f"returned an invalid `structured`: {first.message}")

    warnings = _validate_warnings(payload.get("warnings"), source_id, adapter_name)
    if stderr:
        warnings = (*warnings, f"adapter stderr: {stderr}")

    return AdapterResult(
        name=adapter.name,
        version=adapter.version,
        status=status,
        title=_require_optional_text(payload, "title", source_id, adapter_name),
        abstract=_require_optional_text(payload, "abstract", source_id, adapter_name),
        outline=_validate_outline(payload.get("outline"), source_id, adapter_name),
        body_markdown=body.strip(),
        warnings=warnings,
        rendered_coverage=coverage,
        structured=structured,
    )
