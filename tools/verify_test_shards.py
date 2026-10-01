#!/usr/bin/env python3
"""Require complete, successful shard inventories before building distributions."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from tools.run_test_groups import COMBINED_MODULES, groups, source_identity


def check_run(row: dict, label: str, expected: list[str], *, collect: bool = False) -> None:
    result = row["result"]
    if row["label"] != label or row["exit_code"] != 0 or result["exit_code"] != 0:
        raise ValueError(f"{label}: process did not pass")
    if result["collected"] != expected or result["executed"] != ([] if collect else expected):
        raise ValueError(f"{label}: collection or execution inventory differs")
    if not collect and (row["selection_matches"] is not True or row["execution_complete"] is not True):
        raise ValueError(f"{label}: incomplete execution")


def verify(root: Path, evidence: Path, *, commit: str, run_id: str, platforms: list[str],
           shard_count: int, group_size: int = 160, python_patches: dict[str, str] | None = None) -> dict:
    """Compare each platform's full collection with every expected shard and order."""
    if not commit or not run_id or not platforms or len(set(platforms)) != len(platforms):
        raise ValueError("commit, run ID and unique expected platforms are required")
    if shard_count < 1 or group_size < 1:
        raise ValueError("shard count and group size must be positive")
    if python_patches is not None:
        if set(python_patches) != set(platforms):
            raise ValueError("Python patch expectations must cover exactly the required platforms")
        for platform, patch in python_patches.items():
            if not re.fullmatch(r"\d+\.\d+\.\d+", patch) or not patch.startswith(platform.rsplit("/", 1)[-1] + "."):
                raise ValueError(f"{platform}: invalid Python patch expectation")
    identity = source_identity(root)
    reports: dict[str, dict[int, dict]] = {platform: {} for platform in platforms}
    for path in sorted(evidence.rglob("manifest.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        runner, shard = report["runner"], report["shard"]
        python = ".".join(report["python"].split()[0].split(".")[:2])
        expected_python = report.get("expected_python")
        if expected_python is not None and report["python"].split()[0] != expected_python:
            raise ValueError(f"{path}: resolved Python patch differs from the qualification runtime")
        platform = f"{runner['RUNNER_OS']}/{runner['RUNNER_ARCH']}/{python}"
        index = shard["index"]
        if platform not in reports or type(index) is not int or not 1 <= index <= shard_count:
            raise ValueError(f"{path}: unexpected platform or shard")
        if python_patches is not None and (report["python"].split()[0] != python_patches[platform]
                                           or expected_python != python_patches[platform]):
            raise ValueError(f"{platform}: Python patch does not match the required baseline")
        if index in reports[platform]:
            raise ValueError(f"{platform}: duplicate shard {index}")
        if (report["commit"] != commit or runner["GITHUB_RUN_ID"] != run_id
                or report["source_sha256"] != identity or report["sources_unchanged"] is not True):
            raise ValueError(f"{platform} shard {index}: source or workflow run identity differs")
        if (report["status"] != "passed" or shard["count"] != shard_count
                or report["group_size"] != group_size or report["targets"] != ["tests"]):
            raise ValueError(f"{platform} shard {index}: failed or incompatible suite plan")
        reports[platform][index] = report

    summary = {}
    for platform, shards in reports.items():
        if set(shards) != set(range(1, shard_count + 1)):
            raise ValueError(f"{platform}: missing shards")
        nodes = shards[1]["collection"]["result"]["collected"]
        if (not isinstance(nodes, list) or not nodes or not all(isinstance(node, str) and node for node in nodes)
                or len(nodes) != len(set(nodes))):
            raise ValueError(f"{platform}: invalid full collection")
        planned = groups(nodes, group_size)
        if len(planned) < shard_count:
            raise ValueError(f"{platform}: shard count exceeds the number of test groups")
        modules = [module for module in COMBINED_MODULES if any(node.startswith(module + "::") for node in nodes)]
        orders = [("combined-forward", modules), ("combined-reverse", modules[::-1])] if len(modules) >= 2 else []
        for index, report in shards.items():
            check_run(report["collection"], "collection", nodes, collect=True)
            selected = [(f"group-{number:03}", members) for number, members in enumerate(planned, 1)
                        if (number - 1) % shard_count == index - 1]
            if len(report["groups"]) != len(selected):
                raise ValueError(f"{platform} shard {index}: missing or extra groups")
            for row, (label, members) in zip(report["groups"], selected, strict=True):
                check_run(row, label, members)
            expected_orders = orders if index == 1 else []
            if len(report["combined_orders"]) != len(expected_orders):
                raise ValueError(f"{platform} shard {index}: missing or extra combined orders")
            for row, (label, order) in zip(report["combined_orders"], expected_orders, strict=True):
                expected = [node for module in order for node in nodes if node.startswith(module + "::")]
                check_run(row, label, expected)
        summary[platform] = {"collected": len(nodes), "groups": len(planned),
                             "shards": shard_count, "combined_orders": len(orders)}
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--platform", action="append", required=True, dest="platforms", metavar="OS/ARCH/PYTHON")
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--group-size", type=int, default=160)
    parser.add_argument("--python-patch", action="append", metavar="OS/ARCH/PYTHON=PATCH")
    args = parser.parse_args(argv)
    try:
        patches = None
        if args.python_patch is not None:
            patches = {}
            for value in args.python_patch:
                platform, patch = value.split("=", 1)
                if platform in patches:
                    raise ValueError(f"Duplicate Python patch expectation: {platform}")
                patches[platform] = patch
        summary = verify(args.root.resolve(), args.evidence.resolve(), commit=args.commit, run_id=args.run_id,
                         platforms=args.platforms, shard_count=args.shard_count, group_size=args.group_size,
                         python_patches=patches)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        print(f"Suite shard verification failed: {error}")
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
