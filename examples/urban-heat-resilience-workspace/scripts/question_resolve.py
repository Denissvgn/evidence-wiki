#!/usr/bin/env python3
"""Resolve question task records under the stable question claim lock.

Question resolution is the machine path for moving a claimed question from
``in_progress`` to one of the terminal lifecycle states used by unattended
research runs:

- ``answer --slug SLUG --agent-id ID --answer-page PATH --source-id ID``
  records a real workspace-relative answer page under the configured wiki root,
  cited manifest ``source_ids``, and optional verification fields. At least one
  ``--source-id`` is required unless ``--allow-uncited`` is explicit. When
  ``--require-coverage`` is supplied, the selected coverage manifest must
  evaluate to ``pass`` before the question can be marked ``answered``.
- ``block --slug SLUG --agent-id ID --blocked-reason TEXT`` records the reason
  current evidence is insufficient. When ``--request-id`` is supplied, the
  request must exist in ``sources/source-requests.jsonl`` and reference the
  question slug.
- ``defer`` and ``reject`` record a ``resolution_reason``.
- ``approve --slug SLUG --reviewer REVIEWER`` records human-review approval for
  an answer that reached ``human_review`` because coverage policies required
  manual sign-off. It accepts every policy still pending in one call.
- ``review --slug SLUG --policy POLICY --verdict accepted|rejected --reviewed-by
  PRINCIPAL [--review-ref REF] [--note TEXT]`` records one per-policy review,
  which lets a host collect the review in its own approval queue and point at it
  with the opaque ``--review-ref``. Entries append to ``human_reviews``; the
  question becomes ``answered`` once every declared policy is accepted, and a
  rejection returns it to ``open``. ``--reviewed-by`` is a recorded principal on
  the same trust model as ``--reviewer``: these scripts authenticate nobody, and
  the audit trail is the frontmatter entry plus ``log.md``.
- ``reopen --slug SLUG --agent-id ID --source-id MANIFEST_ID`` moves a
  ``blocked`` question back to ``open`` once the delivered evidence is in the
  manifest and has a normalized record, drops ``blocked_reason``, and adds the
  fulfilled source id(s) so ``research-answer`` can pick the question up. It is
  the deterministic counterpart to ``block`` and the only verb that operates on a
  terminal status; it requires no claim because a blocked question is unclaimed.

By default, the other verbs require the question to be claimed by the same agent
id. ``--allow-unclaimed`` lets an orchestrator or single-agent workflow resolve
an open or otherwise unheld question explicitly, but terminal question statuses
are never rewritten by ``answer``/``block``/``defer``/``reject`` (only ``reopen``
transitions ``blocked`` back to ``open``).

Exit codes:

- ``0``: resolution applied.
- ``2``: invalid usage, unknown slug, invalid page/request/source, unclaimed
  question without ``--allow-unclaimed``, or a status that cannot be resolved.
- ``3``: claim conflict (held by another agent).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_INVALID = 2
EXIT_CONFLICT = 3
CONFIDENCE_VALUES = ("high", "medium", "low")
EVIDENCE_STRENGTH_VALUES = ("corroborated", "single_source", "contested")
TERMINAL_STATUSES = ("answered", "human_review", "blocked", "deferred", "rejected")
REVIEW_VERDICT_ACCEPTED = "accepted"
REVIEW_VERDICT_REJECTED = "rejected"
REVIEW_VERDICTS = (REVIEW_VERDICT_ACCEPTED, REVIEW_VERDICT_REJECTED)

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _delegation_gate import DelegationGateError, require_sanctioned_mutation
from _orchestration_config import OrchestrationConfigError, is_delegated, orchestration_config
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError
from _workspace_module_loader import load_workspace_module


class ResolveError(Exception):
    """A refused resolution with a machine-readable error code."""

    def __init__(self, exit_code: int, error_code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.error_code = error_code
        self.details = details or {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve claimed question task records for unattended research runs.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    answer = subparsers.add_parser("answer", help="Resolve a question as answered.")
    add_common_resolution_args(answer)
    answer.add_argument("--answer-page", required=True, help="Workspace-relative wiki page that answers the question.")
    answer.add_argument("--source-id", action="append", default=None, help="Manifest source id cited by the answer. Repeatable.")
    answer.add_argument(
        "--allow-uncited",
        action="store_true",
        help="Allow an intentionally uncited answer with no --source-id.",
    )
    answer.add_argument("--confidence", choices=CONFIDENCE_VALUES, default=None, help="Optional answer confidence.")
    answer.add_argument(
        "--evidence-strength",
        choices=EVIDENCE_STRENGTH_VALUES,
        default=None,
        help="Optional evidence-strength classification.",
    )
    answer.add_argument(
        "--require-coverage",
        action="store_true",
        help="Require the selected coverage manifest to evaluate to pass before answering.",
    )
    answer.add_argument(
        "--require-grounding",
        action="store_true",
        help="Require grounding quotes to verify against normalized source records before answering.",
    )
    answer.add_argument(
        "--coverage-manifest",
        default=None,
        help="Workspace-relative coverage manifest path under sources.coverage_dir. Defaults to the slug manifest.",
    )

    block = subparsers.add_parser("block", help="Resolve a question as blocked on missing evidence.")
    add_common_resolution_args(block)
    block.add_argument("--blocked-reason", required=True, help="Why the question is blocked.")
    block.add_argument("--request-id", action="append", default=None, help="Linked source request id. Repeatable.")

    defer = subparsers.add_parser("defer", help="Resolve a question as deferred.")
    add_common_resolution_args(defer)
    defer.add_argument("--reason", required=True, help="Why the question is deferred.")

    reject = subparsers.add_parser("reject", help="Resolve a question as rejected.")
    add_common_resolution_args(reject)
    reject.add_argument("--reason", required=True, help="Why the question is rejected.")

    reopen = subparsers.add_parser(
        "reopen",
        help="Reopen a blocked question once its requested evidence is delivered and normalized.",
    )
    reopen.add_argument("--slug", required=True, help="Question page slug (file name without .md).")
    reopen.add_argument("--agent-id", required=True, help="Identifier of the reopening agent.")
    reopen.add_argument(
        "--source-id",
        action="append",
        required=True,
        help="Manifest source id now available (must have a normalized record). Repeatable.",
    )
    reopen.add_argument(
        "--request-id",
        action="append",
        default=None,
        help="Fulfilled source request id linked to this question to verify. Repeatable.",
    )
    reopen.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )
    approve = subparsers.add_parser("approve", help="Approve a question that is pending human review.")
    approve.add_argument("--slug", required=True, help="Question page slug (file name without .md).")
    approve.add_argument("--reviewer", required=True, help="Human reviewer identity to record.")
    approve.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )

    review = subparsers.add_parser(
        "review",
        help="Record one per-policy human review collected inside or outside the workspace.",
    )
    review.add_argument("--slug", required=True, help="Question page slug (file name without .md).")
    review.add_argument(
        "--policy",
        required=True,
        help="One policy identifier from the question's human_review_policies.",
    )
    review.add_argument(
        "--verdict",
        required=True,
        help=f"Review verdict. One of: {', '.join(REVIEW_VERDICTS)}.",
    )
    review.add_argument(
        "--reviewed-by",
        required=True,
        help="Principal that recorded the review. Same trust model as --reviewer; not authenticated here.",
    )
    review.add_argument(
        "--review-ref",
        default=None,
        help="Opaque host-side pointer to where the review was collected, such as an approval-queue id.",
    )
    review.add_argument("--note", default=None, help="Optional reviewer note retained with the entry.")
    review.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )
    return parser.parse_args(argv)


def add_common_resolution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--slug", required=True, help="Question page slug (file name without .md).")
    parser.add_argument("--agent-id", required=True, help="Identifier of the resolving agent.")
    parser.add_argument(
        "--allow-unclaimed",
        action="store_true",
        help="Allow resolving an open or otherwise unheld question without a matching claim.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so its logic is reused directly."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def unique_nonempty(values: list[str] | None, label: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values or []:
        value = raw.strip()
        if not value:
            raise ResolveError(EXIT_INVALID, "VALUE_INVALID", f"{label} must not be empty")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def validate_workspace_relative_path(value: str, label: str) -> str:
    raw = value.strip()
    if not raw:
        raise ResolveError(EXIT_INVALID, "ANSWER_PAGE_INVALID", f"{label} must be a non-empty path")
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise ResolveError(EXIT_INVALID, "ANSWER_PAGE_INVALID", f"{label} must be workspace-relative, not a URL: {value}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise ResolveError(EXIT_INVALID, "ANSWER_PAGE_INVALID", f"{label} must not be an absolute path: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ResolveError(
            EXIT_INVALID,
            "ANSWER_PAGE_INVALID",
            f"{label} must be a workspace-relative path without '..': {value}",
        )
    return path.as_posix()


def validate_coverage_manifest_path_value(value: str, label: str) -> str:
    raw = value.strip()
    if not raw:
        raise ResolveError(EXIT_INVALID, "COVERAGE_MANIFEST_INVALID", f"{label} must be a non-empty path")
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_MANIFEST_INVALID",
            f"{label} must be workspace-relative, not a URL: {value}",
            details={"manifest_path": value},
        )
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_MANIFEST_INVALID",
            f"{label} must not be an absolute path: {value}",
            details={"manifest_path": value},
        )
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_MANIFEST_INVALID",
            f"{label} must be a workspace-relative path without '..': {value}",
            details={"manifest_path": value},
        )
    return path.as_posix()


def workspace_label(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def wiki_root(project_root: Path, config: dict[str, Any]) -> Path:
    question_status = load_sibling_module("question_status")
    wiki_config = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    wiki_value = wiki_config.get("root") if isinstance(wiki_config.get("root"), str) else "wiki"
    return project_root / question_status.validate_workspace_relative_path(wiki_value, "wiki.root")


def validate_answer_page(project_root: Path, config: dict[str, Any], question_path: Path, value: str) -> str:
    relative = validate_workspace_relative_path(value, "--answer-page")
    target = (project_root / relative).resolve()
    root = wiki_root(project_root, config).resolve()
    if target != root and root not in target.parents:
        raise ResolveError(
            EXIT_INVALID,
            "ANSWER_PAGE_INVALID",
            f"--answer-page must be under {workspace_label(project_root, root)}: {value}",
        )
    if not target.is_file():
        raise ResolveError(EXIT_INVALID, "ANSWER_PAGE_MISSING", f"answer page does not exist: {relative}")
    native_relative = os.path.relpath(target, start=question_path.parent.resolve())
    return PurePosixPath(native_relative.replace("\\", "/")).as_posix()


def selected_coverage_manifest_path(project_root: Path, config: dict[str, Any], slug: str, value: str | None) -> Path:
    coverage = load_sibling_module("coverage_manifest")
    try:
        coverage_root = coverage.coverage_dir(project_root, config).resolve()
    except coverage.CoverageManifestError as exc:
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_MANIFEST_INVALID",
            str(exc),
            details=getattr(exc, "details", None),
        ) from exc
    if value is None:
        return coverage_root / f"{slug}.yml"
    relative = validate_coverage_manifest_path_value(value, "--coverage-manifest")
    target = (project_root / relative).resolve()
    if target != coverage_root and coverage_root not in target.parents:
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_MANIFEST_INVALID",
            f"--coverage-manifest must be under {workspace_label(project_root, coverage_root)}: {value}",
            details={"manifest_path": relative},
        )
    return target


def failed_required_facets(document: dict[str, Any]) -> list[str]:
    failed: list[str] = []
    for facet in document.get("required_facets", []):
        if not isinstance(facet, dict) or facet.get("facet_verdict") == "pass":
            continue
        facet_id = facet.get("facet_id")
        failed.append(facet_id if isinstance(facet_id, str) and facet_id else "<unknown>")
    return failed


def requires_human_review(summary: dict[str, Any]) -> tuple[bool, list[str]]:
    raw_results = summary.get("policy_results")
    policy_results: list[dict[str, Any]] = []
    if isinstance(raw_results, list):
        policy_results = [result for result in raw_results if isinstance(result, dict)]
    elif isinstance(raw_results, dict):
        for facet in raw_results.get("facets", []) if isinstance(raw_results.get("facets"), list) else []:
            if not isinstance(facet, dict):
                continue
            policy_results.extend(
                result
                for result in facet.get("policy_results", [])
                if isinstance(result, dict)
            )
    reasons: list[str] = []
    for result in policy_results:
        policy = result.get("policy")
        verdict = result.get("verdict")
        if verdict == "manual_review" or policy in {"manual_review_required", "manual_review"}:
            reasons.append(str(policy or "manual_review"))
    return bool(reasons), sorted(set(reasons))


def enforce_coverage(project_root: Path, config: dict[str, Any], slug: str, manifest_value: str | None) -> dict[str, Any]:
    coverage = load_sibling_module("coverage_manifest")
    frontmatter: dict[str, Any] = {"coverage_required": True}
    if manifest_value is not None:
        frontmatter["coverage_manifest"] = manifest_value
    summary = coverage.coverage_summary_for_question(project_root, config, slug, frontmatter)
    manifest_label = summary.get("coverage_manifest") or f"sources/coverage/{slug}.yml"
    if summary["coverage_status"] == "missing":
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_REQUIRED",
            f"coverage manifest is required before answering: {manifest_label}",
            details={"manifest_path": manifest_label},
        )
    if summary["coverage_status"] == "invalid":
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_MANIFEST_INVALID",
            summary.get("error") or f"coverage manifest is invalid: {manifest_label}",
            details={"manifest_path": manifest_label},
        )
    verdict = summary["coverage_verdict"]
    if verdict != "pass":
        raise ResolveError(
            EXIT_INVALID,
            "COVERAGE_BLOCKED",
            f"coverage manifest must evaluate to pass before answering: {manifest_label}",
            details={
                "manifest_path": manifest_label,
                "coverage_verdict": verdict,
                "failed_required_facets": summary["failed_facets"],
            },
        )
    review_required, review_policies = requires_human_review(summary)
    return {
        "manifest_label": manifest_label,
        "human_review_required": review_required,
        "human_review_policies": review_policies,
    }


def manifest_source_ids(project_root: Path, config: dict[str, Any]) -> set[str]:
    source_requests = load_sibling_module("source_requests")
    return source_requests.manifest_source_ids(project_root, config)


def validate_source_ids(project_root: Path, config: dict[str, Any], source_ids: list[str]) -> list[str]:
    valid_ids = manifest_source_ids(project_root, config)
    for source_id in source_ids:
        if source_id not in valid_ids:
            raise ResolveError(
                EXIT_INVALID,
                "SOURCE_UNKNOWN",
                f"Unknown source id: {source_id} (not in the manifest)",
            )
    return source_ids


def has_normalized_record(project_root: Path, config: dict[str, Any], source_id: str) -> bool:
    """True when a normalized record exists for the source id (the reopen gate)."""
    normalize = load_sibling_module("normalize_sources")
    _, normalized_rel = normalize.source_paths(config)
    record_path = project_root / normalized_rel / f"{normalize.safe_source_id(source_id)}.md"
    return record_path.is_file()


def string_list_field(frontmatter: dict[str, Any], key: str) -> list[str]:
    value = frontmatter.get(key)
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def existing_source_ids(frontmatter: dict[str, Any]) -> list[str]:
    value = frontmatter.get("source_ids")
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def existing_blocking_request_ids(frontmatter: dict[str, Any]) -> list[str]:
    value = frontmatter.get("blocking_request_ids")
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def merge_ordered(existing: list[str], additions: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*existing, *additions]:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def load_source_requests(project_root: Path, config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    source_requests = load_sibling_module("source_requests")
    path = source_requests.requests_path(project_root, config)
    return source_requests.load_requests(path), workspace_label(project_root, path)


def validate_request_ids(project_root: Path, config: dict[str, Any], slug: str, request_ids: list[str]) -> list[str]:
    requests, label = load_source_requests(project_root, config)
    by_id = {
        record.get("request_id"): record
        for record in requests
        if isinstance(record.get("request_id"), str) and record.get("request_id")
    }
    for request_id in request_ids:
        record = by_id.get(request_id)
        if record is None:
            raise ResolveError(EXIT_INVALID, "REQUEST_UNKNOWN", f"Unknown request id: {request_id} (no record in {label})")
        slugs = record.get("question_slugs")
        if not (isinstance(slugs, list) and slug in [item for item in slugs if isinstance(item, str)]):
            raise ResolveError(
                EXIT_INVALID,
                "REQUEST_NOT_LINKED",
                f"source request {request_id} does not reference question slug {slug}",
            )
    return request_ids


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


def quote_scalar(value: str) -> str:
    if not value:
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_./+@ -]+", value):
        return value
    return json.dumps(value)


def render_mapping_sequence(key: str, entries: list[dict[str, str]]) -> list[str]:
    """Render a list of flat mappings, the shape `human_reviews` retains.

    Every value goes through ``quote_scalar``, which quotes anything outside the bare-scalar
    character class. Policy identifiers and ISO timestamps contain ``:`` and are therefore
    always emitted quoted, so the block round-trips through ``yaml.safe_load``.
    """
    lines = [f"{key}:"]
    for entry in entries:
        prefix = "  - "
        for field, value in entry.items():
            lines.append(f"{prefix}{field}: {quote_scalar(str(value))}")
            prefix = "    "
    return lines


def render_frontmatter_value(key: str, value: str | bool | list[str] | list[dict[str, str]], quote: bool) -> list[str]:
    if isinstance(value, list):
        if not value:
            return [f"{key}: []"]
        if all(isinstance(item, dict) for item in value):
            return render_mapping_sequence(key, value)
        return [f"{key}:"] + [f"  - {item}" for item in value]
    if isinstance(value, bool):
        return [f"{key}: {'true' if value else 'false'}"]
    rendered = f'"{value}"' if quote else quote_scalar(value)
    return [f"{key}: {rendered}"]


def set_frontmatter_field_block(lines: list[str], key: str, value: str | bool | list[str], *, quote: bool = False) -> list[str]:
    lines = remove_frontmatter_field_block(lines, key)
    return [*lines, *render_frontmatter_value(key, value, quote)]


def apply_resolution_edits(
    text: str,
    set_fields: dict[str, str | bool | list[str]],
    remove_fields: tuple[str, ...],
    quoted_fields: set[str] | None = None,
) -> str:
    question_claim = load_sibling_module("question_claim")
    parts = question_claim.split_frontmatter_lines(text)
    if parts is None:
        raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
    frontmatter_lines, opening, rest = parts
    for key in remove_fields:
        frontmatter_lines = remove_frontmatter_field_block(frontmatter_lines, key)
    for key, value in set_fields.items():
        frontmatter_lines = set_frontmatter_field_block(
            frontmatter_lines,
            key,
            value,
            quote=key in (quoted_fields or set()),
        )
    return "\n".join([*opening, *frontmatter_lines, *rest])


def enforce_claim(frontmatter: dict[str, Any], slug: str, agent_id: str, allow_unclaimed: bool) -> dict[str, Any]:
    question_claim = load_sibling_module("question_claim")
    status = frontmatter.get("status")
    holder = question_claim.holder_block(frontmatter)
    claimed_by = holder.get("claimed_by")

    if status in TERMINAL_STATUSES:
        raise ResolveError(
            EXIT_INVALID,
            "STATUS_NOT_RESOLVABLE",
            f"question {slug} already has terminal status '{status}'; terminal statuses are not rewritten",
        )
    if claimed_by is not None and claimed_by != agent_id:
        raise ResolveError(
            EXIT_CONFLICT,
            "CLAIM_HELD",
            f"question is claimed by {claimed_by}; agents never resolve another agent's claim",
        )
    if status == "in_progress" and claimed_by == agent_id:
        return holder
    if allow_unclaimed and claimed_by is None:
        return holder
    raise ResolveError(
        EXIT_INVALID,
        "QUESTION_NOT_CLAIMED",
        f"question {slug} is not claimed by {agent_id}; pass --allow-unclaimed for an explicit unclaimed resolution",
    )


def enforce_grounding(project_root: Path, config: dict[str, Any], slug: str, frontmatter: dict[str, Any]) -> dict[str, Any]:
    verify_quotes = load_sibling_module("verify_quotes")
    try:
        report = verify_quotes.verify_question(project_root, config, slug, frontmatter=frontmatter)
    except verify_quotes.VerifyQuotesError as exc:
        raise ResolveError(
            EXIT_INVALID,
            exc.error_code,
            str(exc),
            details={"slug": slug, **getattr(exc, "details", {})},
        ) from exc
    if not report.get("grounding"):
        raise ResolveError(
            EXIT_INVALID,
            "GROUNDING_REQUIRED",
            f"answer resolution for {slug} requires non-empty grounding entries",
            details={"slug": slug},
        )
    failed = [
        result
        for result in report.get("grounding", [])
        if isinstance(result, dict) and result.get("result") != verify_quotes.RESULT_VERIFIED
    ]
    if failed:
        raise ResolveError(
            EXIT_INVALID,
            "GROUNDING_QUOTE_INVALID",
            f"answer resolution for {slug} has {len(failed)} grounding quote verification failure(s)",
            details={"slug": slug, "failures": failed},
        )
    return report


def resolution_fields(
    args: argparse.Namespace,
    project_root: Path,
    config: dict[str, Any],
    question_path: Path,
    frontmatter: dict[str, Any],
) -> dict[str, Any]:
    if args.command == "answer":
        source_ids = validate_source_ids(project_root, config, unique_nonempty(args.source_id, "--source-id"))
        if not source_ids and not getattr(args, "allow_uncited", False):
            raise ResolveError(
                EXIT_INVALID,
                "ANSWER_SOURCE_REQUIRED",
                "answer resolution requires at least one --source-id unless --allow-uncited is explicit",
            )
        coverage_result: dict[str, Any] | None = None
        if getattr(args, "require_coverage", False):
            coverage_result = enforce_coverage(project_root, config, args.slug.strip(), getattr(args, "coverage_manifest", None))
        grounding_report = None
        if getattr(args, "require_grounding", False):
            grounding_report = enforce_grounding(project_root, config, args.slug.strip(), frontmatter)
        status = "human_review" if coverage_result and coverage_result["human_review_required"] else "answered"
        fields: dict[str, Any] = {
            "status": status,
            "answer_page": validate_answer_page(project_root, config, question_path, args.answer_page),
            "answered_by": args.agent_id.strip(),
        }
        if coverage_result is not None:
            fields["coverage_required"] = True
            fields["coverage_manifest"] = coverage_result["manifest_label"]
            if coverage_result["human_review_required"]:
                fields["human_review_required"] = True
                fields["human_review_status"] = "pending"
                fields["human_review_policies"] = coverage_result["human_review_policies"]
        if grounding_report is not None:
            fields["grounding_required"] = True
        if source_ids:
            fields["source_ids"] = source_ids
        if args.confidence:
            fields["confidence"] = args.confidence
        if args.evidence_strength:
            fields["evidence_strength"] = args.evidence_strength
        return {
            "status": status,
            "fields": fields,
            "request_ids": [],
            "source_ids": source_ids,
            "grounding": grounding_report,
        }
    if args.command == "block":
        reason = args.blocked_reason.strip()
        if not reason:
            raise ResolveError(EXIT_INVALID, "RESOLUTION_REASON_INVALID", "--blocked-reason must be non-empty")
        request_ids = validate_request_ids(project_root, config, args.slug.strip(), unique_nonempty(args.request_id, "--request-id"))
        merged_request_ids = merge_ordered(existing_blocking_request_ids(frontmatter), request_ids)
        fields: dict[str, Any] = {"status": "blocked", "blocked_reason": reason}
        if merged_request_ids:
            fields["blocking_request_ids"] = merged_request_ids
        return {
            "status": "blocked",
            "fields": fields,
            "request_ids": request_ids,
            "source_ids": [],
        }
    reason = args.reason.strip()
    if not reason:
        raise ResolveError(EXIT_INVALID, "RESOLUTION_REASON_INVALID", "--reason must be non-empty")
    status = "deferred" if args.command == "defer" else "rejected"
    return {
        "status": status,
        "fields": {"status": status, "resolution_reason": reason},
        "request_ids": [],
        "source_ids": [],
    }


def transition_resolution(
    page_path: Path,
    project_root: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    question_claim = load_sibling_module("question_claim")
    with question_claim.question_lock(page_path):
        text = page_path.read_text(encoding="utf-8")
        parts = question_claim.split_frontmatter_lines(text)
        if parts is None:
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
        frontmatter = question_claim.frontmatter_mapping(parts[0])
        if frontmatter.get("type") != "question":
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "page is not a question task record")
        previous_holder = enforce_claim(frontmatter, args.slug.strip(), args.agent_id.strip(), args.allow_unclaimed)
        resolution = resolution_fields(args, project_root, config, page_path, frontmatter)
        now = question_claim.timestamp_utc()
        fields = dict(resolution["fields"])
        fields["updated"] = now.split("T", 1)[0]
        remove_fields = ("claimed_by", "claimed_at")
        if resolution["status"] in {"answered", "human_review"}:
            remove_fields = (
                *remove_fields,
                "blocked_reason",
                "blocking_request_ids",
                "resolution_reason",
                "approved_by",
                "approved_at",
                "human_review_approved",
                # A new answer opens a new review cycle: reviews of the previous answer must not
                # count towards this one. The audit trail of superseded reviews stays in log.md.
                "human_reviews",
                "human_review_requested_at",
            )
            if resolution["status"] == "human_review":
                # The answer transition is the single writer of entry into human_review, so it also
                # stamps the clock that the stale-review lint finding reads.
                fields["human_review_requested_at"] = now
        elif resolution["status"] == "blocked":
            remove_fields = (*remove_fields, "answer_page", "confidence", "evidence_strength", "resolution_reason")
        else:
            remove_fields = (
                *remove_fields,
                "answer_page",
                "blocked_reason",
                "blocking_request_ids",
                "confidence",
                "evidence_strength",
            )
        updated = apply_resolution_edits(
            text,
            fields,
            remove_fields,
            quoted_fields={"updated", "human_review_requested_at"},
        )
        question_claim.write_page_atomic(page_path, updated)
        return {
            "applied": True,
            "status": resolution["status"],
            "previous_holder": previous_holder,
            "answer_page": fields.get("answer_page"),
            "source_ids": fields.get("source_ids", []),
            "request_ids": resolution["request_ids"],
        }


def require_in_order_question_mutation(project_root: Path, config: dict[str, Any], slug: str) -> None:
    """Refuse reopening a question that no pending work order scopes, under delegation.

    A workspace that does not delegate acquisition is never gated: its questions are
    reopened by an in-workspace acquire agent inside its own work order, or by an operator
    who is not driving a protocol at all.
    """
    try:
        delegated = is_delegated(orchestration_config(config))
    except OrchestrationConfigError as exc:
        raise ResolveError(EXIT_INVALID, exc.error_code, f"invalid research.yml: {exc.message}") from exc
    require_sanctioned_mutation(
        project_root,
        delegated,
        question_slug=slug,
        error_code="QUESTION_REOPEN_DELEGATED",
        subject=f"question {slug}",
        remediation=(
            "Reopen this question while executing the work order that scopes it, or finish the active "
            "session first."
        ),
    )


def transition_reopen(
    page_path: Path,
    project_root: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Move a blocked question back to open after its evidence is delivered and normalized.

    Reopen is the only transition that operates on a terminal status, so it does not
    go through ``enforce_claim`` (a blocked question is never claimed). It requires at
    least one delivered source id that is in the manifest and has a normalized record.
    """
    question_claim = load_sibling_module("question_claim")
    slug = args.slug.strip()
    with question_claim.question_lock(page_path):
        text = page_path.read_text(encoding="utf-8")
        parts = question_claim.split_frontmatter_lines(text)
        if parts is None:
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
        frontmatter = question_claim.frontmatter_mapping(parts[0])
        if frontmatter.get("type") != "question":
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "page is not a question task record")
        status = frontmatter.get("status")
        if status != "blocked":
            raise ResolveError(
                EXIT_INVALID,
                "STATUS_NOT_REOPENABLE",
                f"question {slug} has status '{status}'; only blocked questions can be reopened",
            )
        # Under delegated acquisition this transition must belong to a pending work order.
        # Reopen has no no-op path to exempt: a question that is not blocked was already
        # refused above, so everything reaching here mutates the page.
        require_in_order_question_mutation(project_root, config, slug)
        source_ids = validate_source_ids(project_root, config, unique_nonempty(args.source_id, "--source-id"))
        if not source_ids:
            raise ResolveError(EXIT_INVALID, "VALUE_INVALID", "reopen requires at least one --source-id")
        for source_id in source_ids:
            if not has_normalized_record(project_root, config, source_id):
                raise ResolveError(
                    EXIT_INVALID,
                    "SOURCE_NOT_NORMALIZED",
                    f"source {source_id} has no normalized record yet; normalize the delivered source before reopening",
                )
        request_ids = validate_request_ids(project_root, config, slug, unique_nonempty(args.request_id, "--request-id"))
        merged = existing_source_ids(frontmatter)
        for source_id in source_ids:
            if source_id not in merged:
                merged.append(source_id)
        now = question_claim.timestamp_utc()
        fields: dict[str, Any] = {"status": "open", "source_ids": merged, "updated": now.split("T", 1)[0]}
        remove_fields = ("claimed_by", "claimed_at", "blocked_reason", "blocking_request_ids")
        updated = apply_resolution_edits(text, fields, remove_fields, quoted_fields={"updated"})
        question_claim.write_page_atomic(page_path, updated)
        return {
            "applied": True,
            "status": "open",
            "previous_holder": {},
            "answer_page": None,
            "source_ids": merged,
            "request_ids": request_ids,
        }


def existing_human_reviews(frontmatter: dict[str, Any]) -> list[dict[str, str]]:
    """Return retained per-policy review entries, dropping anything not shaped like one."""
    value = frontmatter.get("human_reviews")
    if not isinstance(value, list):
        return []
    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entry = {
            str(field): str(field_value)
            for field, field_value in item.items()
            if field_value is not None and str(field_value).strip()
        }
        if entry.get("policy") and entry.get("verdict"):
            entries.append(entry)
    return entries


def accepted_review_policies(entries: list[dict[str, str]]) -> set[str]:
    return {entry["policy"] for entry in entries if entry.get("verdict") == REVIEW_VERDICT_ACCEPTED}


def record_human_reviews(
    page_path: Path,
    *,
    slug: str,
    verdict: str,
    reviewed_by: str,
    policies: list[str] | None,
    review_ref: str | None = None,
    note: str | None = None,
    status_error_code: str,
) -> dict[str, Any]:
    """Append per-policy review entries and apply the resulting lifecycle transition.

    This is the only writer of recorded reviews. ``review`` names one policy explicitly;
    ``approve`` passes ``policies=None`` to accept every policy still pending, which keeps
    the in-workspace and host-collected reviewer topologies on one code path.
    """
    question_claim = load_sibling_module("question_claim")
    if not reviewed_by:
        raise ResolveError(EXIT_INVALID, "REVIEWER_INVALID", "review principal must be a non-empty string")
    if verdict not in REVIEW_VERDICTS:
        raise ResolveError(
            EXIT_INVALID,
            "REVIEW_VERDICT_INVALID",
            f"--verdict must be one of: {', '.join(REVIEW_VERDICTS)}",
        )
    with question_claim.question_lock(page_path):
        text = page_path.read_text(encoding="utf-8")
        parts = question_claim.split_frontmatter_lines(text)
        if parts is None:
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
        frontmatter = question_claim.frontmatter_mapping(parts[0])
        if frontmatter.get("type") != "question":
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "page is not a question task record")
        status = frontmatter.get("status")
        if status != "human_review":
            raise ResolveError(
                EXIT_INVALID,
                status_error_code,
                f"question {slug} has status '{status}'; only human_review questions can be reviewed",
            )
        declared = string_list_field(frontmatter, "human_review_policies")
        retained = existing_human_reviews(frontmatter)
        already_accepted = accepted_review_policies(retained)
        if policies is None:
            selected = [policy for policy in declared if policy not in already_accepted]
        else:
            selected = []
            for policy in policies:
                if policy not in declared:
                    raise ResolveError(
                        EXIT_INVALID,
                        "REVIEW_POLICY_UNKNOWN",
                        f"policy {policy} is not one of the question's human_review_policies",
                        details={"policy": policy, "human_review_policies": declared},
                    )
                if verdict == REVIEW_VERDICT_ACCEPTED and policy in already_accepted:
                    raise ResolveError(
                        EXIT_INVALID,
                        "REVIEW_ALREADY_RECORDED",
                        f"policy {policy} already has a recorded accepted review; reviews are append-only",
                        details={"policy": policy},
                    )
                selected.append(policy)

        now = question_claim.timestamp_utc()
        appended: list[dict[str, str]] = []
        for policy in selected:
            entry: dict[str, str] = {"policy": policy, "verdict": verdict, "reviewed_by": reviewed_by}
            if review_ref:
                entry["review_ref"] = review_ref
            if note:
                entry["note"] = note
            entry["reviewed_at"] = now
            appended.append(entry)
        entries = [*retained, *appended]

        fields: dict[str, Any] = {"updated": now.split("T", 1)[0]}
        remove_fields: tuple[str, ...] = ()
        if verdict == REVIEW_VERDICT_REJECTED:
            # A rejected answer returns to ordinary open work. The reason lives in the review
            # entry's note, not in blocked_reason prose, and the stale approval and claim fields
            # go with it.
            resulting_status = "open"
            fields["status"] = "open"
            fields["human_review_status"] = REVIEW_VERDICT_REJECTED
            remove_fields = (
                "claimed_by",
                "claimed_at",
                "approved_by",
                "approved_at",
                "human_review_approved",
                "human_review_requested_at",
            )
        elif all(policy in accepted_review_policies(entries) for policy in declared):
            # Every declared policy now has an accepted review. Write exactly the fields the
            # 0.2.4 approve path wrote so export and publication readiness need no schema change.
            resulting_status = "answered"
            fields["status"] = "answered"
            fields["human_review_required"] = True
            fields["human_review_status"] = "approved"
            fields["human_review_approved"] = True
            fields["approved_by"] = reviewed_by
            fields["approved_at"] = now
        else:
            resulting_status = "human_review"
            fields["human_review_status"] = "pending"
        if entries:
            fields["human_reviews"] = entries

        updated = apply_resolution_edits(
            text,
            fields,
            remove_fields,
            quoted_fields={"approved_at", "updated"},
        )
        question_claim.write_page_atomic(page_path, updated)
        return {
            "applied": True,
            "status": resulting_status,
            "previous_holder": {},
            "answer_page": frontmatter.get("answer_page"),
            "source_ids": existing_source_ids(frontmatter),
            "request_ids": [],
            "reviewer": reviewed_by,
            "approved_at": now if resulting_status == "answered" else None,
            "review_verdict": verdict,
            "reviewed_policies": selected,
            "review_ref": review_ref,
            "pending_policies": [
                policy for policy in declared if policy not in accepted_review_policies(entries)
            ],
            "human_reviews": entries,
        }


def transition_review(page_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    policy = args.policy.strip()
    if not policy:
        raise ResolveError(EXIT_INVALID, "REVIEW_POLICY_UNKNOWN", "--policy must be a non-empty string")
    return record_human_reviews(
        page_path,
        slug=args.slug.strip(),
        verdict=args.verdict.strip(),
        reviewed_by=args.reviewed_by.strip(),
        policies=[policy],
        review_ref=(args.review_ref or "").strip() or None,
        note=(args.note or "").strip() or None,
        status_error_code="STATUS_NOT_REVIEWABLE",
    )


def transition_approve(page_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Accept every still-pending policy in one call: the in-workspace reviewer topology."""
    return record_human_reviews(
        page_path,
        slug=args.slug.strip(),
        verdict=REVIEW_VERDICT_ACCEPTED,
        reviewed_by=args.reviewer.strip(),
        policies=None,
        status_error_code="STATUS_NOT_APPROVABLE",
    )


def render_log(action: str, slug: str, agent_id: str, result: dict[str, Any]) -> str:
    question_claim = load_sibling_module("question_claim")
    date_text = question_claim.timestamp_utc().split("T", 1)[0]
    headline = "reopened" if action == "reopen" else result["status"]
    lines = [
        f"## [{date_text}] resolve | Question {headline}",
        "",
        f"- Question: `{slug}` ({action}).",
        f"- Agent: {agent_id}.",
    ]
    if action == "reopen" and result.get("source_ids"):
        lines.append(f"- Reopened with sources: {', '.join(result['source_ids'])}.")
    if action in {"approve", "review"}:
        lines.append(f"- Reviewer: {result.get('reviewer')}.")
        if result.get("reviewed_policies"):
            lines.append(
                f"- Reviewed {result.get('review_verdict', REVIEW_VERDICT_ACCEPTED)}: "
                f"{', '.join(result['reviewed_policies'])}."
            )
        if result.get("review_ref"):
            lines.append(f"- Review reference: {result['review_ref']}.")
        if result.get("pending_policies"):
            lines.append(f"- Still pending review: {', '.join(result['pending_policies'])}.")
        if result.get("approved_at"):
            lines.append(f"- Approved at: {result['approved_at']}.")
    if result.get("answer_page"):
        lines.append(f"- Answer page: {result['answer_page']}.")
    if result.get("request_ids"):
        lines.append(f"- Source requests: {', '.join(result['request_ids'])}.")
    return "\n".join(lines) + "\n"


def build_report(action: str, slug: str, agent_id: str, page_path: Path, project_root: Path, result: dict[str, Any]) -> dict[str, Any]:
    report = {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": True,
        "slug": slug,
        "agent_id": agent_id,
        "applied": result["applied"],
        "status": result["status"],
        "question_page": workspace_label(project_root, page_path),
        "answer_page": result.get("answer_page"),
        "source_ids": result.get("source_ids", []),
        "request_ids": result.get("request_ids", []),
        "previous_holder": result.get("previous_holder"),
    }
    if result.get("reviewer"):
        report["reviewer"] = result["reviewer"]
    if result.get("approved_at"):
        report["approved_at"] = result["approved_at"]
    if action in {"approve", "review"}:
        report["review_verdict"] = result.get("review_verdict")
        report["reviewed_policies"] = result.get("reviewed_policies", [])
        report["review_ref"] = result.get("review_ref")
        report["pending_policies"] = result.get("pending_policies", [])
        report["human_reviews"] = result.get("human_reviews", [])
    return report


def render_text_report(report: dict[str, Any]) -> str:
    return f"{report['status']}: {report['slug']}\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    slug = args.slug.strip()
    principal_flags = {"approve": ("--reviewer", "reviewer"), "review": ("--reviewed-by", "reviewed_by")}
    if args.command in principal_flags:
        agent_id = getattr(args, principal_flags[args.command][1]).strip()
    else:
        agent_id = args.agent_id.strip()
    action = args.command
    try:
        if not agent_id:
            label = principal_flags.get(args.command, ("--agent-id",))[0]
            error_code = "REVIEWER_INVALID" if args.command in principal_flags else "AGENT_ID_INVALID"
            raise ResolveError(EXIT_INVALID, error_code, f"{label} must be a non-empty string")
        question_claim = load_sibling_module("question_claim")
        question_status = load_sibling_module("question_status")
        config = question_status.load_config(project_root)
        page_path = question_claim.question_page_path(project_root, slug)
        if args.command == "reopen":
            result = transition_reopen(page_path, project_root, config, args)
        elif args.command == "approve":
            result = transition_approve(page_path, args)
        elif args.command == "review":
            result = transition_review(page_path, args)
        else:
            result = transition_resolution(page_path, project_root, config, args)
    except DelegationGateError as error:
        details = {"action": action, "slug": slug, "agent_id": agent_id}
        details.update(error.details)
        if json_mode:
            emit_error(
                error.message,
                json_mode=True,
                error_code=error.error_code,
                remediation=error.remediation,
                details=details,
            )
        else:
            print(f"refused ({error.error_code}): {error.message}", file=sys.stderr)
        return EXIT_INVALID
    except ResolveError as error:
        if json_mode:
            details = {"action": action, "slug": slug, "agent_id": agent_id}
            details.update(error.details)
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                details=details,
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return error.exit_code
    except LockUnavailableError as error:
        if json_mode:
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                details={"action": action, "slug": slug, "agent_id": agent_id, **error.details},
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    question_claim = load_sibling_module("question_claim")
    try:
        question_claim.append_log_entry(project_root / "log.md", render_log(action, slug, agent_id, result))
    except LockUnavailableError as error:
        if json_mode:
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                details={"action": action, "slug": slug, "agent_id": agent_id, **error.details},
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return EXIT_INVALID
    report = build_report(action, slug, agent_id, page_path, project_root, result)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text_report(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
