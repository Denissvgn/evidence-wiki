#!/usr/bin/env python3
"""Explicitly snapshot human-editable workspace changes."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml") from exc


DEFAULT_MESSAGE = "snapshot: user edits"
DEFAULT_STATIC_PATHS = ["index.md", "log.md", "docs", "skills"]
EXCLUDED_PREFIXES = (
    "raw",
    "sources/manifest.jsonl",
    "sources/normalized",
    "sources/cards",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report or commit explicit user-edit snapshots.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional workspace-relative human-editable path to include. Repeat as needed.",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Create a git commit for scoped user-edit changes.",
    )
    parser.add_argument(
        "--message",
        default=DEFAULT_MESSAGE,
        help=f"Commit message to use with --commit. Defaults to {DEFAULT_MESSAGE!r}.",
    )
    return parser.parse_args()


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "research.yml"
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    config = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(config, dict):
        raise SystemExit(f"Invalid config: {config_path}")
    return config


def config_mapping(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"research.yml {key} must be a mapping")
    return value


def require_explicit_snapshot_policy(config: dict[str, Any]) -> None:
    integrations = config_mapping(config, "integrations")
    git_config = integrations.get("git") or {}
    if not isinstance(git_config, dict):
        raise SystemExit("research.yml integrations.git must be a mapping")
    policy = git_config.get("snapshot_user_edits")
    if policy != "explicit":
        raise SystemExit(
            "Refusing to snapshot user edits because research.yml "
            "integrations.git.snapshot_user_edits is not `explicit`."
        )


def normalize_workspace_path(project_root: Path, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"Unsafe workspace path: {value}")
    raw_value = value.strip()
    normalized_text = raw_value.replace("\\", "/")
    parsed = urlparse(normalized_text)
    if "://" in normalized_text or parsed.scheme:
        raise SystemExit(f"Unsafe workspace path: {value}")
    if len(raw_value) >= 2 and raw_value[1] == ":" and raw_value[0].isalpha():
        raise SystemExit(f"Unsafe workspace path: {value}")
    raw_path = Path(value)
    if raw_path.is_absolute():
        resolved = raw_path.resolve()
        try:
            value = resolved.relative_to(project_root).as_posix()
        except ValueError as exc:
            raise SystemExit(f"--path must stay inside project root: {raw_path}") from exc
    normalized = PurePosixPath(value.replace("\\", "/"))
    parts = [part for part in normalized.parts if part not in {"", "."}]
    if not parts or ".." in parts:
        raise SystemExit(f"Unsafe workspace path: {value}")
    path = PurePosixPath(*parts).as_posix()
    if is_excluded(path):
        raise SystemExit(f"Refusing to snapshot raw/generated source path: {path}")
    return path


def normalize_config_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"research.yml {label} must be a non-empty workspace-relative path")
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not a URL: {value}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not an absolute path: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not an absolute path: {value}")
    if ".." in path.parts:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path without '..': {value}")
    return path.as_posix()


def is_excluded(path: str) -> bool:
    normalized = path.rstrip("/")
    return any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in EXCLUDED_PREFIXES)


def human_editable_paths(project_root: Path, config: dict[str, Any], extra_paths: list[str]) -> list[str]:
    wiki_config = config_mapping(config, "wiki")
    wiki_root = normalize_config_path(wiki_config.get("root", "wiki"), "wiki.root")
    paths = [wiki_root]
    paths.extend(normalize_workspace_path(project_root, path) for path in DEFAULT_STATIC_PATHS)
    paths.extend(normalize_workspace_path(project_root, path) for path in extra_paths)

    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path not in seen:
            unique.append(path)
            seen.add(path)
    return unique


def run_git(project_root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - git subcommand is fixed and args are explicit tokens
        ["git", "-C", str(project_root), *args],  # noqa: S607 - git is the intended workspace executable
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
        raise SystemExit(message)
    return result


def scoped_status(project_root: Path, paths: list[str]) -> list[str]:
    result = run_git(project_root, ["status", "--porcelain", "--", *paths])
    return [line for line in result.stdout.splitlines() if line.strip()]


def staged_changes(project_root: Path) -> list[str]:
    result = run_git(project_root, ["diff", "--cached", "--name-only"])
    return [line for line in result.stdout.splitlines() if line.strip()]


def command_text(project_root: Path, git_args: list[str]) -> str:
    return shlex.join(["git", "-C", str(project_root), *git_args])


def print_report(
    project_root: Path,
    paths: list[str],
    status_lines: list[str],
    message: str,
    *,
    dry_run: bool,
) -> None:
    print("User Edit Snapshot")
    print("==================")
    print(f"Project root: {project_root}")
    print("Scoped paths:")
    for path in paths:
        print(f"- {path}")
    print("")

    if not status_lines:
        print("No scoped user edits found.")
        return

    print("Scoped changes:")
    for line in status_lines:
        print(f"- {line}")
    print("")
    if dry_run:
        print("Dry run only; no files were staged or committed.")
        print("To create a snapshot, run:")
        print(command_text(project_root, ["add", "--all", "--", *paths]))
        print(command_text(project_root, ["commit", "-m", message]))
    else:
        print("Snapshot commit requested; staging scoped paths only.")


def commit_snapshot(project_root: Path, paths: list[str], status_lines: list[str], message: str) -> None:
    if not status_lines:
        print("No scoped user edits found; no commit created.")
        return

    already_staged = staged_changes(project_root)
    if already_staged:
        print("Refusing to create a snapshot because staged changes already exist:")
        for path in already_staged:
            print(f"- {path}")
        raise SystemExit("Commit or unstage existing index changes before snapshotting user edits.")

    print_report(project_root, paths, status_lines, message, dry_run=False)
    run_git(project_root, ["add", "--all", "--", *paths])
    post_add_status = staged_changes(project_root)
    if not post_add_status:
        print("No staged changes after scoped add; no commit created.")
        return
    commit_result = run_git(project_root, ["commit", "-m", message])
    if commit_result.stdout.strip():
        print(commit_result.stdout.strip())


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    config = load_config(project_root)
    require_explicit_snapshot_policy(config)
    paths = human_editable_paths(project_root, config, args.path)
    status_lines = scoped_status(project_root, paths)

    if args.commit:
        commit_snapshot(project_root, paths, status_lines, args.message)
    else:
        print_report(project_root, paths, status_lines, args.message, dry_run=True)


if __name__ == "__main__":
    main()
