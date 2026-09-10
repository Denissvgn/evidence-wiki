"""Host-authenticated permissions, sanitized revisions, and revocation lineage."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from _evidence_authority import EvidenceInvalid, authority_basis, load_trust, name
from _evidence_usage import current_view, decode_files, selection, transact, workspace_binding
from _host_evidence_store import LOCK_FILE, STATE_FILE, host_directory, locked_state, read_state
from _record_artifacts import exact_object, json_document
from _script_errors import ScriptRefusal, emit_refusal
from _usage_gate import refusal
from _usage_materialization import materialize


def configuration(root: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load((root / "research.yml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise EvidenceInvalid("usage_configuration_unreadable") from exc
    if not isinstance(config, dict):
        raise EvidenceInvalid("usage_configuration_invalid")
    return config


def run_status(project_root: Path, *, request_id: str | None = None) -> dict[str, Any]:
    """Inspect current state or reconcile an earlier transaction without replaying it."""
    root = Path(project_root).resolve()
    try:
        config = configuration(root)
        state_id = selection(config)
        trust = load_trust(root, config, datetime.now(timezone.utc))
        report = {"schema_version": "evidence-usage-status/v1", "state_id": state_id,
                  "workspace_binding": workspace_binding(root), "authority": authority_basis(trust)}
        if request_id is not None:
            name(request_id)
        with host_directory(root) as directory:
            try:
                os.stat(LOCK_FILE, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.stat(STATE_FILE, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    return {**report, "initialized": False, "checkpoint": None, "receipt": None}
                raise EvidenceInvalid("host_state_lock_missing") from None
        with locked_state(root) as directory:
            if read_state(directory, allow_missing=True) is None:
                return {**report, "initialized": False, "checkpoint": None, "receipt": None}
        with current_view(root, config) as view:
            event = view.state.requests.get(request_id) if request_id is not None else None
            return {**report, "authority": authority_basis(view.trust), "initialized": True, "checkpoint": view.state.checkpoint,
                    "event_count": len(view.state.events), "revision_count": len(view.state.revisions),
                    "node_count": len(view.state.nodes), "revoked_source_count": len(view.state.revoked_sources),
                    "revoked_revision_count": len(view.state.revoked_revisions),
                    "receipt": view.state.receipt(event) if event is not None else None}
    except EvidenceInvalid as exc:
        raise refusal(str(exc)) from exc


def run_transact(project_root: Path, *, command: dict[str, Any], artifacts: dict[str, bytes] | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        return transact(root, configuration(root), command, artifacts)
    except (EvidenceInvalid, TypeError, ValueError, RecursionError) as exc:
        raise refusal(str(exc) if isinstance(exc, EvidenceInvalid) else "usage_command_invalid") from exc


def run_check(project_root: Path, *, revision: str, uses: list[str], purpose: str, consumer: str) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        with current_view(root, configuration(root)) as view:
            return view.check(revision, uses=uses, purpose=purpose, consumer=consumer)
    except EvidenceInvalid as exc:
        raise refusal(str(exc)) from exc


def run_lineage(project_root: Path, *, revision: str, limit: int = 4096) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        with current_view(root, configuration(root)) as view:
            return view.lineage(revision, limit=limit)
    except EvidenceInvalid as exc:
        raise refusal(str(exc)) from exc


def run_materialize(project_root: Path, *, revision: str, expected_content_hash: str | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        return materialize(root, configuration(root), revision, expected_content_hash)
    except EvidenceInvalid as exc:
        raise refusal(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("status", "transact", "check", "lineage", "materialize"))
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--format", choices=("json",), default="json")
    parser.add_argument("--request-id")
    parser.add_argument("--revision")
    parser.add_argument("--use", action="append", choices=("retrieval", "training", "export"))
    parser.add_argument("--purpose")
    parser.add_argument("--consumer")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--expected-content-hash")
    args = parser.parse_args(argv)
    selected_flags = {key for key in ("request_id", "revision", "use", "purpose", "consumer", "limit", "expected_content_hash")
                      if getattr(args, key) is not None}
    allowed_flags = {"status": {"request_id"}, "transact": set(), "check": {"revision", "use", "purpose", "consumer"},
                     "lineage": {"revision", "limit"}, "materialize": {"revision", "expected_content_hash"}}
    if selected_flags - allowed_flags[args.operation]:
        parser.error("the selected operation does not accept these options")
    if args.operation in {"check", "lineage", "materialize"} and not args.revision:
        parser.error("the selected operation requires --revision")
    if args.operation == "check" and not all((args.use, args.purpose, args.consumer)):
        parser.error("check requires --use, --purpose, and --consumer")
    root = Path(args.project_root).resolve()
    try:
        if args.operation == "transact":
            # Stdin is an in-memory transport; the package creates no raw input file.
            raw = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024:
                raise EvidenceInvalid("usage_transport_bound_exceeded")
            document = exact_object(json_document(raw), {"command", "artifacts"})
            if not isinstance(document["artifacts"], dict):
                raise EvidenceInvalid("invalid_artifact_transport")
            artifacts = decode_files(document["artifacts"]) if document["artifacts"] else {}
            report = run_transact(root, command=document["command"], artifacts=artifacts)
        elif args.operation == "status":
            report = run_status(root, request_id=args.request_id)
        elif args.operation == "check":
            report = run_check(root, revision=args.revision, uses=args.use, purpose=args.purpose, consumer=args.consumer)
        elif args.operation == "lineage":
            report = run_lineage(root, revision=args.revision, limit=args.limit if args.limit is not None else 4096)
        else:
            report = run_materialize(root, revision=args.revision, expected_content_hash=args.expected_content_hash)
        print(json.dumps(report, sort_keys=True, indent=2))
        return int(args.operation == "check" and not report["eligible"])
    except EvidenceInvalid as exc:
        return emit_refusal(refusal(str(exc)), json_mode=True)
    except ScriptRefusal as exc:
        return emit_refusal(exc, json_mode=True)


if __name__ == "__main__":
    raise SystemExit(main())
