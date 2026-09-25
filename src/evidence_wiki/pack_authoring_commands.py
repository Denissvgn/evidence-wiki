"""Bounded transport for explicit pack authoring, assessment and plan resumption."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ._pack_io import canonical, read_file
from .agent import _Parser
from .errors import ONBOARDING_ERROR_CONTRACTS, default_exit_code, default_recoverable
from .pack_authoring_contracts import MAX_BYTES, refuse
from .source_commands import _no_secret_values

OPERATIONS = {"scaffold", "derive", "qualify", "freeze-cases", "assess", "accept", "resume"}


def _input(path):
    selected = Path(path).expanduser().absolute()
    return read_file(selected.parent, selected.name)


def main(operation, argv):
    parser = _Parser(prog="evidence-wiki pack " + operation)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    if operation in {"scaffold", "derive", "freeze-cases", "resume"}:
        parser.add_argument("--from-file", required=True)
    if operation in {"scaffold", "derive", "resume"}:
        parser.add_argument("--output", required=operation != "resume")
    if operation in {"qualify", "freeze-cases", "assess", "accept"}:
        parser.add_argument("--draft", required=True, help="Caller-owned draft container; its records are outside packs/NAME.")
    if operation == "assess":
        parser.add_argument("--observations", help="Optional caller-declared semantic judgments; these are not authenticated approvals.")
    if operation in {"accept", "resume"}:
        parser.add_argument("--catalog", required=True)
        parser.add_argument("--id", dest="revision", required=True)
    if operation == "accept":
        parser.add_argument("--assessment-id", required=True)
        parser.add_argument("--root-id", required=True)
        parser.add_argument("--scope", required=True)
    try:
        args = parser.parse_args(argv)
        code = 0
        if operation in {"scaffold", "derive"}:
            from . import pack_authoring

            result = getattr(pack_authoring, operation)(_input(args.from_file), output=args.output)
        elif operation == "qualify":
            from .pack_qualification import qualify_draft

            result = qualify_draft(args.draft)
            code = 0 if result["validation"]["ok"] else 1
        elif operation in {"freeze-cases", "assess"}:
            from .pack_assessment import assess, freeze_cases

            if operation == "freeze-cases":
                result = freeze_cases(args.draft, _input(args.from_file))
            else:
                result = assess(args.draft, observations=_input(args.observations) if args.observations else None)
                code = 1 if result["assessment"]["gaps"] else 0
        else:
            from .pack_acceptance import accept, resume

            if operation == "accept":
                result = accept(args.draft, assessment_id=args.assessment_id, catalog=args.catalog,
                    root_id=args.root_id, revision=args.revision, scope=args.scope)
            else:
                result = resume(_input(args.from_file), catalog=args.catalog, revision=args.revision)
        _no_secret_values(result)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False,
            **({"separators": (",", ":")} if args.format == "json" else {"indent": 2}))
        if len(rendered.encode()) + 1 > MAX_BYTES or len(canonical(result)) + 1 > MAX_BYTES:
            refuse("pack_authoring_output_bound", "ONBOARDING_LIMIT")
        if operation == "resume" and args.output:
            from .planning_commands import save_plan

            save_plan(result, args.output)
        print(rendered)
        return code
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        code = getattr(error, "error_code", "ONBOARDING_INVALID")
        if code not in ONBOARDING_ERROR_CONTRACTS:
            code = "ONBOARDING_INVALID"
        if getattr(error, "contended", False):
            code = "ONBOARDING_LOCK_BUSY"
        details = getattr(error, "details", {})
        field = details.get("field", "pack_authoring_input_or_environment_invalid") if isinstance(details, dict) else "pack_authoring_input_or_environment_invalid"
        if not isinstance(field, str) or re.fullmatch(r"[a-zA-Z0-9_/*.-]{1,256}", field) is None:
            field = "pack_authoring_owner_refusal"
        print(json.dumps({"schema_version": "1.0", "error_code": code, "message": "Pack authoring request refused.",
            "details": {"field": field}, "recoverable": default_recoverable(code),
            "remediation": "Inspect current draft/case identities. Preserve partial outputs and choose a new container; never force overwrite."}))
        return default_exit_code(code)
