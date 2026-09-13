#!/usr/bin/env python3
"""Preserve available release reports with explicit missing/invalid dispositions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path

REPORT_NAMES = ("artifact-validation.json", "scale-benchmark-standard.json")


def collect(root: Path, output: Path, reports: list[str] | tuple[str, ...] | None = None) -> dict:
    selected = tuple(reports) if reports is not None else REPORT_NAMES
    if not selected or len(selected) != len(set(selected)) or any(name not in REPORT_NAMES for name in selected):
        raise ValueError("Select unique supported release report names")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(), "platform": platform.platform(),
        "context": {key: os.environ.get(key) for key in (
            "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_EVENT_NAME",
            "GITHUB_REF", "RUNNER_OS", "RUNNER_ARCH", "RUNNER_NAME", "RELEASE_GATE_STATUS",
        )},
        "reports": [],
    }
    for name in selected:
        source = root / name
        entry = {"path": name}
        if not source.is_file():
            entry.update(status="missing", reason="Report was not produced; inspect the producing step's logs/outcome.")
        else:
            shutil.copyfile(source, output / name)
            content = (output / name).read_bytes()
            entry.update(status="available", size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
            try:
                json.loads(content)
            except ValueError:
                entry.update(status="invalid-json", reason="Partial/invalid producer output retained; inspect its logs.")
        manifest["reports"].append(entry)
        if entry["status"] != "available":
            print(f"{name}: {entry['reason']}")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("release-diagnostics"))
    parser.add_argument("--report", action="append", choices=REPORT_NAMES,
                        help="Report owned by this job; repeat to select several. Defaults to both release reports.")
    args = parser.parse_args()
    collect(args.root, args.output, args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
