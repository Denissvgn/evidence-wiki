"""Installed caller research commands; explicit mutations keep their canonical owners."""

from __future__ import annotations

import json
from pathlib import Path

from ._pack_io import read_file
from ._script_host import shared_assets_root
from .agent import _Parser
from .errors import ONBOARDING_ERROR_CONTRACTS, default_exit_code, default_recoverable
from .planning_contracts import MAX_BYTES, refuse
from .research_contracts import contract_index, decode_estimates, schema_document
from .source_commands import _no_secret_values


def main(operation, argv):
    parser = _Parser(prog="evidence-wiki agent " + operation)
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
        elif operation == "next":
            from .research_actions import guidance

            result = guidance(args.target, agent_id=args.agent_id, run_id=args.run_id)
        elif operation in {"start", "resume", "heartbeat"}:
            from .research_operations import run_operation

            result = run_operation(args.target, operation, agent_id=args.agent_id, run_id=args.run_id)
        elif operation == "research-export":
            from .research_completion import completion

            result = completion(args.target, allow_partial=args.allow_partial, run_id=args.run_id)
        elif operation == "progress":
            from .research_progress import progress, save_report

            estimates = None
            if args.estimates:
                path = Path(args.estimates).expanduser().absolute()
                estimates = decode_estimates(read_file(path.parent, path.name))
            result = progress(args.target, run_id=args.run_id, estimates=estimates)
            if args.output:
                save_report(args.output, result, target=args.target)
        else:
            from .research_operations import acquire, ingest

            shared = {"agent_id": args.agent_id, "run_id": args.run_id, "request_id": args.request_id}
            result = (
                acquire(args.target, url=args.url, **shared)
                if operation == "acquire"
                else ingest(
                    args.target,
                    source_id=args.source_id,
                    source_path=args.source_path,
                    needs_complete=not args.allow_partial_source,
                    **shared,
                )
            )
        _no_secret_values(result)
        rendered = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            **({"indent": 2} if args.format == "text" else {"separators": (",", ":")}),
        )
        if len(rendered.encode()) + 1 > MAX_BYTES:
            refuse("research_output_bound", "ONBOARDING_LIMIT")
        print(result["content"].rstrip() if operation == "research-guide" and args.format == "text" else rendered)
        return 3 if operation == "research-export" and result["status"] == "incomplete" else 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        from .research_observation import reason

        code = getattr(error, "error_code", "ONBOARDING_INVALID")
        if code not in ONBOARDING_ERROR_CONTRACTS:
            code = (
                "ONBOARDING_LOCK_BUSY"
                if getattr(error, "contended", False)
                else "ONBOARDING_OWNERSHIP_CONFLICT"
                if "CONFLICT" in code
                else "ONBOARDING_CHECK_FAILED"
            )
        print(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "error_code": code,
                    "message": "Caller research operation refused; retained state preserved.",
                    "details": {"reason": reason(error), "owner_code": getattr(error, "error_code", None)},
                    "recoverable": default_recoverable(code),
                    "remediation": "Inspect agent research-guide and current owner state. Refresh advice; never force claims, reset usage or treat declarations as evidence.",
                }
            )
        )
        return default_exit_code(code)
