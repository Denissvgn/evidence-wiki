"""JSON file transport for explicit data-only lifecycle extensions."""

from __future__ import annotations

import json
from pathlib import Path

from ._pack_io import canonical, read_file, refuse
from .agent import _Parser
from .errors import EvidenceWikiError


def main(operation, argv):
    from . import fleet_revisions, host_transitions, native_instructions, pack_composition, pack_migrations

    parser = _Parser(prog="evidence-wiki " + ("agent " if operation.startswith(("transition-", "instructions-")) else "pack ") + operation)
    parser.add_argument("--from-file", required=True)
    parser.add_argument("--output")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    try:
        args = parser.parse_args(argv)
        path = Path(args.from_file).expanduser().absolute()
        raw = read_file(path.parent, path.name)
        if args.output and operation not in {"migration-plan", "compose-plan", "compose", "fleet-plan", "transition-plan", "instructions-plan"}:
            refuse("output_option_requires_plan")
        if operation == "compose":
            if not args.output:
                refuse("composition_output_required")
            result = pack_composition.apply(raw, output=args.output)
        else:
            result = {"migration-plan": pack_migrations.plan, "migration-apply": pack_migrations.apply,
                      "compose-plan": pack_composition.plan, "fleet-plan": fleet_revisions.plan,
                      "fleet-apply": fleet_revisions.apply, "transition-plan": host_transitions.plan,
                      "transition-apply": host_transitions.apply, "instructions-plan": native_instructions.plan,
                      "instructions-apply": native_instructions.apply,
                      "instructions-remove": lambda raw: native_instructions.apply(raw, remove=True)}[operation](raw)
        if args.output and operation in {"migration-plan", "compose-plan", "fleet-plan", "transition-plan", "instructions-plan"}:
            from .planning_commands import save_document

            output = Path(args.output).expanduser().absolute()
            if operation == "migration-plan":
                target = Path(result["request"]["target"]).resolve()
                candidate, _ = pack_migrations._candidate(result["request"])
                if output.resolve().is_relative_to(target) or output.resolve().is_relative_to(candidate.resolve()):
                    refuse("plan_output_overlaps_inputs")
            elif operation == "compose-plan" and any(output.resolve().is_relative_to(Path(row["path"]).resolve()) for row in result["members"]):
                refuse("plan_output_overlaps_inputs")
            elif operation == "fleet-plan":
                candidate, _ = fleet_revisions._candidate(result["request"]["candidate"])
                inputs = [Path(row["target"]) for row in result["proposals"]] + [candidate]
                if result["request"]["candidate"]["catalog"]:
                    inputs.append(Path(result["request"]["candidate"]["catalog"]))
                if any(output.resolve().is_relative_to(path.resolve()) for path in inputs):
                    refuse("plan_output_overlaps_inputs")
            elif operation == "transition-plan" and output.resolve().is_relative_to(Path(result["request"]["target"]).resolve()):
                refuse("plan_output_overlaps_inputs")
            elif operation == "instructions-plan" and output.resolve().is_relative_to(Path(result["request"]["root"]) / result["relative_path"]):
                refuse("plan_output_overlaps_inputs")
            save_document(result, output)
        if len(canonical(result)) > 1_048_576:
            refuse("extension_output_bound", "ONBOARDING_LIMIT")
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2 if args.format == "text" else None))
        return 0
    except EvidenceWikiError as error:
        print(json.dumps({"schema_version": "1.0", "error_code": error.error_code, "message": str(error),
            "recoverable": error.recoverable, "remediation": error.remediation, "details": error.details}, ensure_ascii=False))
        return error.exit_code
    except OSError:
        print(json.dumps({"schema_version": "1.0", "error_code": "ONBOARDING_ENVIRONMENT_INCOMPATIBLE",
            "message": "Selected local input or output is unavailable.", "recoverable": False,
            "remediation": "Inspect the explicit local paths and retained owner state.", "details": {"field": "extension_io"}}))
        return 2
    except SystemExit as error:
        if error.code == 0:
            return 0
        raise
