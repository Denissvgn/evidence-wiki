"""Bounded CLI and exclusive atomic publication for saved research setup plans."""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from pathlib import Path

from ._filesystem import os
from ._pack_io import canonical, identity, read_file, relative_path
from ._script_host import shared_assets_root
from .agent import _Parser
from .errors import ONBOARDING_ERROR_CONTRACTS, default_exit_code, default_recoverable
from .pack_catalog import _outside_assets, _writer_flags
from .planning import check_plan, compile_plan
from .planning_contracts import MAX_BYTES, contract_index, refuse, schema_document


def save_plan(plan, output):
    path = Path(output).expanduser().absolute()
    relative_path(path.name)
    parent = path.parent.resolve(strict=True)
    path = parent / path.name
    target = plan["bindings"]["target"]["target"]
    target_path = Path(target["writable_root"]) / target["relative_path"]
    if path == target_path or target_path in path.parents:
        refuse("plan_output_inside_target_forbidden")
    for source in plan["bindings"]["inputs"]:
        if source.get("root") and path == Path(source["root"]) / source["path"]:
            refuse("plan_output_collides_with_source_input")
    return save_document(plan, path)


def save_document(plan, output):
    """Publish a new bounded JSON artifact through a pinned parent descriptor."""
    path = Path(output).expanduser().absolute()
    relative_path(path.name)
    parent = path.parent.resolve(strict=True)
    path = parent / path.name
    _outside_assets(path, additional_roots=(Path(__file__).parent,))
    raw = canonical(plan) + b"\n"
    if len(raw) > MAX_BYTES:
        refuse("planning_output_bound", "ONBOARDING_LIMIT")
    before = identity(parent)
    directory = os.open(parent, _writer_flags())
    temporary = ".evidence-plan-" + uuid.uuid4().hex
    try:
        _outside_assets(directory, additional_roots=(Path(__file__).parent,))
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if identity(parent) != before:
            refuse("plan_output_parent_changed", "ONBOARDING_PLAN_STALE")
        try:
            os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            refuse("plan_output_exists", "ONBOARDING_TARGET_CONFLICT")
        os.fsync(directory)
        if identity(parent) != before or read_file(directory, path.name) != raw:
            refuse("plan_output_changed", "ONBOARDING_PLAN_STALE")
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)
    return str(path)


def main(operation, argv):
    parser = _Parser(prog="evidence-wiki agent " + operation)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    if operation in {"plan", "plan-check"}:
        parser.add_argument("--from-file", required=True)
    if operation == "plan":
        parser.add_argument("--output", help="Save one immutable plan to a new caller-selected file; parent must exist.")
    if operation == "plan-schemas":
        parser.add_argument("--schema-id")
    try:
        args = parser.parse_args(argv)
        if operation == "plan-guide":
            content = read_file(shared_assets_root(), "workspace-template/docs/research-planning.md").decode()
            result = {"schema_version": "evidence-planning-guide/v1", "content": content}
        elif operation == "plan-schemas":
            result = schema_document(args.schema_id) if args.schema_id else contract_index()
        else:
            path = Path(args.from_file).expanduser().absolute()
            raw = read_file(path.parent, path.name)
            result = compile_plan(raw) if operation == "plan" else check_plan(raw)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False,
                              **({"separators": (",", ":")} if args.format == "json" else {"indent": 2}))
        if operation == "plan" and args.format == "text":
            rendered = "\n".join(["Research setup plan " + result["plan_id"],
                "Setup: " + ("ready" if result["setup_ready"] else "blocked"),
                f"Questions: {result['questions']['original_count']} original, {result['questions']['derived_count']} derived",
                "Initializer dry-run: passed. Target actions: not run. Research and release: pending.",
                *[f"- {row['stage']}: {row['reason']} ({','.join(row['question_ids']) or 'all'}; {row['field']})" for row in result["blockers"]]])
        elif operation == "plan-guide" and args.format == "text":
            rendered = result["content"].rstrip()
        if len(rendered.encode()) + 1 > MAX_BYTES:
            refuse("planning_output_bound", "ONBOARDING_LIMIT")
        if operation == "plan" and args.output:
            save_plan(result, args.output)
        print(rendered)
        return 0
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        code = getattr(error, "error_code", "ONBOARDING_INVALID")
        if code not in ONBOARDING_ERROR_CONTRACTS:
            code = "ONBOARDING_INVALID"
        details = getattr(error, "details", {})
        field = details.get("field", "planning_input_or_environment_invalid") if isinstance(details, dict) else "planning_input_or_environment_invalid"
        if not isinstance(field, str) or re.fullmatch(r"[a-zA-Z0-9_/*.-]{1,256}", field) is None:
            field = "planning_owner_refusal"
        print(json.dumps({"schema_version": "1.0", "error_code": code, "message": "Research planning request refused.",
            "details": {"field": field}, "recoverable": default_recoverable(code),
            "remediation": "Inspect the named input and current plan preconditions; use agent plan-guide and plan-schemas."}))
        return default_exit_code(code)
