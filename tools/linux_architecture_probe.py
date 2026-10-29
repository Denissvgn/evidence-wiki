#!/usr/bin/env python3
"""Run the same bounded native Linux probe selection on x64 and ARM64."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools._local_ci_contract import (  # noqa: E402
    GROUP_SIZE,
    GROUP_TIMEOUT,
    LINUX_NATIVE_TARGETS,
    PYTHON_PATCHES,
    checked_runtime,
    runtime_identity,
    suite_result,
)
from tools.run_test_groups import main as run_groups  # noqa: E402
from tools.run_test_groups import source_identity  # noqa: E402
from tools.verify_local_ci import git  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=("X64", "ARM64"), required=True)
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        runtime = checked_runtime(runtime_identity(), system="Linux", python=PYTHON_PATCHES["3.12"])
        if runtime["architecture"] != args.architecture:
            raise ValueError("native machine differs from the requested probe architecture")
        args.output.mkdir(parents=True, exist_ok=False)
        commit = git(root, "rev-parse", "HEAD")
        tree = git(root, "rev-parse", "HEAD^{tree}")
        source = source_identity(root)
        saved = {key: value for key, value in os.environ.items() if key.startswith(("GITHUB_", "RUNNER_"))}
        try:
            for key in saved:
                del os.environ[key]
            os.environ["EVIDENCE_WIKI_EXPECTED_PYTHON"] = runtime["python"]
            result = run_groups(["--group-size", str(GROUP_SIZE), "--timeout", str(GROUP_TIMEOUT),
                                 "--output", str(args.output / "suite"), *LINUX_NATIVE_TARGETS])
        finally:
            os.environ.update(saved)
        if result:
            return result
        report = json.loads((args.output / "suite/manifest.json").read_text(encoding="utf-8"))
        value = {"commit": commit, "tree": tree, "runtime": runtime, "targets": list(LINUX_NATIVE_TARGETS),
                 **suite_result(report, commit=commit, source=source, targets=LINUX_NATIVE_TARGETS, runtime=runtime)}
        (args.output / "summary.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(value))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Native architecture probe refused: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
