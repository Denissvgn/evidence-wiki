#!/usr/bin/env python3
"""Archive old terminal run-controller directories without touching workspace data."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_locks import LockUnavailableError, workspace_lock

SCHEMA_VERSION = "1.0"
TERMINAL_STATES = {"complete", "blocked_on_sources", "no_ship", "failed"}
EXIT_OK = 0
EXIT_INVALID = 2


def parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive old terminal run-controller directories.")
    parser.add_argument("--project-root", default=".", help="Research workspace root. Defaults to current directory.")
    parser.add_argument(
        "--older-than-days",
        type=parse_positive_int,
        default=30,
        help="Archive terminal runs last updated at least this many days ago. Defaults to 30.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually archive eligible runs. Default is dry-run.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format. Defaults to text.")
    return parser.parse_args(argv)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def relative_workspace_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def load_run_state(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "run-state.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def normalize_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime for deterministic retention calculations."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def eligible_runs(
    project_root: Path,
    older_than_days: int,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    runs_root = project_root / "runs"
    evaluated_at = normalize_utc(now or datetime.now(timezone.utc))
    cutoff = evaluated_at - timedelta(days=older_than_days)
    actions: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return actions
    archive_root = runs_root / "archive"
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir() or run_dir == archive_root:
            continue
        document = load_run_state(run_dir)
        if document is None:
            continue
        state = document.get("state") if isinstance(document.get("state"), dict) else {}
        current = state.get("current")
        updated_at = parse_timestamp(document.get("updated_at"))
        if current not in TERMINAL_STATES or updated_at is None or updated_at > cutoff:
            continue
        archive_path = archive_root / f"{run_dir.name}.tar.gz"
        if archive_path.exists():
            continue
        actions.append(
            {
                "run_id": run_dir.name,
                "state": current,
                "updated_at": document.get("updated_at"),
                "run_path": relative_workspace_path(project_root, run_dir),
                "archive_path": relative_workspace_path(project_root, archive_path),
            }
        )
    return actions


def run_lock_path(run_dir: Path) -> Path:
    return run_dir / ".locks" / "run-state.lock"


def run_is_still_eligible(run_dir: Path, older_than_days: int, evaluated_at: datetime) -> bool:
    document = load_run_state(run_dir)
    if document is None:
        return False
    state = document.get("state") if isinstance(document.get("state"), dict) else {}
    updated_at = parse_timestamp(document.get("updated_at"))
    cutoff = normalize_utc(evaluated_at) - timedelta(days=older_than_days)
    return state.get("current") in TERMINAL_STATES and updated_at is not None and updated_at <= cutoff


def exclude_runtime_locks(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Exclude ephemeral lock state, which may be unreadable while held on Windows."""
    if ".locks" in PurePosixPath(member.name).parts:
        return None
    return member


def publish_archive_without_overwrite(run_dir: Path, archive_path: Path) -> bool:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.",
        suffix=".tmp",
        dir=archive_path.parent,
    )
    os.close(descriptor)
    tmp_path = Path(tmp_name)
    try:
        with tmp_path.open("r+b") as output:
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                archive.add(run_dir, arcname=run_dir.name, filter=exclude_runtime_locks)
            output.flush()
            os.fsync(output.fileno())
        try:
            # A same-directory hard link publishes the complete archive atomically
            # and refuses an existing destination on POSIX and Windows.
            os.link(tmp_path, archive_path)
        except FileExistsError:
            return False
        return True
    finally:
        tmp_path.unlink(missing_ok=True)


def archive_run(
    project_root: Path,
    action: dict[str, Any],
    older_than_days: int,
    evaluated_at: datetime,
    *,
    lock_timeout_seconds: float = 10.0,
) -> tuple[bool, str | None]:
    run_dir = project_root / str(action["run_path"])
    archive_path = project_root / str(action["archive_path"])
    with workspace_lock(
        run_lock_path(run_dir),
        purpose=f"workspace GC run {action['run_id']}",
        timeout_seconds=lock_timeout_seconds,
    ):
        if not run_is_still_eligible(run_dir, older_than_days, evaluated_at):
            return False, "no_longer_eligible"
        if archive_path.exists():
            return False, "archive_exists"
        if not publish_archive_without_overwrite(run_dir, archive_path):
            return False, "archive_exists"

    # The run lock itself lives inside the terminal run directory. Release it
    # before removal so Windows can close the lock handle; terminal state was
    # revalidated and archived while the lock was held.
    shutil.rmtree(run_dir)
    return True, None


def build_report(
    project_root: Path,
    older_than_days: int,
    apply: bool,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    evaluated_at = normalize_utc(now or datetime.now(timezone.utc))
    actions = eligible_runs(project_root, older_than_days, now=evaluated_at)
    archived = 0
    if apply:
        for action in actions:
            applied, skip_reason = archive_run(
                project_root,
                action,
                older_than_days,
                evaluated_at,
            )
            action["applied"] = applied
            if skip_reason is not None:
                action["skip_reason"] = skip_reason
            if applied:
                archived += 1
    else:
        for action in actions:
            action["applied"] = False
    return {
        "schema_version": SCHEMA_VERSION,
        "project_root": str(project_root),
        "dry_run": not apply,
        "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "older_than_days": older_than_days,
        "counts": {"eligible": len(actions), "archived": archived},
        "actions": actions,
    }


def render_text(report: dict[str, Any]) -> str:
    mode = "dry-run" if report["dry_run"] else "apply"
    lines = [
        "Workspace GC",
        "============",
        f"Project root: {report['project_root']}",
        f"Mode: {mode}",
        f"Eligible terminal runs: {report['counts']['eligible']}",
        f"Archived: {report['counts']['archived']}",
    ]
    for action in report["actions"]:
        lines.append(f"- {action['run_id']} -> {action['archive_path']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        report = build_report(project_root, args.older_than_days, args.apply)
    except (LockUnavailableError, OSError, tarfile.TarError) as exc:
        error_code = getattr(exc, "error_code", "WORKSPACE_UNREADABLE")
        if args.format == "json":
            payload = {"schema_version": SCHEMA_VERSION, "error_code": error_code, "message": str(exc)}
            remediation = getattr(exc, "remediation", None)
            if remediation:
                payload["remediation"] = remediation
            print(json.dumps(payload))
        else:
            print(f"workspace-gc failed: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(render_text(report), end="")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
