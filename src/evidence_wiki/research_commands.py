"""Installed caller research commands; explicit mutations keep their canonical owners."""

from __future__ import annotations

import json
from pathlib import Path

from ._pack_io import read_file
from ._script_host import shared_assets_root
from .agent import _Parser
from .planning_contracts import MAX_BYTES, refuse
from .research_contracts import contract_index, decode_estimates, schema_document
from .source_commands import _no_secret_values


def summary(operation, result):
    if operation == "research-guide":
        return result["content"].rstrip()
    status = result.get("status", "complete" if result.get("research_complete") else "returned")
    run = result.get("run", {})
    return f"{operation}: {status}." + (f" Run: {run['run_id']}." if run.get("run_id") else "")


def main(operation, argv):
    parser = _Parser(prog="evidence-wiki agent " + operation,
        description="Noninteractive adapter to the scoped public research library.",
        epilog="Side effects: start/heartbeat write caller run state; resume inspects retained state. Acquire writes captured source bytes and ingest invokes fixed native extraction. Next/export/progress are reads; progress --output writes a new external local report. No semantic, review or credential prompts.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    if operation not in {"research-guide", "research-schemas"}:
        parser.add_argument("--target", required=True)
    if operation in {"next", "start", "resume", "heartbeat", "acquire", "ingest", "progress", "research-export"}:
        parser.add_argument("--run-id", required=operation in {"resume", "heartbeat", "acquire", "ingest"})
        parser.add_argument("--agent-id", required=operation in {"start", "resume", "heartbeat", "acquire", "ingest"})
    if operation in {"acquire", "ingest"}:
        parser.add_argument("--request-id", required=True)
    if operation == "acquire":
        parser.add_argument("--url", required=True)
    if operation == "ingest":
        parser.add_argument("--source-id")
        parser.add_argument("--source-path")
        parser.add_argument("--allow-partial-source", action="store_true")
    if operation == "research-export":
        parser.add_argument("--allow-partial", action="store_true")
    if operation == "progress":
        parser.add_argument("--estimates")
        parser.add_argument("--output")
    if operation == "research-schemas":
        parser.add_argument("--schema-id")
    try:
        args = parser.parse_args(argv)
        if operation == "research-guide":
            result = {
                "schema_version": "evidence-research-guide/v1",
                "content": read_file(shared_assets_root(), "workspace-template/docs/caller-research.md").decode(),
            }
        elif operation == "research-schemas":
            result = schema_document(args.schema_id) if args.schema_id else contract_index()
        else:
            from .onboarding import Onboarding
            roots = [Path(args.target).expanduser().absolute()]
            if operation == "progress" and args.output:
                roots.append(Path(args.output).expanduser().absolute().parent)
            grant = {"start": "research.start", "resume": "research.resume", "heartbeat": "research.heartbeat",
                "ingest": "research.ingest", "acquire": "research_acquire", "progress": "research_progress"}.get(operation)
            if operation == "progress" and args.output:
                grant = "research_progress.write"
            with Onboarding.open(allowed_roots=roots, allow=[grant] if grant else []) as library:
                if operation == "next":
                    result = library.research_next(args.target, agent_id=args.agent_id, run_id=args.run_id)
                elif operation in {"start", "resume", "heartbeat"}:
                    result = library.research(args.target, operation, agent_id=args.agent_id, run_id=args.run_id)
                elif operation == "research-export":
                    result = library.research_export(args.target, allow_partial=args.allow_partial, run_id=args.run_id)
                elif operation == "progress":
                    estimates = None
                    if args.estimates:
                        path = Path(args.estimates).expanduser().absolute()
                        estimates = decode_estimates(read_file(path.parent, path.name))
                    result = library.research_progress(args.target, run_id=args.run_id, estimates=estimates, output=args.output)
                elif operation == "acquire":
                    result = library.research_acquire(args.target, agent_id=args.agent_id, run_id=args.run_id, request_id=args.request_id, url=args.url)
                else:
                    result = library.research(args.target, "ingest", agent_id=args.agent_id, run_id=args.run_id, request_id=args.request_id,
                        source_id=args.source_id, source_path=args.source_path, needs_complete=not args.allow_partial_source)
        _no_secret_values(result)
        rendered = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            **({"indent": 2} if args.format == "text" else {"separators": (",", ":")}),
        )
        if len(rendered.encode()) + 1 > MAX_BYTES:
            refuse("research_output_bound", "ONBOARDING_LIMIT")
        print(summary(operation, result) if args.format == "text" else rendered)
        return 3 if operation == "research-export" and result["status"] == "incomplete" else 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        from ._facades._base import _translated
        from .errors import EvidenceWikiError, UsageError
        from .research_command_contracts import error_document
        translated = error if isinstance(error, EvidenceWikiError) else _translated(error)
        if translated is None:
            translated = UsageError("ONBOARDING_INVALID", "Caller research arguments or inputs are invalid.", recoverable=False)
        print(json.dumps(error_document(translated), ensure_ascii=False, separators=(",", ":")))
        return translated.exit_code
