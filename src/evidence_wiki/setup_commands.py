"""Installed apply and recovery entry points with bounded content-free refusals."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ._pack_io import read_file
from ._script_host import shared_assets_root
from .agent import _Parser
from .errors import ONBOARDING_ERROR_CONTRACTS, default_exit_code, default_recoverable
from .setup_application import apply_plan
from .setup_contracts import contract_index, schema_document


def main(operation, argv):
    parser = _Parser(prog="evidence-wiki agent " + operation)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    if operation == "apply":
        parser.add_argument("--from-file", required=True, help="Saved plan; the same command resumes owned setup.")
    if operation == "setup-schemas":
        parser.add_argument("--schema-id")
    try:
        args = parser.parse_args(argv)
        if operation == "setup-guide":
            content = read_file(shared_assets_root(), "workspace-template/docs/workspace-application.md").decode()
            result = {"schema_version": "evidence-setup-guide/v1", "content": content}
        elif operation == "setup-schemas":
            result = schema_document(args.schema_id) if args.schema_id else contract_index()
        else:
            path = Path(args.from_file).expanduser().absolute()
            result = apply_plan(read_file(path.parent, path.name))
        if args.format == "text" and operation != "setup-schemas":
            print(result["content"].rstrip() if operation == "setup-guide" else
                  f"Setup: {result['status']}\nTarget: {result['target']}\nResearch complete: false\nReceipt: "
                  + str(Path(result["checkpoint"]).with_name("receipt.json")))
        else:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 2 if result.get("status") == "failed" else 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        code = getattr(error, "error_code", "ONBOARDING_WRITE_FAILED" if isinstance(error, OSError) else "ONBOARDING_INVALID")
        if code not in ONBOARDING_ERROR_CONTRACTS:
            code = "ONBOARDING_INVALID"
        details = getattr(error, "details", {})
        field = details.get("field", "setup_input_or_state_invalid") if isinstance(details, dict) else "setup_input_or_state_invalid"
        if not isinstance(field, str) or not re.fullmatch(r"[a-zA-Z0-9_/*.-]{1,256}", field):
            field = "setup_owner_refusal"
        print(json.dumps({"schema_version": "1.0", "error_code": code, "message": "Workspace setup refused; existing artifacts preserved.",
            "details": {"field": field}, "recoverable": default_recoverable(code),
            "remediation": "Read agent setup-guide. Retry the same plan only with unchanged owned state; inspect partial writes or choose a fresh target and plan."}))
        return default_exit_code(code)
