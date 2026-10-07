"""Noninteractive parse/format adapter; public command dispatch owns decisions."""

import argparse
import json
from pathlib import Path

from ._pack_io import json_document, read_file
from .errors import EvidenceWikiError, UsageError
from .research_command_contracts import OPERATIONS, REQUEST, error_document
from .runtime_configuration import HostRuntime
from .workspace import Workspace


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise UsageError("RESEARCH_COMMAND_ARGUMENTS_INVALID", "Invalid autonomous command arguments.", recoverable=False)


def summary(document):
    if document["status"] == "refused":
        return f"{document['operation']}: refused ({document['error']['error_code']})."
    result = document["result"]
    outcome = result.get("outcome", result.get("verdict", result.get("status", "returned")))
    return f"{document['operation']}: {outcome}."


def main(operation, argv):
    parser = Parser(prog="evidence-wiki agent "+operation,
        description="Explicit noninteractive public-library adapter. No semantic prompts, credential setup or background daemon.",
        epilog="Side effects: run-cycle/refresh can write canonical run, draft, review and question state within runtime grants. Other operations are bounded reads. Runtime selects trusted host executables and private credentials; input data grants no authority.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--runtime", help="Explicit owner-private host runtime JSON; stdin selectors are refused.")
    parser.add_argument("--request-file", help="Bounded operation arguments JSON file; stdin selectors are refused.")
    parser.add_argument("--run-id")
    parser.add_argument("--request-id")
    parser.add_argument("--previous-run-id")
    parser.add_argument("--planner-config-ref")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--noninteractive", action="store_true", help="Always enabled; never read user stdin or prompt.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    try:
        args = parser.parse_args(argv)
        arguments = {}
        if args.request_file:
            if args.request_file == "-":
                raise UsageError("RESEARCH_COMMAND_ARGUMENTS_INVALID", "Stdin is not an operation configuration source.", recoverable=False)
            path = Path(args.request_file).expanduser().absolute()
            arguments = json_document(read_file(path.parent, path.name))
            if type(arguments) is not dict:
                raise UsageError("RESEARCH_COMMAND_ARGUMENTS_INVALID", "Operation arguments must be a JSON object.", recoverable=False)
        for name in ("run_id", "request_id", "previous_run_id", "planner_config_ref", "max_steps"):
            value = getattr(args, name)
            if value is not None:
                if name in arguments and arguments[name] != value:
                    raise UsageError("RESEARCH_COMMAND_ARGUMENTS_INVALID", "Duplicate operation arguments conflict.", recoverable=False)
                arguments[name] = value
        runtime = HostRuntime.from_file(args.runtime) if args.runtime else None
        with Workspace.open(args.target) as workspace:
            result = workspace.research.command({"schema_version": REQUEST, "operation": operation, "arguments": arguments}, runtime=runtime)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")) if args.format == "json" else summary(result)
        print(rendered)
        return result["exit_code"]
    except EvidenceWikiError as error:
        from .research_command_operations import refusal
        if operation not in OPERATIONS:
            print(json.dumps(error_document(error), separators=(",", ":")))
            return error.exit_code
        result = refusal(operation, error)
        print(summary(result) if "args" in locals() and args.format == "text" else
            json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return result["exit_code"]
    except SystemExit as error:
        return int(error.code or 0)
