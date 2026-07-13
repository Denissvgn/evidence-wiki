#!/usr/bin/env python3
"""Keep vendored workspace ``scripts/`` copies in sync with the starter template.

The reusable starter at ``workspace-template/scripts/`` is the single source
of truth for workspace tooling. Worked examples under ``examples/`` ship their
own ``scripts/`` directory so they look like a real initialized workspace. Those
copies must stay byte-identical to the template, otherwise published examples
drift onto stale, possibly less-safe tooling.

Usage:

    python3 tools/sync_vendored_scripts.py            # rewrite drifted copies
    python3 tools/sync_vendored_scripts.py --check    # report drift, write nothing

``--check`` exits non-zero on any drift so CI can guard against regressions.
"""

from __future__ import annotations

import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SCRIPTS_DIR = REPO_ROOT / "workspace-template" / "scripts"
VENDORED_PARENTS = ("examples",)


def template_scripts(template_dir: Path = TEMPLATE_SCRIPTS_DIR) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(template_dir.glob("*.py"))}


def vendored_script_dirs(repo_root: Path = REPO_ROOT) -> list[Path]:
    dirs: list[Path] = []
    for parent in VENDORED_PARENTS:
        base = repo_root / parent
        if not base.is_dir():
            continue
        for workspace in sorted(base.glob("*")):
            scripts_dir = workspace / "scripts"
            if scripts_dir.is_dir():
                dirs.append(scripts_dir)
    return dirs


def find_drift(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return human-readable drift messages; empty list means everything matches."""
    expected = template_scripts()
    messages: list[str] = []
    for scripts_dir in vendored_script_dirs(repo_root):
        relative = scripts_dir.relative_to(repo_root).as_posix()
        present = {path.name for path in scripts_dir.glob("*.py")}
        for name, content in expected.items():
            target = scripts_dir / name
            if not target.is_file():
                messages.append(f"{relative}/{name}: missing (not synced from template)")
            elif target.read_bytes() != content:
                messages.append(f"{relative}/{name}: differs from template")
        for extra in sorted(present - set(expected)):
            messages.append(f"{relative}/{extra}: not present in template (unexpected extra script)")
    return messages


def sync(repo_root: Path = REPO_ROOT, dry_run: bool = False) -> list[str]:
    """Rewrite drifted/missing vendored scripts from the template. Returns changed paths."""
    expected = template_scripts()
    changed: list[str] = []
    for scripts_dir in vendored_script_dirs(repo_root):
        relative = scripts_dir.relative_to(repo_root).as_posix()
        for name, content in expected.items():
            target = scripts_dir / name
            if target.is_file() and target.read_bytes() == content:
                continue
            changed.append(f"{relative}/{name}")
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync vendored workspace scripts with the starter template.")
    parser.add_argument("--check", action="store_true", help="Report drift and exit non-zero; write nothing.")
    args = parser.parse_args(argv)

    if args.check:
        drift = find_drift()
        if drift:
            print("Vendored scripts are out of sync with workspace-template/scripts/:")
            for message in drift:
                print(f"  - {message}")
            print("Run: python3 tools/sync_vendored_scripts.py")
            return 1
        print("Vendored scripts are in sync with the template.")
        return 0

    changed = sync()
    if changed:
        for path in changed:
            print(f"synced {path}")
        print(f"Updated {len(changed)} file(s) from workspace-template/scripts/.")
    else:
        print("Vendored scripts already in sync with the template.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
