#!/usr/bin/env python3
"""Aggregate workspace status across multiple research workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

SCHEMA_VERSION = "1.0"
EXIT_OK = 0

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_module_loader import load_workspace_module


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate status for a fleet of research workspaces.")
    parser.add_argument("--target", action="append", required=True, help="Workspace root. Repeatable.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format. Defaults to text.")
    parser.add_argument("--no-cache", action="store_true", help="Bypass each workspace's status cache.")
    return parser.parse_args(argv)


def load_workspace_status() -> ModuleType:
    return load_workspace_module(_SCRIPT_DIR, "workspace_status")


def target_summary(target: Path, status_module: ModuleType, *, no_cache: bool) -> dict[str, Any]:
    resolved = target.expanduser().resolve()
    try:
        document = status_module.cached_status_document(resolved, no_cache=no_cache)
    except SystemExit as exc:
        return {
            "path": str(resolved),
            "ok": False,
            "error_code": "WORKSPACE_UNREADABLE",
            "message": str(exc),
        }
    except Exception as exc:
        error_code = "WORKSPACE_UNREADABLE"
        message = str(exc)
        if hasattr(exc, "error_code"):
            error_code = str(exc.error_code)
        return {
            "path": str(resolved),
            "ok": False,
            "error_code": error_code,
            "message": message,
        }

    workspace_health = document.get("workspace_health")
    if isinstance(workspace_health, dict) and not workspace_health.get("materially_valid", False):
        return {
            "path": str(resolved),
            "ok": False,
            "error_code": "WORKSPACE_UNREADABLE",
            "message": "Shared workspace health rejected the workspace contract.",
            "finding_codes": workspace_health.get("finding_codes", []),
        }

    run_controller = document.get("run_controller") if isinstance(document.get("run_controller"), dict) else {}
    has_active_run = bool(run_controller.get("present")) and not bool(run_controller.get("terminal"))
    is_stale = has_active_run and bool(run_controller.get("stale"))
    project = document.get("project") if isinstance(document.get("project"), dict) else {}
    readiness = document.get("readiness") if isinstance(document.get("readiness"), dict) else {}
    budget_state = readiness.get("budget_state") if isinstance(readiness.get("budget_state"), dict) else None
    operational_debt = (
        readiness.get("operational_debt")
        if isinstance(readiness.get("operational_debt"), dict)
        else {
            "warning_count": 0,
            "deferred_count": 0,
            "blocks_completion": False,
            "has_debt": False,
        }
    )
    return {
        "path": str(resolved),
        "ok": True,
        "project_name": project.get("name"),
        "readiness_verdict": readiness.get("verdict"),
        "budget_state": budget_state,
        "operational_debt": operational_debt,
        "active_run_count": 1 if has_active_run else 0,
        "stale_run_count": 1 if is_stale else 0,
        "run_controller": run_controller,
    }


def build_report(targets: list[str], *, no_cache: bool) -> dict[str, Any]:
    status_module = load_workspace_status()
    summaries = [target_summary(Path(target), status_module, no_cache=no_cache) for target in targets]
    return {
        "schema_version": SCHEMA_VERSION,
        "targets": summaries,
        "counts": {
            "targets": len(summaries),
            "ok": sum(1 for summary in summaries if summary.get("ok")),
            "errors": sum(1 for summary in summaries if not summary.get("ok")),
            "active_runs": sum(int(summary.get("active_run_count", 0) or 0) for summary in summaries),
            "stale_runs": sum(int(summary.get("stale_run_count", 0) or 0) for summary in summaries),
            "targets_with_operational_debt": sum(
                1
                for summary in summaries
                if isinstance(summary.get("operational_debt"), dict)
                and summary["operational_debt"].get("has_debt")
            ),
            "operational_warnings": sum(
                int(summary.get("operational_debt", {}).get("warning_count", 0) or 0)
                for summary in summaries
                if isinstance(summary.get("operational_debt"), dict)
            ),
            "deferred_items": sum(
                int(summary.get("operational_debt", {}).get("deferred_count", 0) or 0)
                for summary in summaries
                if isinstance(summary.get("operational_debt"), dict)
            ),
        },
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Research Workspace Fleet Status",
        "===============================",
        f"Targets: {report['counts']['targets']}",
        f"OK: {report['counts']['ok']}",
        f"Errors: {report['counts']['errors']}",
        f"Active runs: {report['counts']['active_runs']}",
        f"Stale runs: {report['counts']['stale_runs']}",
        f"Targets with operational debt: {report['counts']['targets_with_operational_debt']}",
        f"Operational warnings: {report['counts']['operational_warnings']}",
        f"Deferred items: {report['counts']['deferred_items']}",
    ]
    for summary in report["targets"]:
        if summary.get("ok"):
            debt = summary.get("operational_debt") if isinstance(summary.get("operational_debt"), dict) else {}
            lines.append(
                f"- {summary['path']}: {summary.get('readiness_verdict')} "
                f"(active={summary.get('active_run_count')}, stale={summary.get('stale_run_count')}, "
                f"warnings={debt.get('warning_count', 0)}, deferred={debt.get('deferred_count', 0)})"
            )
        else:
            lines.append(f"- {summary['path']}: {summary.get('error_code')} {summary.get('message')}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args.target, no_cache=args.no_cache)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(render_text(report), end="")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
