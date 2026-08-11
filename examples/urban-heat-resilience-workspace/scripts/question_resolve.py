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
  ``--grounding-file FILE`` records the answer's grounding from the same file
  format ``grounding set`` reads, in the *same* atomic write as the resolution
  fields, so the page never holds new grounding beside an old status. With
  ``--require-grounding`` the file's entries are what must verify.
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
- ``grounding set --slug SLUG --from-file FILE --agent-id ID`` replaces the
  question's whole ``grounding`` block from a YAML (or JSON) file, under the same
  claim rules and the same per-question lock every other mutation uses. It is the
  supported alternative to hand-editing question frontmatter: the block is written
  in the canonical serialization, so a host never has to round-trip a question page
  through its own YAML dumper. Entry shape and manifest membership are enforced
  before anything is written; verification is **not** performed here, because the
  two-step flow records grounding while cited evidence may still be normalizing.
  Run ``verify_quotes.py --slug SLUG`` for that, and see ``--grounding-file`` below
  for the single-write alternative. Like the resolution verbs it does not rewrite a
  terminal question: correcting an answered question's grounding is a reopen cycle,
  not an edit.
- ``reopen --slug SLUG --agent-id ID --source-id MANIFEST_ID`` moves a
  ``blocked`` question back to ``open`` once the delivered evidence is in the
  manifest and has a normalized record, drops ``blocked_reason``, and adds the
  fulfilled source id(s) so ``research-answer`` can pick the question up. It is
  the deterministic counterpart to ``block`` and the only verb that operates on a
  terminal status; it requires no claim because a blocked question is unclaimed.
  When a supplied ``--request-id`` carries a structured ``scope``, reopen pairs it
  with the supplied source whose provenance scope agrees, and reports the result
  as ``pairs`` — so a host stops zipping the two repeatable flags by position.
  Reopen never writes to the request records: pairing is computed and verified,
  and fulfilment stays with ``source_requests.py fulfill``.

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
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

import yaml

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
# `grounding` is the only nested subcommand here, so `args.command` alone no longer names
# the action. Reports and log entries carry the full spelling a host would type.
GROUNDING_COMMAND = "grounding"
GROUNDING_SET_ACTION = "grounding set"
# Verifier stamps attest specific grounding entries. Replacing the block invalidates them,
# so both write paths drop them rather than leave a page claiming verified state it lost.
GROUNDING_VERIFICATION_STAMPS = ("verified_by", "grounding_verified_at")
GROUNDING_VERIFICATION_NOT_PERFORMED = "not_performed"

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _delegation_gate import DelegationGateError, require_sanctioned_mutation
from _orchestration_config import OrchestrationConfigError, is_delegated, orchestration_config
from _request_scope import conflict_details, format_scope, normalize_scope, scope_match
from _script_errors import ScriptRefusal, emit_refusal, json_mode_requested
from _workspace_locks import LockUnavailableError
from _workspace_module_loader import load_workspace_module

#: The verb whose principal is not ``--agent-id``, and the namespace attribute it lands on.
#: Read by both the seams and ``main`` so the recorded principal cannot depend on the door.
PRINCIPAL_FLAGS = {"approve": ("--reviewer", "reviewer"), "review": ("--reviewed-by", "reviewed_by")}


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
    answer.add_argument(
        "--grounding-file",
        default=None,
        help=(
            "YAML/JSON file whose grounding entries replace the question's grounding block in the same "
            "atomic write as the answer. Same format as 'grounding set --from-file'."
        ),
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

    grounding = subparsers.add_parser(
        GROUNDING_COMMAND,
        help="Write the question's grounding block from a file, without resolving the question.",
    )
    # A distinct dest keeps the outer `command` equal to "grounding", so main()'s dispatch,
    # principal lookup, and error envelopes keep reading one key. The action string a report
    # or log entry carries is normalized to the full "grounding set" spelling instead.
    grounding_commands = grounding.add_subparsers(dest="grounding_command", required=True)
    grounding_set = grounding_commands.add_parser(
        "set",
        help="Replace the whole grounding block from a YAML/JSON file (never merged).",
    )
    grounding_set.add_argument("--slug", required=True, help="Question page slug (file name without .md).")
    grounding_set.add_argument("--agent-id", required=True, help="Identifier of the writing agent.")
    grounding_set.add_argument(
        "--from-file",
        required=True,
        help="YAML/JSON file carrying a top-level 'grounding:' list, or a bare list of entries.",
    )
    grounding_set.add_argument(
        "--allow-unclaimed",
        action="store_true",
        help="Allow writing grounding on an open or otherwise unheld question without a matching claim.",
    )
    grounding_set.add_argument(
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
    try:
        summary = coverage.coverage_summary_for_question(project_root, config, slug, frontmatter)
    except coverage.CoverageManifestError as exc:
        # A research.yml error — a malformed `domain_pack.policy_rules` block — reaches
        # here as a refusal rather than a summary, precisely so it is not reported against
        # this question's manifest. Carried through with its own code so the operator is
        # sent to the file that is actually wrong.
        raise ResolveError(
            EXIT_INVALID,
            exc.error_code,
            exc.message,
            details=exc.details,
        ) from exc
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


def validate_request_records(
    project_root: Path,
    config: dict[str, Any],
    slug: str,
    request_ids: list[str],
) -> list[dict[str, Any]]:
    """Validate supplied request ids and return their records in the supplied order.

    Callers that only need the ids use ``validate_request_ids``; reopen needs the
    records themselves because the optional ``scope`` mapping lives on them.
    """
    requests, label = load_source_requests(project_root, config)
    by_id = {
        record.get("request_id"): record
        for record in requests
        if isinstance(record.get("request_id"), str) and record.get("request_id")
    }
    records: list[dict[str, Any]] = []
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
        records.append(record)
    return records


def validate_request_ids(project_root: Path, config: dict[str, Any], slug: str, request_ids: list[str]) -> list[str]:
    validate_request_records(project_root, config, slug, request_ids)
    return request_ids


def source_scope_resolver(project_root: Path, config: dict[str, Any]) -> Callable[[str], dict[str, str]]:
    """Return a memoized reader of a delivered source's declared provenance scope.

    Lazy on purpose. Each lookup scans the manifest, so resolving eagerly would make
    every reopen pay for pairing even in a workspace where no request declares a
    scope. Pairing asks only for the sources it actually examines, and asks once.
    """
    source_requests = load_sibling_module("source_requests")
    cache: dict[str, dict[str, str]] = {}

    def resolve(source_id: str) -> dict[str, str]:
        if source_id not in cache:
            cache[source_id] = source_requests.source_provenance_scope(project_root, config, source_id)
        return cache[source_id]

    return resolve


def _no_matching_source_error(
    request_id: str,
    request_scope: dict[str, str],
    rejected: list[tuple[str, list[str]]],
    source_scope: Callable[[str], dict[str, str]],
) -> ResolveError:
    rejected_details = [
        {
            "source_id": source_id,
            "conflicts": conflict_details(request_scope, source_scope(source_id), keys),
        }
        for source_id, keys in rejected
    ]
    rendered = "; ".join(
        "{source_id} disagrees on {keys}".format(
            source_id=entry["source_id"],
            keys=", ".join(
                f"{item['key']} (request {item['request_value']!r}, source {item['source_value']!r})"
                for item in entry["conflicts"]
            ),
        )
        for entry in rejected_details
    ) or "no source was supplied to pair it with"
    return ResolveError(
        EXIT_INVALID,
        "REQUEST_SCOPE_MISMATCH",
        (
            f"source request {request_id} declares scope {format_scope(request_scope)} and no supplied "
            f"source agrees with it: {rendered}"
        ),
        details={
            "reason": "no_matching_source",
            "request_id": request_id,
            "request_scope": dict(request_scope),
            "rejected_sources": rejected_details,
        },
    )


def _ambiguous_assignment_error(
    request_ids: list[str],
    source_ids: list[str],
    scoped: list[tuple[str, dict[str, str]]],
    candidates: dict[str, list[str]],
) -> ResolveError:
    scope_by_id = dict(scoped)
    return ResolveError(
        EXIT_INVALID,
        "REQUEST_SCOPE_MISMATCH",
        (
            f"scoped source requests {', '.join(request_ids)} cannot each be paired with a distinct "
            f"supplied source; they compete for {', '.join(source_ids)}. Supply one delivered source per "
            "scoped request, or stamp each delivery's provenance scope so the pairing is unambiguous"
        ),
        details={
            "reason": "ambiguous_assignment",
            "request_ids": request_ids,
            "source_ids": source_ids,
            "requests": [
                {
                    "request_id": request_id,
                    "request_scope": dict(scope_by_id.get(request_id, {})),
                    "candidate_source_ids": candidates.get(request_id, []),
                }
                for request_id in request_ids
            ],
        },
    )


def compute_request_source_pairs(
    request_records: list[dict[str, Any]],
    source_ids: list[str],
    source_scope: Callable[[str], dict[str, str]],
) -> list[dict[str, str]]:
    """Pair each scoped request with the supplied source that answers it.

    Only the contradiction layer applies here (see ``_request_scope``): a key both
    sides declare must agree, while a key only one side declares is compatible.
    Strictness about absence is a fulfil-time concern (``fulfill --require-scope``)
    and deliberately has no equivalent on reopen — reopen reports a pairing, it does
    not record fulfilment.

    Requests without a scope are left unpaired, which is exactly today's behaviour;
    a workspace where nothing declares scope therefore gets an empty list and no new
    refusals. Refusals are raised before the caller writes anything.
    """
    scoped: list[tuple[str, dict[str, str]]] = []
    for record in request_records:
        request_scope = normalize_scope(record.get("scope"))
        if request_scope:
            scoped.append((str(record["request_id"]), request_scope))
    if not scoped:
        return []

    candidates: dict[str, list[str]] = {}
    for request_id, request_scope in scoped:
        ranked: list[tuple[int, int, str]] = []
        rejected: list[tuple[str, list[str]]] = []
        for index, source_id in enumerate(source_ids):
            declared = source_scope(source_id)
            conflicts, _ = scope_match(request_scope, declared)
            if conflicts:
                rejected.append((source_id, conflicts))
                continue
            # Prefer a source that positively corroborates more of the request's scope
            # over one that merely fails to contradict it; ties keep the supplied order,
            # so the reported pairing is deterministic.
            agreeing = sum(1 for key in request_scope if key in declared)
            ranked.append((-agreeing, index, source_id))
        if not ranked:
            raise _no_matching_source_error(request_id, request_scope, rejected, source_scope)
        candidates[request_id] = [source_id for _, _, source_id in sorted(ranked)]

    # One source answers at most one scoped request, so a valid reopen needs a perfect
    # matching over the scoped requests. Augmenting paths (Kuhn's algorithm) find one if
    # it exists; failing to extend the matching means two scoped requests can only be
    # satisfied by the same source, which is precisely the mis-pairing this refuses.
    # Recursion depth is bounded by the number of scoped --request-id values in one
    # invocation, which is a handful in every real reopen.
    assigned_to: dict[str, str] = {}

    def augment(request_id: str, visited: set[str]) -> bool:
        for source_id in candidates[request_id]:
            if source_id in visited:
                continue
            visited.add(source_id)
            holder = assigned_to.get(source_id)
            if holder is None or augment(holder, visited):
                assigned_to[source_id] = request_id
                return True
        return False

    for request_id, _ in scoped:
        visited: set[str] = set()
        if not augment(request_id, visited):
            contested_sources = sorted(visited)
            contested_requests = sorted(
                {request_id, *(assigned_to[source_id] for source_id in visited if source_id in assigned_to)}
            )
            raise _ambiguous_assignment_error(contested_requests, contested_sources, scoped, candidates)

    paired = {request_id: source_id for source_id, request_id in assigned_to.items()}
    return [{"request_id": request_id, "source_id": paired[request_id]} for request_id, _ in scoped]


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


_BARE_SCALAR_RE = re.compile(r"[A-Za-z0-9_./+@ -]+")


def _reloads_bare(probe: str, expected: Any) -> bool:
    """True when a bare rendering reloads as exactly the string it was written from.

    Membership in a character class cannot decide whether YAML will hand a value back
    unchanged: ``007`` reloads as an int, ``true``/``yes``/``on`` as a bool, ``null`` as
    None, ``2026-08-09`` as a date, a padded value loses its spaces, and a leading ``-``
    is a block-sequence indicator that makes the whole page unparseable. Rather than
    enumerate those rules — they differ between YAML 1.1 and 1.2 and are the loader's to
    change — ask the loader, in the same syntactic position the value will occupy.
    """
    try:
        return yaml.safe_load(probe) == expected
    except yaml.YAMLError:
        return False


def quote_yaml_string(value: str) -> str:
    """A YAML double-quoted scalar carrying ``value`` exactly, and readably.

    ``ensure_ascii=False``: JSON's default would write a claim quoting a source as
    ``"23,99 \\u20ac \\u2014 confirm\\u00e9"``. That reloads correctly, so nothing fails —
    it just makes the block unreadable in the Obsidian page a human is meant to be able
    to open and edit, which is most of the point of storing evidence as frontmatter.
    A YAML double-quoted scalar carries literal UTF-8, so the escaping bought nothing.
    """
    return json.dumps(value, ensure_ascii=False)


def quote_scalar(value: str) -> str:
    """Render a mapping value, quoting whenever a bare rendering would not survive reload."""
    if not value:
        return '""'
    if _BARE_SCALAR_RE.fullmatch(value) and _reloads_bare(f"probe: {value}", {"probe": value}):
        return value
    return quote_yaml_string(value)


def quote_sequence_item(value: str) -> str:
    """Render a sequence item, quoting only when a bare rendering would not survive reload.

    The probe differs from ``quote_scalar``'s because the context does: ``- web:vendor`` is
    a plain string, while ``- has: colon space`` is a mapping and ``- - dash`` a nested
    sequence. A value is only safe bare in the position it is actually written to.

    Unlike ``quote_scalar`` this applies no character-class prefilter. Manifest ids carry
    colons (``web:vendor-…``, ``pack:market-data/…``) and reload identically bare here, so
    the class would quote them for nothing — rewriting the ``source_ids`` of every existing
    workspace on the next resolution. Round-tripping is the property that matters; the class
    was only ever a proxy for it, and in this position a poor one.
    """
    if not value:
        return '""'
    if _reloads_bare(f"probe:\n  - {value}", {"probe": [value]}):
        return value
    return quote_yaml_string(value)


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


GROUNDING_FIELD = "grounding"
GROUNDING_HEAD_FIELDS = ("claim", "source_id")
GROUNDING_ANCHOR_FIELDS = ("pointer", "expected")
GROUNDING_ENTRY_FIELDS = ("claim", "source_id", "quote", "location_hint", "anchor")
# Free-text grounding fields are always emitted in the JSON double-quoted form: claims,
# quotes, hints, pointers and expected values are host prose, and `quote_scalar`'s bare
# character class silently loses leading/trailing spaces and retypes number-, date- and
# bool-shaped values on the way back through `yaml.safe_load`. `source_id` is the one
# grounding scalar left to `quote_scalar`, because it is a validated manifest identifier
# and the workspace already renders bare ids in the `source_ids` list.
GROUNDING_ALWAYS_QUOTED_FIELDS = frozenset({"claim", "quote", "location_hint", "pointer", "expected"})
GROUNDING_ENTRY_LEAD = "  - "
GROUNDING_ENTRY_INDENT = "    "
GROUNDING_NESTED_INDENT = "      "


def quote_grounding_scalar(value: str, *, always_quote: bool = False) -> str:
    """Render one grounding scalar so ``yaml.safe_load`` returns the identical string.

    Delegates to ``quote_scalar`` and forces its JSON double-quoted branch in the two
    cases where the bare form does not round-trip: ``always_quote`` (every free-text
    grounding field, so ``expected: "23.99"`` cannot reload as a float) and values with
    leading or trailing whitespace, which YAML strips from a plain scalar.
    """
    if always_quote or value != value.strip():
        return quote_yaml_string(value)
    return quote_scalar(value)


def _grounding_scalar(index: int, field: str, value: Any, *, path: str = "") -> str:
    """Render one already-canonical grounding scalar, or refuse if it is not a string.

    Grounding scalars are canonically strings by the time they reach the renderer
    (``verify_quotes.grounding_entries`` canonicalizes ``expected`` at load). Refusing a
    non-string here is the guard against the failure this renderer exists to prevent:
    ``str()``-ing a mapping into a YAML string that reloads as the wrong type.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"grounding[{index}].{path}{field} must be a string; canonicalize scalars before "
            f"rendering (got {type(value).__name__})"
        )
    return quote_grounding_scalar(value, always_quote=field in GROUNDING_ALWAYS_QUOTED_FIELDS)


def render_grounding_entry(index: int, entry: dict[str, Any]) -> list[str]:
    """Render one grounding entry in canonical key order.

    ``claim``, ``source_id``, then exactly one form: ``quote`` (optionally followed by
    ``location_hint``) or a nested ``anchor`` block carrying ``pointer`` then ``expected``.
    Keys whose value is ``None`` count as absent.

    This is a serializer for an already-validated entry, not a validator: it refuses a
    malformed entry with ``ValueError`` rather than emitting bytes that would reload as a
    different shape. Callers validate entry shape first (``verify_quotes.grounding_entries``
    owns those rules and their stable ``GROUNDING_INVALID`` code).
    """
    fields = {field: value for field, value in entry.items() if value is not None}
    unknown = sorted(str(field) for field in fields if field not in GROUNDING_ENTRY_FIELDS)
    if unknown:
        raise ValueError(f"grounding[{index}] has unsupported key(s): {', '.join(unknown)}")

    lines: list[str] = []
    indent = GROUNDING_ENTRY_LEAD
    for field in GROUNDING_HEAD_FIELDS:
        if field not in fields:
            raise ValueError(f"grounding[{index}] is missing required {field}")
        lines.append(f"{indent}{field}: {_grounding_scalar(index, field, fields[field])}")
        indent = GROUNDING_ENTRY_INDENT

    has_quote = "quote" in fields
    has_anchor = "anchor" in fields
    if has_quote and has_anchor:
        raise ValueError(f"grounding[{index}] carries both forms; an entry must carry exactly one of quote or anchor")
    if not has_quote and not has_anchor:
        raise ValueError(f"grounding[{index}] carries no form; an entry must carry exactly one of quote or anchor")

    if has_quote:
        lines.append(f"{indent}quote: {_grounding_scalar(index, 'quote', fields['quote'])}")
        if "location_hint" in fields:
            hint = _grounding_scalar(index, "location_hint", fields["location_hint"])
            lines.append(f"{indent}location_hint: {hint}")
        return lines

    if "location_hint" in fields:
        # location_hint anchors a quote inside the rendered body; it is meaningless beside a
        # pointer into the structured view, so emitting it would record a claim about nothing.
        raise ValueError(f"grounding[{index}] cannot carry location_hint beside anchor")
    anchor = fields["anchor"]
    if not isinstance(anchor, dict):
        raise ValueError(f"grounding[{index}].anchor must be a mapping, got {type(anchor).__name__}")
    anchor_fields = {field: value for field, value in anchor.items() if value is not None}
    unknown_anchor = sorted(str(field) for field in anchor_fields if field not in GROUNDING_ANCHOR_FIELDS)
    if unknown_anchor:
        raise ValueError(f"grounding[{index}].anchor has unsupported key(s): {', '.join(unknown_anchor)}")
    lines.append(f"{indent}anchor:")
    for field in GROUNDING_ANCHOR_FIELDS:
        if field not in anchor_fields:
            raise ValueError(f"grounding[{index}].anchor is missing required {field}")
        rendered = _grounding_scalar(index, field, anchor_fields[field], path="anchor.")
        lines.append(f"{GROUNDING_NESTED_INDENT}{field}: {rendered}")
    return lines


def render_grounding_sequence(key: str, entries: list[dict[str, Any]]) -> list[str]:
    """Render the canonical `grounding` block — the one nested mapping sequence this schema has.

    `render_mapping_sequence` cannot serve grounding: it hardcodes two indentation levels
    and stringifies every value, so a nested `anchor` mapping would be written as a Python
    repr inside a YAML string and reload silently as the wrong type.

    The emitted bytes are normative. Hosts that hand-edit `grounding` must match them
    exactly, and the write path is specified against them.
    """
    lines = [f"{key}:"]
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"grounding[{index}] must be a mapping, got {type(entry).__name__}")
        lines.extend(render_grounding_entry(index, entry))
    return lines


def _grounding_file_invalid(path: Path, message: str) -> ResolveError:
    return ResolveError(
        EXIT_INVALID,
        "GROUNDING_FILE_INVALID",
        f"grounding file {path}: {message}",
        details={"grounding_file": str(path)},
    )


def read_grounding_document(value: str) -> tuple[Path, list[Any]]:
    """Read a grounding file down to its raw entry list, or refuse with a file-level code.

    Accepted shapes are a mapping with a top-level ``grounding:`` list, or a bare list —
    JSON is a subset of YAML, so a host that prefers JSON needs no second code path.
    Everything that can be decided about the *file* (unreadable, not YAML, not one of the
    two shapes) is refused here with ``GROUNDING_FILE_INVALID``. Everything that is a
    statement about an *entry* stays with ``grounding_entries`` and its ``GROUNDING_INVALID``,
    so the two write paths and the page reader never drift on entry shape.
    """
    path = Path(value).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _grounding_file_invalid(path, f"cannot be read ({exc.strerror or exc})") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _grounding_file_invalid(path, f"is not valid YAML ({exc})") from exc
    if isinstance(document, dict):
        if "grounding" not in document:
            raise _grounding_file_invalid(path, "is a mapping without a top-level 'grounding' key")
        raw = document["grounding"]
    else:
        raw = document
    # `grounding: ` with nothing after it loads as None. That is an unfinished edit, not the
    # explicit empty set a host writes as `grounding: []`, so it is refused rather than read
    # as "clear this question's grounding".
    if not isinstance(raw, list):
        raise _grounding_file_invalid(
            path,
            f"must carry a list of grounding entries, got {type(raw).__name__}",
        )
    return path, raw


def load_grounding_file(value: str, slug: str) -> list[dict[str, Any]]:
    """Read and validate a grounding file into canonical, renderable entries.

    Entry shape is validated by ``verify_quotes.grounding_entries``, the one owner of those
    rules, and the *validated* entries are what gets rendered — not the file's raw mappings.
    That choice matters twice. It canonicalizes on the way in (``expected: 23.99`` becomes
    the string ``"23.99"``, whitespace is trimmed, an empty ``location_hint`` disappears), so
    two files differing only in how their author quoted a value write identical bytes. And it
    means a host's formatting never reaches the page.

    ``grounding_entries`` tags each entry with ``form``, which is a report field rather than
    a frontmatter one — ``render_grounding_entry`` rejects it as an unknown key. It is
    stripped here, at the single point where validated entries become writable ones. Keys
    outside the entry schema are dropped for the same reason: the schema is what the
    canonical serialization can express.
    """
    verify_quotes = load_sibling_module("verify_quotes")
    path, raw = read_grounding_document(value)
    try:
        validated = verify_quotes.grounding_entries({"grounding": raw}, slug)
    except verify_quotes.VerifyQuotesError as exc:
        raise ResolveError(
            EXIT_INVALID,
            exc.error_code,
            f"grounding file {path}: {exc}",
            details={"grounding_file": str(path), **getattr(exc, "details", {})},
        ) from exc
    return [
        {field: entry[field] for field in GROUNDING_ENTRY_FIELDS if field in entry}
        for entry in validated
    ]


def grounding_source_ids(entries: list[dict[str, Any]]) -> list[str]:
    """The distinct manifest ids the entries cite, in first-cited order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        source_id = entry.get("source_id")
        if isinstance(source_id, str) and source_id not in seen:
            seen.add(source_id)
            ordered.append(source_id)
    return ordered


def grounding_form_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    """Forms breakdown for the result envelope.

    The form vocabulary and the zero-initialized shape both come from the verifier, so a
    ``grounding set`` envelope and a verification report never disagree about what forms
    exist or how an empty set is spelled. Counted here rather than through
    ``count_by_form``, which reads a ``form`` tag off a *result*: these are entries on
    their way to the page and carry no tag, and synthesizing one per entry just to have
    it read straight back out said less than the count it was hiding.
    """
    verify_quotes = load_sibling_module("verify_quotes")
    counts = dict.fromkeys(verify_quotes.GROUNDING_FORMS, 0)
    for entry in entries:
        form = (
            verify_quotes.GROUNDING_FORM_ANCHOR
            if entry.get("anchor") is not None
            else verify_quotes.GROUNDING_FORM_QUOTE
        )
        counts[form] += 1
    return counts


def render_frontmatter_value(
    key: str,
    value: str | bool | list[str] | list[dict[str, Any]],
    quote: bool,
) -> list[str]:
    if isinstance(value, list):
        if not value:
            return [f"{key}: []"]
        if key == GROUNDING_FIELD:
            return render_grounding_sequence(key, value)
        if all(isinstance(item, dict) for item in value):
            return render_mapping_sequence(key, value)
        return [f"{key}:"] + [f"  - {quote_sequence_item(str(item))}" for item in value]
    if isinstance(value, bool):
        return [f"{key}: {'true' if value else 'false'}"]
    rendered = f'"{value}"' if quote else quote_scalar(value)
    return [f"{key}: {rendered}"]


def set_frontmatter_field_block(
    lines: list[str],
    key: str,
    value: str | bool | list[str] | list[dict[str, Any]],
    *,
    quote: bool = False,
) -> list[str]:
    lines = remove_frontmatter_field_block(lines, key)
    return [*lines, *render_frontmatter_value(key, value, quote)]


def apply_resolution_edits(
    text: str,
    set_fields: dict[str, str | bool | list[str] | list[dict[str, Any]]],
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
        try:
            frontmatter_lines = set_frontmatter_field_block(
                frontmatter_lines,
                key,
                value,
                quote=key in (quoted_fields or set()),
            )
        except ValueError as exc:
            # The grounding renderer refuses a malformed entry rather than emit bytes that
            # would reload as a different shape. That is a serializer-misuse signal, and the
            # single choke point where both write paths turn it into a host-facing refusal —
            # never a traceback — before anything reaches the page.
            raise ResolveError(
                EXIT_INVALID,
                "GROUNDING_INVALID" if key == GROUNDING_FIELD else "PAGE_INVALID",
                f"cannot serialize {key} into question frontmatter: {exc}",
                details={"field": key},
            ) from exc
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
        # Which code tops the envelope is the verifier's rule, not a second copy of it here:
        # all-quote failures keep `GROUNDING_QUOTE_INVALID` bit-for-bit, because hosts switch
        # on it, and one anchor failure raises `GROUNDING_ANCHOR_INVALID` so a caller is not
        # sent looking for a quote there is none of. `details` enumerates every failure
        # either way, so a mixed set is fully described whichever code names it.
        raise ResolveError(
            EXIT_INVALID,
            verify_quotes.grounding_failure_error_code(failed),
            f"answer resolution for {slug} has {len(failed)} grounding verification failure(s)",
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
        # The file is read and validated before anything is verified or written, so a bad
        # file refuses at the same point a bad --source-id does: with the page untouched.
        file_entries: list[dict[str, Any]] | None = None
        grounding_frontmatter = frontmatter
        if getattr(args, "grounding_file", None):
            file_entries = load_grounding_file(args.grounding_file, args.slug.strip())
            validate_source_ids(project_root, config, grounding_source_ids(file_entries))
            # What --require-grounding must verify is what this answer is about to record,
            # not whatever the page still holds from a previous cycle.
            grounding_frontmatter = {**frontmatter, GROUNDING_FIELD: file_entries}
        grounding_report = None
        if getattr(args, "require_grounding", False):
            grounding_report = enforce_grounding(project_root, config, args.slug.strip(), grounding_frontmatter)
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
        remove_fields: tuple[str, ...] = ()
        if file_entries is not None:
            # One dict, one apply_resolution_edits, one lock, one atomic write: the page
            # never exists in a state where the new grounding is present and the status is
            # still the old one.
            fields[GROUNDING_FIELD] = file_entries
            remove_fields = GROUNDING_VERIFICATION_STAMPS
        return {
            "status": status,
            "fields": fields,
            "request_ids": [],
            "source_ids": source_ids,
            "grounding": grounding_report,
            "remove_fields": remove_fields,
            "grounding_entries": file_entries,
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
        # A resolution that also rewrites grounding drops the stamps that attested the
        # previous entries; nothing else this verb does clears them.
        remove_fields = (*remove_fields, *resolution.get("remove_fields", ()))
        updated = apply_resolution_edits(
            text,
            fields,
            remove_fields,
            quoted_fields={"updated", "human_review_requested_at"},
        )
        question_claim.write_page_atomic(page_path, updated)
        result: dict[str, Any] = {
            "applied": True,
            "status": resolution["status"],
            "previous_holder": previous_holder,
            "answer_page": fields.get("answer_page"),
            "source_ids": fields.get("source_ids", []),
            "request_ids": resolution["request_ids"],
        }
        entries = resolution.get("grounding_entries")
        if entries is not None:
            result["grounding_count"] = len(entries)
            result["by_form"] = grounding_form_counts(entries)
        return result


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
        request_records = validate_request_records(
            project_root, config, slug, unique_nonempty(args.request_id, "--request-id")
        )
        request_ids = [str(record["request_id"]) for record in request_records]
        # Structured pairing runs last among the refusals: the cheaper, more fundamental
        # checks above (delegation, manifest membership, normalization) fail first, and this
        # still fails closed before the page is written. It is orthogonal to delegation and
        # runs in both modes.
        pairs = compute_request_source_pairs(
            request_records,
            source_ids,
            source_scope_resolver(project_root, config),
        )
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
            "pairs": pairs,
        }


def transition_grounding_set(
    page_path: Path,
    project_root: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Replace a question's grounding block from a file, without resolving the question.

    This exists so a host never hand-edits question frontmatter. Hand-editing means
    round-tripping a page through some other YAML dumper — reordering keys, retyping dates,
    losing the canonical grounding layout — on a file this package also writes under a lock.
    Two writers and one file is how that ends.

    The block is **replaced**, never merged. Grounding is authored as a set for one answer;
    merging two sets invites duplicate claims in an order nobody chose. Replacement also
    invalidates any verifier stamp on the page, so those are dropped in the same write.

    Verification is deliberately not performed here (CR-7 §7.2). The two-step flow writes
    grounding while cited evidence may still be normalizing, and ``verify_quotes.py --slug S``
    already *is* the check step — a second spelling of it would be a second door every
    future change to verification semantics had to remember.

    **Terminal questions are not rewritten**, by the same ``enforce_claim`` gate the
    resolution verbs use: once a question is ``answered`` its grounding is part of a
    recorded answer, and silently swapping the evidence under an answer that has already
    been verified, reviewed, or exported is the thing terminal statuses exist to prevent.
    The consequence is worth stating plainly, because this command exists to stop hosts
    editing frontmatter: correcting the grounding of an answered question is a *reopen*
    cycle (``reopen`` for a blocked question, otherwise a new question), not an edit —
    and never a hand-edit of the page, which drops the audit trail this path keeps.
    """
    question_claim = load_sibling_module("question_claim")
    slug = args.slug.strip()
    agent_id = args.agent_id.strip()
    # Read and validate the file before taking the lock: a malformed file is a statement
    # about the file, and holding a question lock to discover it helps nobody.
    entries = load_grounding_file(args.from_file, slug)
    with question_claim.question_lock(page_path):
        text = page_path.read_text(encoding="utf-8")
        parts = question_claim.split_frontmatter_lines(text)
        if parts is None:
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
        frontmatter = question_claim.frontmatter_mapping(parts[0])
        if frontmatter.get("type") != "question":
            raise ResolveError(EXIT_INVALID, "PAGE_INVALID", "page is not a question task record")
        # Identical gate to the resolution verbs: terminal statuses are not rewritten
        # (STATUS_NOT_RESOLVABLE), and grounding is never written over another agent's claim.
        holder = enforce_claim(frontmatter, slug, agent_id, args.allow_unclaimed)
        source_ids = validate_source_ids(project_root, config, grounding_source_ids(entries))
        now = question_claim.timestamp_utc()
        updated = apply_resolution_edits(
            text,
            {GROUNDING_FIELD: entries, "updated": now.split("T", 1)[0]},
            GROUNDING_VERIFICATION_STAMPS,
            quoted_fields={"updated"},
        )
        question_claim.write_page_atomic(page_path, updated)
        return {
            "applied": True,
            # The question's own lifecycle is untouched; reporting the status it still has
            # keeps this envelope readable beside the resolution verbs' envelopes.
            "status": frontmatter.get("status"),
            # Same key the resolution verbs report, so a host reads one envelope shape — but
            # this verb does not release the claim, so the holder named here is still current.
            "previous_holder": holder,
            "answer_page": None,
            "source_ids": source_ids,
            "request_ids": [],
            "grounding_count": len(entries),
            "by_form": grounding_form_counts(entries),
            "verification": GROUNDING_VERIFICATION_NOT_PERFORMED,
            "verification_remediation": (
                f"Run verify_quotes.py --slug {slug} to verify these grounding entries against "
                "normalized source records."
            ),
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
    if action == "reopen":
        headline = "reopened"
    elif action == GROUNDING_SET_ACTION:
        # The status did not change; naming it in the headline would read as if it had.
        headline = "grounding recorded"
    else:
        headline = result["status"]
    lines = [
        f"## [{date_text}] resolve | Question {headline}",
        "",
        f"- Question: `{slug}` ({action}).",
        f"- Agent: {agent_id}.",
    ]
    if action == GROUNDING_SET_ACTION:
        forms = result.get("by_form", {})
        rendered_forms = ", ".join(f"{form}: {count}" for form, count in forms.items())
        lines.append(f"- Grounding entries: {result.get('grounding_count', 0)} ({rendered_forms}).")
        if result.get("source_ids"):
            lines.append(f"- Cited sources: {', '.join(result['source_ids'])}.")
        lines.append(f"- Verification: {result.get('verification')}. {result.get('verification_remediation')}")
    if action == "reopen" and result.get("source_ids"):
        lines.append(f"- Reopened with sources: {', '.join(result['source_ids'])}.")
    if action == "reopen" and result.get("pairs"):
        rendered = ", ".join(f"{pair['request_id']} -> {pair['source_id']}" for pair in result["pairs"])
        lines.append(f"- Paired by declared scope: {rendered}.")
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
    if action == "reopen":
        # Always present on reopen, empty when nothing declared a scope, so a host can
        # read `pairs` unconditionally instead of zipping the two repeatable flags.
        report["pairs"] = result.get("pairs", [])
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
    if "grounding_count" in result:
        # Present for both write paths, so a host reads one shape whether grounding arrived
        # with an answer or on its own.
        report["grounding_count"] = result["grounding_count"]
        report["by_form"] = result.get("by_form", {})
    if action == GROUNDING_SET_ACTION:
        # Named explicitly rather than left to be inferred from a missing key: this command
        # writes grounding it has not checked, and the envelope says so and says what checks it.
        report["verification"] = result.get("verification")
        report["remediation"] = result.get("verification_remediation")
    return report


def render_text_report(report: dict[str, Any]) -> str:
    if report.get("action") == GROUNDING_SET_ACTION:
        forms = report.get("by_form", {})
        rendered_forms = ", ".join(f"{form}: {count}" for form, count in forms.items())
        return (
            f"grounding set: {report['slug']} ({report.get('grounding_count', 0)} entries; {rendered_forms}; "
            f"verification {report.get('verification')})\n"
        )
    return f"{report['status']}: {report['slug']}\n"


def resolved_project_root(value: str | Path) -> Path:
    """Resolve a caller-supplied project root the way this command always has."""
    return Path(value).expanduser().resolve()


def resolved_principal(args: argparse.Namespace) -> str:
    """Return the principal this verb records, which is not always ``--agent-id``."""
    if args.command in PRINCIPAL_FLAGS:
        return getattr(args, PRINCIPAL_FLAGS[args.command][1]).strip()
    return args.agent_id.strip()


def resolved_action(args: argparse.Namespace) -> str:
    """Return the action string a report and a log entry carry.

    ``grounding`` is the one nested command, so the action is the full spelling a
    host typed; dispatch still switches on ``args.command`` alone.
    """
    if args.command == GROUNDING_COMMAND:
        return f"{args.command} {args.grounding_command}"
    return args.command


def _run_command(project_root: str | Path, args: argparse.Namespace) -> dict[str, Any]:
    """Apply one resolution verb and return the report ``main`` prints under ``--format json``.

    This is the body every ``run_*`` seam below shares. The seams differ only in
    which flags they accept, so they each assemble the namespace this function
    dispatches on and hand it over — which is also the namespace ``argparse``
    produces, so the CLI and a host drive one implementation, not two.

    Refusals are raised as ``ScriptRefusal`` rather than printed. The four refusal
    families this command has each keep the exit code they always returned:
    ``DelegationGateError`` and ``LockUnavailableError`` are invalid-usage refusals
    (the gate error carries no exit code of its own and never did), while
    ``ClaimError`` and ``ResolveError`` carry theirs, which is how a claim conflict
    stays exit 3 and everything else stays exit 2.

    **The ``log.md`` append is inside this seam**, in the position it has always
    occupied: after the page is written, before the report is built. Resolving a
    question is a workspace mutation, and the audit entry is part of that mutation
    rather than part of printing it. Leaving the append in ``main`` would mean a
    host that answers or blocks a question in-process rewrites the page and writes
    no audit entry, while the CLI doing the same thing writes one — the trail this
    package exists to guarantee would then record only the callers who came
    through the command line. CR-6 AC-1 requires the two doors to produce
    byte-identical workspace state and audit entries, and this is where that is
    either true or not.
    """
    root = resolved_project_root(project_root)
    slug = args.slug.strip()
    agent_id = resolved_principal(args)
    action = resolved_action(args)
    # Bound before the `try` so the ClaimError handler below can name the class. The module
    # loader is cached, so this is the same object the body uses.
    question_claim = load_sibling_module("question_claim")
    try:
        if not agent_id:
            label = PRINCIPAL_FLAGS.get(args.command, ("--agent-id",))[0]
            error_code = "REVIEWER_INVALID" if args.command in PRINCIPAL_FLAGS else "AGENT_ID_INVALID"
            raise ResolveError(EXIT_INVALID, error_code, f"{label} must be a non-empty string")
        question_status = load_sibling_module("question_status")
        config = question_status.load_config(root)
        page_path = question_claim.question_page_path(root, slug)
        if args.command == "reopen":
            result = transition_reopen(page_path, root, config, args)
        elif args.command == "approve":
            result = transition_approve(page_path, args)
        elif args.command == "review":
            result = transition_review(page_path, args)
        elif args.command == GROUNDING_COMMAND:
            # Explicit, not left to the trailing `else`: that branch funnels anything
            # unrecognized into transition_resolution, which would then fail on an attribute
            # a grounding namespace does not have.
            result = transition_grounding_set(page_path, root, config, args)
        else:
            result = transition_resolution(page_path, root, config, args)
    except DelegationGateError as error:
        details = {"action": action, "slug": slug, "agent_id": agent_id}
        details.update(error.details)
        raise ScriptRefusal(
            error.error_code,
            error.message,
            exit_code=EXIT_INVALID,
            remediation=error.remediation,
            details=details,
        ) from error
    except question_claim.ClaimError as error:
        # `question_page_path` refuses an unknown or malformed slug with a ClaimError, and
        # every claim helper this script calls can raise one. Without this clause they reach
        # a host as a traceback rather than the refusal envelope every other refusal uses —
        # which for a host parsing stdout as JSON is indistinguishable from a crash. It is
        # itself a ScriptRefusal now, but of the sibling module's own class and without this
        # command's details, so it is re-raised in this command's shape rather than passed on.
        raise ScriptRefusal(
            error.error_code,
            str(error),
            exit_code=error.exit_code,
            details={"action": action, "slug": slug, "agent_id": agent_id},
        ) from error
    except ResolveError as error:
        details = {"action": action, "slug": slug, "agent_id": agent_id}
        details.update(error.details)
        raise ScriptRefusal(
            error.error_code,
            str(error),
            exit_code=error.exit_code,
            details=details,
        ) from error
    except LockUnavailableError as error:
        raise ScriptRefusal(
            error.error_code,
            str(error),
            exit_code=EXIT_INVALID,
            details={"action": action, "slug": slug, "agent_id": agent_id, **error.details},
        ) from error
    except SystemExit as exc:
        # An unreadable workspace reaches here as SystemExit(str); from_system_exit
        # re-raises anything else untouched.
        raise ScriptRefusal.from_system_exit(exc, exit_code=EXIT_INVALID) from exc

    try:
        question_claim.append_log_entry(root / "log.md", render_log(action, slug, agent_id, result))
    except LockUnavailableError as error:
        raise ScriptRefusal(
            error.error_code,
            str(error),
            exit_code=EXIT_INVALID,
            details={"action": action, "slug": slug, "agent_id": agent_id, **error.details},
        ) from error
    return build_report(action, slug, agent_id, page_path, root, result)


def run_answer(
    project_root: str | Path,
    *,
    slug: str,
    agent_id: str,
    answer_page: str,
    source_id: list[str] | None = None,
    allow_uncited: bool = False,
    allow_unclaimed: bool = False,
    confidence: str | None = None,
    evidence_strength: str | None = None,
    require_coverage: bool = False,
    require_grounding: bool = False,
    coverage_manifest: str | None = None,
    grounding_file: str | None = None,
) -> dict[str, Any]:
    """Resolve a question as answered. Keyword arguments mirror the ``answer`` flags one for one."""
    return _run_command(
        project_root,
        argparse.Namespace(
            command="answer",
            slug=slug,
            agent_id=agent_id,
            allow_unclaimed=allow_unclaimed,
            answer_page=answer_page,
            source_id=source_id,
            allow_uncited=allow_uncited,
            confidence=confidence,
            evidence_strength=evidence_strength,
            require_coverage=require_coverage,
            require_grounding=require_grounding,
            coverage_manifest=coverage_manifest,
            grounding_file=grounding_file,
        ),
    )


def run_block(
    project_root: str | Path,
    *,
    slug: str,
    agent_id: str,
    blocked_reason: str,
    request_id: list[str] | None = None,
    allow_unclaimed: bool = False,
) -> dict[str, Any]:
    """Resolve a question as blocked on missing evidence."""
    return _run_command(
        project_root,
        argparse.Namespace(
            command="block",
            slug=slug,
            agent_id=agent_id,
            allow_unclaimed=allow_unclaimed,
            blocked_reason=blocked_reason,
            request_id=request_id,
        ),
    )


def run_defer(
    project_root: str | Path,
    *,
    slug: str,
    agent_id: str,
    reason: str,
    allow_unclaimed: bool = False,
) -> dict[str, Any]:
    """Resolve a question as deferred."""
    return _run_command(
        project_root,
        argparse.Namespace(
            command="defer",
            slug=slug,
            agent_id=agent_id,
            allow_unclaimed=allow_unclaimed,
            reason=reason,
        ),
    )


def run_reject(
    project_root: str | Path,
    *,
    slug: str,
    agent_id: str,
    reason: str,
    allow_unclaimed: bool = False,
) -> dict[str, Any]:
    """Resolve a question as rejected."""
    return _run_command(
        project_root,
        argparse.Namespace(
            command="reject",
            slug=slug,
            agent_id=agent_id,
            allow_unclaimed=allow_unclaimed,
            reason=reason,
        ),
    )


def run_reopen(
    project_root: str | Path,
    *,
    slug: str,
    agent_id: str,
    source_id: list[str],
    request_id: list[str] | None = None,
) -> dict[str, Any]:
    """Move a blocked question back to open once its evidence is delivered and normalized.

    ``reopen`` has no ``--allow-unclaimed``: a blocked question is never claimed.
    """
    return _run_command(
        project_root,
        argparse.Namespace(
            command="reopen",
            slug=slug,
            agent_id=agent_id,
            source_id=source_id,
            request_id=request_id,
        ),
    )


def run_approve(project_root: str | Path, *, slug: str, reviewer: str) -> dict[str, Any]:
    """Approve every policy still pending human review, in one call.

    The recorded principal is ``reviewer``, not an agent id — the same trust model
    the CLI has: this authenticates nobody, and the audit trail is the frontmatter
    entry plus ``log.md``.
    """
    return _run_command(
        project_root,
        argparse.Namespace(command="approve", slug=slug, reviewer=reviewer),
    )


def run_review(
    project_root: str | Path,
    *,
    slug: str,
    policy: str,
    verdict: str,
    reviewed_by: str,
    review_ref: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record one per-policy human review collected inside or outside the workspace."""
    return _run_command(
        project_root,
        argparse.Namespace(
            command="review",
            slug=slug,
            policy=policy,
            verdict=verdict,
            reviewed_by=reviewed_by,
            review_ref=review_ref,
            note=note,
        ),
    )


def run_grounding_set(
    project_root: str | Path,
    *,
    slug: str,
    agent_id: str,
    from_file: str,
    allow_unclaimed: bool = False,
) -> dict[str, Any]:
    """Replace a question's whole grounding block from a file, without resolving it."""
    return _run_command(
        project_root,
        argparse.Namespace(
            command=GROUNDING_COMMAND,
            grounding_command="set",
            slug=slug,
            agent_id=agent_id,
            from_file=from_file,
            allow_unclaimed=allow_unclaimed,
        ),
    )


def dispatch_seam(args: argparse.Namespace) -> dict[str, Any]:
    """Call the seam that matches the parsed command, flag for flag.

    ``main`` goes through the public seams rather than straight to ``_run_command``
    on purpose: it makes the CLI one more caller of the library API instead of a
    parallel path, so the existing CLI suites exercise the seams' argument mapping
    too, and a seam whose keyword does not reach the transition fails loudly here
    rather than only in the conformance harness.
    """
    if args.command == "answer":
        return run_answer(
            args.project_root,
            slug=args.slug,
            agent_id=args.agent_id,
            answer_page=args.answer_page,
            source_id=args.source_id,
            allow_uncited=args.allow_uncited,
            allow_unclaimed=args.allow_unclaimed,
            confidence=args.confidence,
            evidence_strength=args.evidence_strength,
            require_coverage=args.require_coverage,
            require_grounding=args.require_grounding,
            coverage_manifest=args.coverage_manifest,
            grounding_file=args.grounding_file,
        )
    if args.command == "block":
        return run_block(
            args.project_root,
            slug=args.slug,
            agent_id=args.agent_id,
            blocked_reason=args.blocked_reason,
            request_id=args.request_id,
            allow_unclaimed=args.allow_unclaimed,
        )
    if args.command == "defer":
        return run_defer(
            args.project_root,
            slug=args.slug,
            agent_id=args.agent_id,
            reason=args.reason,
            allow_unclaimed=args.allow_unclaimed,
        )
    if args.command == "reject":
        return run_reject(
            args.project_root,
            slug=args.slug,
            agent_id=args.agent_id,
            reason=args.reason,
            allow_unclaimed=args.allow_unclaimed,
        )
    if args.command == "reopen":
        return run_reopen(
            args.project_root,
            slug=args.slug,
            agent_id=args.agent_id,
            source_id=args.source_id,
            request_id=args.request_id,
        )
    if args.command == "approve":
        return run_approve(args.project_root, slug=args.slug, reviewer=args.reviewer)
    if args.command == "review":
        return run_review(
            args.project_root,
            slug=args.slug,
            policy=args.policy,
            verdict=args.verdict,
            reviewed_by=args.reviewed_by,
            review_ref=args.review_ref,
            note=args.note,
        )
    if args.command == GROUNDING_COMMAND:
        return run_grounding_set(
            args.project_root,
            slug=args.slug,
            agent_id=args.agent_id,
            from_file=args.from_file,
            allow_unclaimed=args.allow_unclaimed,
        )
    # argparse rejects anything else before this point; `_run_command` refuses the same
    # way `main` always did if a future subcommand ever forgets to add itself here.
    return _run_command(args.project_root, args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    try:
        report = dispatch_seam(args)
    except ScriptRefusal as refusal:
        return emit_refusal(refusal, json_mode=json_mode)

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text_report(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
