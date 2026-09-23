#!/usr/bin/env python3
"""Run the complete collection in bounded fresh processes and selected combined orders."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

COMBINED_MODULES = (
    "tests/test_script_loader.py", "tests/test_script_host.py", "tests/test_library_workspace.py",
    "tests/test_provider_contract.py", "tests/test_provider_plugins.py",
)


def source_identity(root: Path) -> dict:
    roots = ("src/evidence_wiki", "workspace-template", "domain-packs", "tests", "tools", ".github")
    ignored = {"__pycache__", ".pytest_cache", ".ruff_cache"}
    files = {path for name in roots for path in (root / name).rglob("*")
             if path.is_file() and not ignored.intersection(path.relative_to(root).parts)
             and path.suffix not in {".pyc", ".pyo"}}
    files.update(root / name for name in ("pyproject.toml", "README.md", "CHANGELOG.md") if (root / name).is_file())
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


def groups(nodes: list[str], limit: int) -> list[list[str]]:
    """Keep each module intact; release its process state between bounded groups."""
    modules: dict[str, list[str]] = {}
    for node in nodes:
        modules.setdefault(node.split("::", 1)[0], []).append(node)
    result, current = [], []
    for members in modules.values():
        if current and len(current) + len(members) > limit:
            result.append(current)
            current = []
        current.extend(members)
    if current:
        result.append(current)
    return result


def run(root: Path, output: Path, label: str, targets: list[str], *, coverage: bool = False,
        collect: bool = False, timeout: float = 1800) -> dict:
    log, record = output / f"{label}.log", output / f"{label}.json"
    command = [sys.executable, "-m"]
    command += ["coverage", "run", "-m", "pytest"] if coverage else ["pytest"]
    command += ["-q", "--tb=short", "-p", "tools._suite_plugin"]
    if collect:
        command.append("--collect-only")
    else:
        command.append(f"--junitxml={output / (label + '.xml')}")
    arguments = output / f"{label}.args"
    arguments.write_text("\n".join(targets) + "\n", encoding="utf-8", newline="\n")
    command.append("@" + str(arguments))
    environment = dict(os.environ, EVIDENCE_WIKI_SUITE_RECORD=str(record))
    environment["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "src"), environment.get("PYTHONPATH", "")))
    started = time.perf_counter()
    with log.open("w", encoding="utf-8", newline="\n") as stream:
        try:
            completed = subprocess.run(command, cwd=root, env=environment, stdout=stream,  # noqa: S603 - explicit pytest argv.
                                       stderr=subprocess.STDOUT, timeout=timeout, check=False)
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = 124
    result = {"label": label, "command": command, "exit_code": code,
              "seconds": time.perf_counter() - started, "log": str(log), "record": str(record)}
    if record.is_file():
        result["result"] = json.loads(record.read_text(encoding="utf-8"))
    else:
        result["missing_record"] = "process did not finish; inspect retained log"
    print(f"{label}: exit {code}, {result['seconds']:.2f}s", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("suite-evidence"))
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--group-size", type=int, default=160)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=1,
                        help="One-based shard; whole groups are distributed round-robin.")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("targets", nargs="*", default=["tests"])
    args = parser.parse_args(argv)
    if args.group_size < 1 or args.timeout <= 0:
        parser.error("group size and timeout must be positive")
    if not 1 <= args.shard_index <= args.shard_count:
        parser.error("shard index must be between 1 and shard count")
    root, output = args.root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    identity = source_identity(root)
    git = shutil.which("git")
    commit = subprocess.run([git, "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False) if git else None  # noqa: S603
    report = {"strategy": "fresh processes; intact modules grouped around a target count",
              "limits": ["No complete-suite single-interpreter claim.",
                         "Two combined module orders cover selected shared caches and registries only.",
                         "A module larger than the target runs alone; timeout is per process."],
              "python": sys.version, "platform": platform.platform(), "source_sha256": identity,
              "shard": {"index": args.shard_index, "count": args.shard_count},
              "group_size": args.group_size, "targets": args.targets,
              "commit": commit.stdout.strip() if commit is not None and commit.returncode == 0 else None,
              "runner": {key: os.environ[key] for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                         "RUNNER_OS", "RUNNER_ARCH", "RUNNER_NAME") if key in os.environ},
              "groups": [], "combined_orders": [], "status": "running"}
    manifest = output / "manifest.json"

    def save():
        manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")

    save()
    try:
        collected = run(root, output, "collection", args.targets, collect=True, timeout=args.timeout)
        report["collection"] = collected
        if collected["exit_code"] or "result" not in collected:
            raise ValueError("collection did not complete")
        nodes = collected["result"]["collected"]
        if not nodes or len(nodes) != len(set(nodes)):
            raise ValueError("collection is empty or has duplicate node identifiers")
        planned = groups(nodes, args.group_size)
        if args.shard_count > len(planned):
            raise ValueError("shard count exceeds the number of test groups")
        failed = False
        for number, selected in enumerate(planned, 1):
            if (number - 1) % args.shard_count != args.shard_index - 1:
                continue
            row = run(root, output, f"group-{number:03}", selected, coverage=args.coverage, timeout=args.timeout)
            observed = row.get("result", {})
            row["selection_matches"] = observed.get("collected") == selected
            row["execution_complete"] = observed.get("executed") == selected
            failed |= bool(row["exit_code"] or not row["selection_matches"] or not row["execution_complete"])
            report["groups"].append(row)
            save()
        modules = [name for name in COMBINED_MODULES if any(node.startswith(name + "::") for node in nodes)]
        if args.shard_index == 1 and len(modules) >= 2:
            for label, order in (("combined-forward", modules), ("combined-reverse", modules[::-1])):
                row = run(root, output, label, order, coverage=args.coverage, timeout=args.timeout)
                expected = [node for module in order for node in nodes if node.startswith(module + "::")]
                row["selection_matches"] = row.get("result", {}).get("collected") == expected
                row["execution_complete"] = row.get("result", {}).get("executed") == expected
                failed |= bool(row["exit_code"] or not row["selection_matches"] or not row["execution_complete"])
                report["combined_orders"].append(row)
                save()
        report["sources_unchanged"] = source_identity(root) == identity
        report["status"] = "failed" if failed or not report["sources_unchanged"] else "passed"
        return int(report["status"] != "passed")
    except KeyboardInterrupt:
        report.update(status="interrupted", error="Interrupted; partial process logs are retained without pass credit.")
        return 130
    except (OSError, ValueError) as error:
        report.update(status="failed", error=str(error))
        return 1
    finally:
        save()


if __name__ == "__main__":
    raise SystemExit(main())
