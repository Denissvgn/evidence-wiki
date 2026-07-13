#!/usr/bin/env python3
"""Claim and release question task records for unattended research runs.

Multiple research agents can work one backlog. A claim marks a question
``in_progress`` with ``claimed_by`` (agent identifier) and ``claimed_at``
(quoted ISO 8601 UTC timestamp) so a second agent never duplicates the work
and an orchestrator can audit who holds what. ``question_status.py
--format json`` exposes both fields per question.

Subcommands:

- ``claim --slug SLUG --agent-id ID``: transition ``open`` to ``in_progress``
  and write the claim fields atomically (write-temp-rename under a stable
  per-question workspace lock). Re-claiming a question already held by the
  same agent is an idempotent no-op. A question held by another agent is
  refused with exit 3 and a machine-readable refusal naming the holder.
- ``claim --slug SLUG --agent-id ID --steal --if-older-than HOURS``:
  orchestrator-mediated stale-claim recovery. Transfers the claim only when
  the existing claim is older than HOURS (or its timestamp is unparseable).
  Stealing is never automatic; both flags must be explicit.
- ``release --slug SLUG --agent-id ID``: transition ``in_progress`` back to
  ``open`` and clear the claim fields. Releasing a question claimed by
  another agent is refused with exit 3. Releasing an already-open question
  is an idempotent no-op.

Resolution to ``answered``/``blocked``/``deferred``/``rejected`` is handled by
``question_resolve.py`` under the same stable per-question lock. This script
only manages the claim lifecycle between ``open`` and ``in_progress``.

Exit codes:

- ``0``: transition applied or idempotent no-op.
- ``2``: invalid usage, unknown slug, non-question page, unreadable
  workspace, or a status that cannot be claimed/released.
- ``3``: claim conflict (held by another agent, or stale-steal threshold not
  met).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to manage question claims") from exc


SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_INVALID = 2
EXIT_CONFLICT = 3
LOG_HEADER = "# Research Wiki Activity Log\n\n"

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import emit_error, handle_system_exit, json_mode_requested
from _workspace_locks import LockUnavailableError, workspace_lock
from _workspace_module_loader import load_workspace_module


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Claim and release question task records for multi-agent research runs.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    claim_parser = subparsers.add_parser("claim", help="Claim an open question (open -> in_progress).")
    claim_parser.add_argument("--slug", required=True, help="Question page slug (file name without .md).")
    claim_parser.add_argument("--agent-id", required=True, help="Identifier of the claiming agent.")
    claim_parser.add_argument(
        "--steal",
        action="store_true",
        help="Take over a stale claim held by another agent. Requires --if-older-than.",
    )
    claim_parser.add_argument(
        "--if-older-than",
        type=float,
        default=None,
        metavar="HOURS",
        help="With --steal: only transfer claims older than this many hours.",
    )
    claim_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )

    release_parser = subparsers.add_parser("release", help="Release a claim (in_progress -> open).")
    release_parser.add_argument("--slug", required=True, help="Question page slug (file name without .md).")
    release_parser.add_argument("--agent-id", required=True, help="Identifier of the releasing agent.")
    release_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so its logic is reused directly."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


class ClaimError(Exception):
    """A refused transition with a machine-readable error code."""

    def __init__(self, exit_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.error_code = error_code


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a claimed_at value (quoted string or YAML-parsed datetime) to aware UTC."""
    if hasattr(value, "isoformat") and not isinstance(value, str):
        text = str(value.isoformat())
    elif isinstance(value, str) and value.strip():
        text = value.strip()
    else:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def claim_age_hours(claimed_at: Any) -> float | None:
    parsed = parse_timestamp(claimed_at)
    if parsed is None:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600


def split_frontmatter_lines(text: str) -> tuple[list[str], list[str], list[str]] | None:
    """Split page text into (frontmatter lines, opening fence, rest) preserving bytes.

    Returns None when the page has no well-formed frontmatter block.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return None
    return lines[1:closing], lines[: 1], lines[closing:]


def frontmatter_mapping(frontmatter_lines: list[str]) -> dict[str, Any]:
    try:
        data = yaml.safe_load("\n".join(frontmatter_lines)) or {}
    except yaml.YAMLError as exc:
        raise ClaimError(EXIT_INVALID, "PAGE_INVALID", f"question page has invalid frontmatter YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ClaimError(EXIT_INVALID, "PAGE_INVALID", "question page frontmatter must be a mapping")
    return data


def render_scalar(value: str, quote: bool) -> str:
    return f'"{value}"' if quote else value


def set_frontmatter_field(lines: list[str], key: str, rendered_value: str) -> list[str]:
    """Replace a top-level scalar field line, or append it, preserving all other lines."""
    prefix = f"{key}:"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines = list(lines)
            lines[index] = f"{key}: {rendered_value}"
            return lines
    return [*lines, f"{key}: {rendered_value}"]


def remove_frontmatter_field(lines: list[str], key: str) -> list[str]:
    prefix = f"{key}:"
    return [line for line in lines if not line.startswith(prefix)]


def apply_frontmatter_edits(
    text: str,
    set_fields: dict[str, tuple[str, bool]],
    remove_fields: tuple[str, ...] = (),
) -> str:
    parts = split_frontmatter_lines(text)
    if parts is None:
        raise ClaimError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
    frontmatter_lines, opening, rest = parts
    for key in remove_fields:
        frontmatter_lines = remove_frontmatter_field(frontmatter_lines, key)
    for key, (value, quote) in set_fields.items():
        frontmatter_lines = set_frontmatter_field(frontmatter_lines, key, render_scalar(value, quote))
    return "\n".join([*opening, *frontmatter_lines, *rest])


def write_page_atomic(path: Path, content: str) -> None:
    # Unique temp name so concurrent writers cannot steal each other's temp file;
    # the final rename stays atomic on POSIX (same filesystem).
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:  # pragma: no cover - fsync can be unavailable on unusual filesystems
                pass
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass


def append_log_entry(log_path: Path, entry: str) -> None:
    """Append a rendered log entry under the shared workspace lock."""
    lock_path = log_path.parent / ".locks" / "log.lock"
    with workspace_lock(lock_path, purpose="activity log append"):
        with log_path.open("a+", encoding="utf-8") as handle:
            handle.seek(0)
            content = handle.read()
            if not content:
                prefix = LOG_HEADER
            elif content.endswith("\n\n"):
                prefix = ""
            elif content.endswith("\n"):
                prefix = "\n"
            else:
                prefix = "\n\n"
            handle.seek(0, 2)
            handle.write(prefix + entry + "\n")


def question_page_path(project_root: Path, slug: str) -> Path:
    question_status = load_sibling_module("question_status")
    config = question_status.load_config(project_root)
    questions_dir = question_status.questions_directory(project_root, config)
    clean_slug = slug.strip()
    if not clean_slug or "/" in clean_slug or "\\" in clean_slug or clean_slug.startswith("."):
        raise ClaimError(EXIT_INVALID, "SLUG_INVALID", f"invalid question slug: {slug}")
    path = questions_dir / f"{clean_slug}.md"
    if not path.is_file():
        try:
            label = questions_dir.relative_to(project_root).as_posix()
        except ValueError:
            label = questions_dir.as_posix()
        raise ClaimError(EXIT_INVALID, "SLUG_UNKNOWN", f"unknown question slug: {clean_slug} (no page under {label}/)")
    return path


def question_lock_path(page_path: Path) -> Path:
    return page_path.parent / ".locks" / f"{page_path.stem}.lock"


def question_lock(page_path: Path):
    """Hold a stable per-question lock that survives page temp-file replacement."""
    lock_path = question_lock_path(page_path)
    return workspace_lock(lock_path, purpose=f"question mutation {page_path.stem}")


def holder_block(frontmatter: dict[str, Any]) -> dict[str, Any]:
    claimed_by = frontmatter.get("claimed_by")
    claimed_at = frontmatter.get("claimed_at")
    parsed = parse_timestamp(claimed_at)
    return {
        "claimed_by": claimed_by if isinstance(claimed_by, str) and claimed_by.strip() else None,
        "claimed_at": parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else None,
    }


def transition_claim(page_path: Path, agent_id: str, steal: bool, if_older_than: float | None) -> dict[str, Any]:
    """Apply the claim transition under a stable workspace lock for this question."""
    with question_lock(page_path):
        text = page_path.read_text(encoding="utf-8")
        parts = split_frontmatter_lines(text)
        if parts is None:
            raise ClaimError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
        frontmatter = frontmatter_mapping(parts[0])
        if frontmatter.get("type") != "question":
            raise ClaimError(EXIT_INVALID, "PAGE_INVALID", "page is not a question task record")
        status = frontmatter.get("status")
        holder = holder_block(frontmatter)

        if status == "in_progress":
            if holder["claimed_by"] == agent_id:
                return {"applied": False, "outcome": "already_claimed_by_agent", "holder": holder}
            age = claim_age_hours(frontmatter.get("claimed_at"))
            if not steal:
                raise ClaimError(
                    EXIT_CONFLICT,
                    "CLAIM_HELD",
                    f"question is already claimed by {holder['claimed_by'] or '<unrecorded agent>'} "
                    f"(claimed_at: {holder['claimed_at'] or 'unrecorded'})",
                )
            if if_older_than is None:
                raise ClaimError(EXIT_INVALID, "STEAL_THRESHOLD_REQUIRED", "--steal requires --if-older-than HOURS")
            if age is not None and age < if_older_than:
                raise ClaimError(
                    EXIT_CONFLICT,
                    "CLAIM_NOT_STALE",
                    f"claim held by {holder['claimed_by'] or '<unrecorded agent>'} is {age:.1f}h old; "
                    f"refusing to steal before {if_older_than:g}h",
                )
            outcome = "stolen"
        elif status == "open":
            if steal:
                raise ClaimError(EXIT_INVALID, "STEAL_NOT_APPLICABLE", "--steal applies only to in_progress questions")
            outcome = "claimed"
        else:
            raise ClaimError(
                EXIT_INVALID,
                "STATUS_NOT_CLAIMABLE",
                f"cannot claim a question with status '{status}'; only open (or stale in_progress with --steal)",
            )

        now = timestamp_utc()
        updated = apply_frontmatter_edits(
            text,
            {
                "status": ("in_progress", False),
                "claimed_by": (agent_id, False),
                "claimed_at": (now, True),
                "updated": (now.split("T", 1)[0], False),
            },
        )
        write_page_atomic(page_path, updated)
        return {
            "applied": True,
            "outcome": outcome,
            "previous_holder": holder if outcome == "stolen" else None,
            "holder": {"claimed_by": agent_id, "claimed_at": now},
        }


def transition_release(page_path: Path, agent_id: str) -> dict[str, Any]:
    """Apply the release transition under a stable workspace lock for this question."""
    with question_lock(page_path):
        text = page_path.read_text(encoding="utf-8")
        parts = split_frontmatter_lines(text)
        if parts is None:
            raise ClaimError(EXIT_INVALID, "PAGE_INVALID", "question page has no frontmatter block")
        frontmatter = frontmatter_mapping(parts[0])
        if frontmatter.get("type") != "question":
            raise ClaimError(EXIT_INVALID, "PAGE_INVALID", "page is not a question task record")
        status = frontmatter.get("status")
        holder = holder_block(frontmatter)

        if status == "open" and holder["claimed_by"] is None:
            return {"applied": False, "outcome": "already_open", "holder": holder}
        if status != "in_progress" and status != "open":
            raise ClaimError(
                EXIT_INVALID,
                "STATUS_NOT_RELEASABLE",
                f"cannot release a question with status '{status}'; only in_progress claims are releasable",
            )
        if holder["claimed_by"] is not None and holder["claimed_by"] != agent_id:
            raise ClaimError(
                EXIT_CONFLICT,
                "CLAIM_HELD",
                f"claim is held by {holder['claimed_by']}; agents never downgrade others' claims. "
                "Use claim --steal --if-older-than for orchestrator-mediated recovery.",
            )

        updated = apply_frontmatter_edits(
            text,
            {
                "status": ("open", False),
                "updated": (timestamp_utc().split("T", 1)[0], False),
            },
            remove_fields=("claimed_by", "claimed_at"),
        )
        write_page_atomic(page_path, updated)
        return {"applied": True, "outcome": "released", "previous_holder": holder, "holder": None}


def render_log(action: str, slug: str, agent_id: str, outcome: str) -> str:
    date_text = datetime.now(timezone.utc).date().isoformat()
    return (
        f"## [{date_text}] claim | Question {action}\n\n"
        f"- Question: `{slug}` ({outcome}).\n"
        f"- Agent: {agent_id}.\n"
    )


def build_report(action: str, slug: str, agent_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": True,
        "slug": slug,
        "agent_id": agent_id,
        "applied": result["applied"],
        "outcome": result["outcome"],
        "holder": result.get("holder"),
        "previous_holder": result.get("previous_holder"),
    }


def build_refusal(action: str, slug: str, agent_id: str, error: ClaimError) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": False,
        "slug": slug,
        "agent_id": agent_id,
        "error_code": error.error_code,
        "message": str(error),
    }


def render_text_report(report: dict[str, Any]) -> str:
    holder = report.get("holder")
    holder_text = f" (held by {holder['claimed_by']} since {holder['claimed_at']})" if holder else ""
    return f"{report['outcome']}: {report['slug']}{holder_text}\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    action = args.command
    slug = args.slug.strip()
    agent_id = args.agent_id.strip()
    try:
        if not agent_id:
            raise ClaimError(EXIT_INVALID, "AGENT_ID_INVALID", "--agent-id must be a non-empty string")
        if action == "claim" and args.if_older_than is not None and not args.steal:
            raise ClaimError(EXIT_INVALID, "STEAL_FLAG_REQUIRED", "--if-older-than requires --steal")
        page_path = question_page_path(project_root, slug)
        if action == "claim":
            result = transition_claim(page_path, agent_id, args.steal, args.if_older_than)
        else:
            result = transition_release(page_path, agent_id)
    except ClaimError as error:
        if json_mode:
            emit_error(
                str(error),
                json_mode=True,
                error_code=error.error_code,
                details={"action": action, "slug": slug, "agent_id": agent_id},
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
                details=error.details,
            )
        else:
            print(f"refused ({error.error_code}): {error}", file=sys.stderr)
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    if result["applied"]:
        append_log_entry(project_root / "log.md", render_log(action, slug, agent_id, result["outcome"]))
    report = build_report(action, slug, agent_id, result)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text_report(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
