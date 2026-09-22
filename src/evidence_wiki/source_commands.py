"""Bounded CLI boundary for declared capabilities, scoped sources and delivery."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ._pack_io import read_file
from ._script_host import shared_assets_root
from .agent import _Parser
from .errors import ONBOARDING_ERROR_CONTRACTS, default_exit_code, default_recoverable
from .host_capabilities import known_credentials
from .onboarding_contract import _matches
from .source_contracts import MAX_DOCUMENT, contract_index, refuse, schema_document


def _input(path):
    selected = Path(path).expanduser().absolute()
    return read_file(selected.parent, selected.name)


def _no_secret_values(value):
    secrets = known_credentials()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and any(secret and len(secret) >= 6 and secret in item for secret in secrets):
            refuse("source_credential_value_forbidden")


def main(operation, argv):
    parser = _Parser(prog="evidence-wiki agent " + operation)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    if operation in {"inspect", "source-status", "routes", "capture"}:
        parser.add_argument("--target", required=operation in {"source-status", "capture"})
        parser.add_argument("--host-tools")
    if operation in {"inspect", "source-status"}:
        parser.add_argument("--source-id", action="append", default=[])
        parser.add_argument("--source-path", action="append", default=[])
    if operation in {"inspect", "routes"}:
        parser.add_argument("--probe-provider", action="append", default=[], metavar="PHASE:DISTRIBUTION/ENTRYPOINT",
                            help="Explicitly load a selected installed provider in a bounded credential-stripped process; no fetch is invoked.")
    if operation == "inspect":
        parser.add_argument("--probe-tool", choices=("git", "pdftotext"), action="append", default=[],
                            help="Explicitly run this tool's fixed version command in a temporary directory.")
    if operation in {"routes", "capture"}:
        parser.add_argument("--from-file", required=True)
    if operation == "capture":
        parser.add_argument("--path", required=True, help="A new capture path beneath a configured raw root, outside raw/links.")
    if operation == "source-schemas":
        parser.add_argument("--schema-id")
    try:
        args = parser.parse_args(argv)
        hosts = _input(args.host_tools) if getattr(args, "host_tools", None) else None
        if operation == "source-guide":
            content = read_file(shared_assets_root(), "workspace-template/docs/source-usability.md").decode("utf-8")
            if args.format == "text":
                print(content, end="")
                return 0
            result = {"schema_version": "evidence-source-guide/v1", "content": content}
        elif operation == "source-schemas":
            result = schema_document(args.schema_id) if args.schema_id else contract_index()
        elif operation in {"inspect", "source-status"}:
            if operation == "source-status" and not (args.source_id or args.source_path):
                refuse("source_status_requires_selection")
            from .source_inspection import inspect

            result = inspect(target=args.target, host_tools=hosts, source_ids=args.source_id, source_paths=args.source_path,
                probe_tools=getattr(args, "probe_tool", []), probe_providers=getattr(args, "probe_provider", []))
        elif operation == "routes":
            from .source_routing import plan

            result = plan(_input(args.from_file), target=args.target, host_tools=hosts, probe_providers=args.probe_provider)
        elif operation == "capture":
            from .source_delivery import deliver

            result = deliver(_input(args.from_file), target=args.target, path=args.path, host_tools=hosts)
        else:
            refuse("source_operation_unknown")
        _no_secret_values(result)
        if operation in {"inspect", "source-status", "routes", "capture"}:
            _matches(result, schema_document(result["schema_version"]))
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, **({"separators": (",", ":")} if args.format == "json" else {"indent": 2}))
        if len(rendered.encode()) + 1 > MAX_DOCUMENT:
            refuse("source_output_bound", "ONBOARDING_LIMIT")
        print(rendered)
        return 0
    except Exception as error:
        code = getattr(error, "error_code", "ONBOARDING_INVALID")
        if getattr(error, "contended", False):
            code = "ONBOARDING_LOCK_BUSY"
        if code not in ONBOARDING_ERROR_CONTRACTS:
            code = "ONBOARDING_ENVIRONMENT_INCOMPATIBLE"
        details = getattr(error, "details", {})
        reason = details.get("field", "source_input_or_environment_invalid") if isinstance(details, dict) else "source_input_or_environment_invalid"
        if not isinstance(reason, str) or re.fullmatch(r"[a-zA-Z0-9_/*.-]{1,256}", reason) is None:
            reason = "source_owner_refusal"
        print(json.dumps({"schema_version": "1.0", "error_code": code, "message": "Source capability request refused.",
            "recoverable": default_recoverable(code), "details": {"field": reason},
            "remediation": "Inspect the explicit source/configuration scope and use a supported owner; do not infer authority or successful evidence."}))
        return default_exit_code(code)
