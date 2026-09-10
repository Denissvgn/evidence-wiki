"""Read-only discovery and validation of inert evidence intake profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _qualified_packet import inspect_packet
from _qualified_packet import profiles as packet_profiles
from _script_errors import ScriptRefusal
from _workspace_module_loader import load_workspace_module


def profiles() -> dict[str, Any]:
    """Discover all supported inert evidence profiles and authority boundaries."""
    report = packet_profiles()
    execution = load_workspace_module(Path(__file__).resolve().parent, "_execution_evidence")
    report["profiles"].append(execution.profile_description())
    return report


def selected_source(project_root: Path, source_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(source_id, str) or not source_id.strip():
        raise ScriptRefusal("SOURCE_UNKNOWN", "A manifest source ID is required.", exit_code=2)
    directory = Path(__file__).resolve().parent
    verify = load_workspace_module(directory, "normalize_verify")
    normalizer = load_workspace_module(directory, "normalize_sources")
    config = verify.load_config(project_root)
    manifest, _normalized = normalizer.source_paths(config)
    records = normalizer.load_manifest(project_root / manifest)
    matches = [record for record in records if record.get("id") == source_id]
    if len(matches) != 1:
        raise ScriptRefusal("SOURCE_UNKNOWN", "The source ID must identify one manifest record.", exit_code=2)
    return config, matches[0]


def validate_source(project_root: Path, source_id: str) -> dict[str, Any]:
    config, record = selected_source(project_root, source_id)
    return inspect_packet(project_root, config, record)


def validate_execution(project_root: Path, source_id: str) -> dict[str, Any]:
    """Return structural and current evaluator-authority verdicts independently."""
    config, record = selected_source(project_root, source_id)
    execution = load_workspace_module(Path(__file__).resolve().parent, "_execution_evidence")
    report = execution.inspect_execution(project_root, config, record)
    return {**report, "verification": execution.assess_verification(report, project_root, config)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("profiles", "packet", "execution"))
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--source-id")
    parser.add_argument("--format", choices=("json",), default="json")
    args = parser.parse_args(argv)
    if args.operation != "profiles" and not args.source_id:
        parser.error(f"{args.operation} requires --source-id")
    if args.operation == "profiles" and args.source_id:
        parser.error("profiles does not select a source")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    operation = {"packet": validate_source, "execution": validate_execution}
    report = profiles() if args.operation == "profiles" else operation[args.operation](Path(args.project_root).resolve(), args.source_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(args.operation != "profiles" and (not report["valid"] or args.operation == "packet" and not report.get("policy_satisfied")))


if __name__ == "__main__":
    from _script_errors import emit_refusal

    try:
        raise SystemExit(main())
    except ScriptRefusal as exc:
        emit_refusal(exc, json_mode=True)
        raise SystemExit(exc.exit_code) from exc
