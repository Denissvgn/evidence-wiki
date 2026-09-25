"""CLI transport for revision planning, owner application and explicit migration."""

from __future__ import annotations

import json
from pathlib import Path

from ._pack_io import canonical, refuse
from .agent import _Parser
from .pack_discovery import owner
from .pack_revision_contracts import REEVALUATION, REQUEST, decode
from .pack_revisions import apply, plan, read_input, status


def main(operation, argv):
    parser = _Parser(prog="evidence-wiki pack " + operation)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    if operation != "revision-apply":
        parser.add_argument("--target", required=True)
    if operation in {"revision-plan", "revision-status"}:
        parser.add_argument("--catalog")
    if operation == "revision-status":
        parser.add_argument("--evaluate", action="store_true", help="Explicitly run read-only coverage, computation and current review checks.")
    if operation == "revision-plan":
        parser.add_argument("--path")
        parser.add_argument("--id", dest="revision")
        parser.add_argument("--rationale", required=True)
        parser.add_argument("--keep-local", action="append", default=[])
        parser.add_argument("--accept-pack", action="append", default=[])
        parser.add_argument("--output")
    if operation in {"revision-apply", "reevaluate"}:
        parser.add_argument("--from-file", required=True)
    try:
        args = parser.parse_args(argv)
        if operation == "revision-plan":
            request = {k: getattr(args, k) for k in ("target", "path", "catalog", "revision", "rationale", "keep_local", "accept_pack")}
            for key in ("target", "path", "catalog"):
                if request[key] is not None:
                    request[key] = str(Path(request[key]).expanduser().absolute())
            result = plan({"schema_version": REQUEST, **request})
            if args.output:
                from .pack_revisions import _candidate
                from .planning_commands import save_document

                output = Path(args.output).expanduser().resolve()
                candidate, _ = _candidate(request)
                if output.is_relative_to(Path(request["target"]).resolve()) or output.is_relative_to(candidate.resolve()):
                    refuse("revision_plan_output_overlaps_inputs")
                save_document(result, str(output))
        elif operation == "revision-apply":
            result = apply(read_input(args.from_file))
        elif operation == "reevaluate":
            from .pack_catalog import _outside_assets

            _outside_assets(Path(args.target), additional_roots=(Path(__file__).parent,))
            result = owner("coverage_manifest").run_revision(args.target, decode(read_input(args.from_file), REEVALUATION))
        else:
            result = status(args.target, catalog=args.catalog, evaluate=args.evaluate)
        if len(canonical(result)) > 1_048_576:
            refuse("revision_output_bound", "ONBOARDING_LIMIT")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2 if args.format == "text" else None))
        return 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        print(json.dumps({"schema_version": "1.0", "error_code": getattr(error, "error_code", "ONBOARDING_INVALID"),
            "message": "Pack revision request refused.", "recoverable": getattr(error, "recoverable", False),
            "details": getattr(error, "details", {"reason": "revision_input_or_environment_invalid"}),
            "remediation": "Inspect current conflicts, original action bindings and incomplete migration; preserve existing evidence."}))
        return getattr(error, "exit_code", 2)
