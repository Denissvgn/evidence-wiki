"""Portable resources and bounded calls to canonical EvidenceWiki CLI owners."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from pathlib import Path

from ._filesystem import os
from .agent_resources import resource_document
from .errors import UsageError

CALL_SCHEMA = "evidence-framework-call/v1"
RESULT_SCHEMA = "evidence-framework-result/v1"
MAX_INPUT = 65_536
MAX_OUTPUT = 2_097_152
TOOL_NAME = "evidence_wiki"
OPERATIONS = {
    "bootstrap": ("agent", "--format", "json"),
    "capabilities": ("agent", "summary", "--format", "json"),
    "resources": ("agent", "resources", "--format", "json"),
    "resource": ("agent", "resource"),
    "strict_check": ("strict", "check"),
    "strict_prepare_review": ("strict", "prepare-review"),
    "strict_export": ("strict", "export"),
    **{f"computation_{name}": ("computation", name) for name in ("check", "aggregate", "evaluate", "verify", "schedule")},
}


def refuse(reason: str) -> None:
    raise UsageError("ONBOARDING_ENVIRONMENT_INCOMPATIBLE", "Framework integration refused.", recoverable=False,
                     remediation="Use a qualified version and explicit local scope; inspect the compatibility matrix.",
                     details={"field": reason})


def tool_schemas() -> dict:
    def text(maximum=128):
        return {"type": "string", "minLength": 1, "maxLength": maximum}
    def obj(**properties):
        return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
    hashed = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    call = obj(schema_version={"const": CALL_SCHEMA}, request_id=text(),
               instruction_sha256=hashed, operation={"enum": list(OPERATIONS)},
               parameters={"type": "object", "additionalProperties": False, "properties": {
                   "resource_id": text(), "claim_id": text(), "as_of": text(64)}})
    result = obj(schema_version={"const": RESULT_SCHEMA}, request_id=text(), operation={"enum": list(OPERATIONS)},
                 exit_code={"type": "integer", "minimum": 0, "maximum": 255},
                 status={"enum": ["completed", "incomplete", "refused"]}, result_json=text(1_048_576),
                 result_sha256=hashed, instruction_sha256=hashed,
                 evidence_acceptance={"enum": ["not_evaluated", "eligible", "ineligible"]},
                 host_enforced={"const": False})
    return {key: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value}
            for key, value in ((CALL_SCHEMA, call), (RESULT_SCHEMA, result))}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            refuse("duplicate_json_key")
        result[key] = value
    return result


def decode_call(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_INPUT:
        refuse("call_size")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=lambda _: refuse("nonfinite_json"))
        if (not isinstance(value, dict) or set(value) != {"schema_version", "request_id", "instruction_sha256", "operation", "parameters"}
                or value["schema_version"] != CALL_SCHEMA or value["operation"] not in OPERATIONS
                or not isinstance(value["request_id"], str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value["request_id"]) is None
                or not isinstance(value["instruction_sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", value["instruction_sha256"]) is None):
            refuse("call_shape")
        params = value["parameters"]
        expected = {"resource_id"} if value["operation"] == "resource" else {"claim_id"} if value["operation"] == "strict_prepare_review" else set()
        optional = {"as_of"} if value["operation"].startswith("computation_") else set()
        if (not isinstance(params, dict) or not expected <= set(params) <= expected | optional
                or any(not isinstance(item, str) or not 1 <= len(item) <= 128
                       or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in item) for item in params.values())):
            refuse("call_parameters")
        return value
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        refuse("call_invalid")


def invoke(raw: bytes, *, target: str | Path, python: str | Path | None = None) -> dict:
    """Read-only native-tool mapping; mutations remain in their existing CLI owners."""
    call = decode_call(raw)
    guide = resource_document("guide/bootstrap/v1")
    if call["instruction_sha256"] != guide["sha256"]:
        refuse("instruction_changed")
    try:
        root = Path(target).expanduser().resolve(strict=True)
        interpreter = Path(python or sys.executable).expanduser().absolute()
    except (OSError, TypeError, ValueError):
        refuse("runtime_selection")
    if not root.is_dir() or not interpreter.is_file():
        refuse("runtime_selection")
    argv = [str(interpreter), "-m", "evidence_wiki.cli", *OPERATIONS[call["operation"]]]
    params = call["parameters"]
    if call["operation"] == "resource":
        argv.extend([params["resource_id"], "--format", "json"])
    elif call["operation"] == "bootstrap" or call["operation"].startswith(("strict_", "computation_")):
        argv.extend(["--target", str(root)])
    for key, flag in (("claim_id", "--claim-id"), ("as_of", "--as-of")):
        if key in params:
            argv.extend([flag, params[key]])
    # Reuse process cleanup and bounded capture; no shell or model invocation.
    from .orchestration import _execute_bounded

    if interpreter != Path(sys.executable).absolute():
        probe = _execute_bounded([str(interpreter), "-m", "evidence_wiki.cli", "agent", "resource", "guide/bootstrap/v1", "--format", "json"],
                                 cwd=root, stdin_text="", timeout_seconds=10, capture_limit=65_536, preserve_stdout=True)
        try:
            if (probe.returncode or probe.timed_out or probe.stdout_truncated
                    or json.loads(probe.stdout)["payload"]["sha256"] != guide["sha256"]):
                refuse("interpreter_instruction_mismatch")
        except (ValueError, KeyError, TypeError):
            refuse("interpreter_contract_unavailable")
    try:
        result = _execute_bounded(argv, cwd=root, stdin_text="", timeout_seconds=60, capture_limit=1_048_576, preserve_stdout=True)
    except (OSError, UnicodeError):
        refuse("canonical_transport_invalid")
    if result.timed_out or result.stdout_truncated or result.stderr_truncated:
        refuse("canonical_call_incomplete")
    try:
        owner = json.loads(result.stdout, object_pairs_hook=_unique,
                           parse_constant=lambda _: refuse("canonical_nonfinite_json"))
        if not isinstance(owner, dict) or not 0 <= result.returncode <= 255:
            refuse("canonical_result_shape")
    except (ValueError, TypeError, RecursionError):
        refuse("canonical_result_invalid")
    acceptance = "not_evaluated"
    if call["operation"] == "strict_export":
        if result.returncode == 0:
            from ._script_host import load_packaged_script, shared_assets_root

            contract = load_packaged_script(shared_assets_root(), "_strict_contract")
            try:
                schema = owner.get("schema_version")
                if schema not in {"evidence-strict-publication/v1", "evidence-strict-publication/v2"}:
                    refuse("strict_result_schema")
                contract.validate_shape(owner, contract.schema_documents()[schema])
            except (ValueError, KeyError, TypeError):
                refuse("strict_result_invalid")
        acceptance = "eligible" if result.returncode == 0 and owner.get("verdict") == "ship" else "ineligible"
    return {"schema_version": RESULT_SCHEMA, "request_id": call["request_id"], "operation": call["operation"],
            "exit_code": result.returncode,
            "status": "completed" if result.returncode == 0 else "refused" if "error_code" in owner else "incomplete",
            "result_json": result.stdout, "result_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "instruction_sha256": guide["sha256"], "evidence_acceptance": acceptance, "host_enforced": False}


def compatibility(*, framework: str | None = None, version: str | None = None, mode: str | None = None,
                  platform_id: str | None = None) -> dict:
    matrix = json.loads(resource_document("framework/compatibility/v1")["content"])
    try:
        if matrix["schema_version"] != "evidence-framework-compatibility/v1" or not 1 <= len(matrix["frameworks"]) <= 16:
            refuse("framework_matrix_invalid")
        seen = set()
        for row in matrix["frameworks"]:
            if (not isinstance(row, dict) or row["id"] in seen or not isinstance(row["modes"], dict)
                    or not 1 <= len(row["modes"]) <= 16 or not isinstance(row["source_basis"], list)
                    or not row["source_basis"] or re.fullmatch(r"[a-f0-9]{40}", row["commit"]) is None):
                refuse("framework_matrix_invalid")
            seen.add(row["id"])
            for qualification in row["modes"].values():
                if (qualification["status"] not in {"supported", "unsupported", "untested"} or not qualification["basis"]
                        or qualification["status"] == "supported"
                        and re.fullmatch(r"[a-f0-9]{64}", qualification.get("observation_sha256", "")) is None):
                    refuse("framework_matrix_evidence_invalid")
    except (ValueError, TypeError, KeyError, AttributeError):
        refuse("framework_matrix_invalid")
    if framework is None and version is None and mode is None and platform_id is None:
        return matrix
    rows = [row for row in matrix["frameworks"] if row["id"] == framework]
    if len(rows) != 1 or version != rows[0]["version"] or mode not in rows[0]["modes"]:
        refuse("framework_version_or_mode_unqualified")
    selected_platform = platform_id or f"{sys.platform}-{platform.machine().lower()}"
    if selected_platform not in rows[0]["platforms_observed"]:
        refuse("framework_platform_unqualified")
    selected = rows[0]["modes"][mode]
    if selected["status"] != "supported":
        refuse("framework_mode_" + selected["status"])
    return {"schema_version": matrix["schema_version"], "framework": framework, "version": version,
            "platform": selected_platform, "mode": mode, **selected}


def portable_bundle(guide: dict, extension: str, license_text: str) -> dict:
    """Derive a portable bundle from canonical content; no local installation occurs."""
    skill = f'''---
name: evidence-wiki
description: Investigate research questions with EvidenceWiki, retain verifiable evidence, evaluate declared computations, and report supported answers or explicit evidence gaps. Use when the user selects EvidenceWiki.
license: MIT
compatibility: Requires an installed EvidenceWiki package and a caller-selected Python environment. A second model CLI and MCP are optional.
metadata:
  version: "1.0"
  canonical-resource: "guide/bootstrap/v1"
  instruction-sha256: "{guide['sha256']}"
---

# EvidenceWiki

Read the [canonical operating guide](references/bootstrap.md) when this skill
is selected. Its resource links are installed IDs: retrieve them using the
package's `agent resource ID` command as needed. Use the Python interpreter
selected by the caller; do not assume a checkout or change global configuration.

Start with `evidence-wiki agent --format json` and compare the returned guide
digest with this skill's metadata. If it differs, retrieve a fresh bundle before
continuing. Preserve the selected strict policy, original question IDs, request
correlation and canonical workspace/run state through compaction or resume.
Reinspect current evidence and pending actions; never replay a mutation merely
because a conversation was compacted or a transport was interrupted.

The optional native tool offers bounded read/check/export operations through the
same CLI. Use existing authorized CLI/script owners for state changes. A tool
response, model brand, instruction match or session receipt does not confer
authority or host enforcement. Final accepted artifacts come from the strict
export owner; ordinary conversation remains outside that delivery boundary.
'''
    files = {"LICENSE.txt": license_text, "skills/evidence-wiki/SKILL.md": skill,
             "skills/evidence-wiki/references/bootstrap.md": guide["content"],
             "pi/evidence-wiki.js": extension.replace("__INSTRUCTION_SHA256__", guide["sha256"]),
             "tool-call.schema.json": json.dumps(tool_schemas()[CALL_SCHEMA], sort_keys=True, indent=2) + "\n",
             "tool-result.schema.json": json.dumps(tool_schemas()[RESULT_SCHEMA], sort_keys=True, indent=2) + "\n"}
    return {"schema_version": "evidence-framework-bundle/v1", "name": "evidence-wiki",
            "instruction_sha256": guide["sha256"], "files": files,
            "sha256": {key: hashlib.sha256(value.encode()).hexdigest() for key, value in files.items()}}


def validate_bundle(bundle: dict) -> None:
    import yaml

    try:
        files = bundle["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 8:
            refuse("bundle_files")
        if sum(len(value.encode()) for value in files.values()) > 131_072:
            refuse("bundle_size")
        for name, content in files.items():
            if (not isinstance(name, str) or not isinstance(content, str) or Path(name).is_absolute()
                    or "\\" in name or ":" in name or any(part in {"", ".", ".."} for part in name.split("/"))
                    or hashlib.sha256(content.encode()).hexdigest() != bundle["sha256"][name]):
                refuse("bundle_content")
        path = "skills/evidence-wiki/SKILL.md"
        front = files[path].split("---", 2)
        metadata = yaml.safe_load(front[1]) if len(front) == 3 and not front[0] else None
        if (not isinstance(metadata, dict) or set(metadata) != {"name", "description", "license", "compatibility", "metadata"}
                or metadata["name"] != Path(path).parent.name or metadata["name"] != "evidence-wiki"
                or not isinstance(metadata["description"], str) or not 1 <= len(metadata["description"]) <= 1024
                or not isinstance(metadata["compatibility"], str) or not 1 <= len(metadata["compatibility"]) <= 500
                or not isinstance(metadata["metadata"], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata["metadata"].items())
                or metadata["metadata"]["instruction-sha256"] != bundle["instruction_sha256"]
                or hashlib.sha256(files["skills/evidence-wiki/references/bootstrap.md"].encode()).hexdigest() != bundle["instruction_sha256"]):
            refuse("portable_skill_invalid")
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError, IndexError, yaml.YAMLError):
        refuse("bundle_invalid")


def export_bundle(target: str | Path) -> dict:
    """Create only a new caller-selected bundle directory; never merge instructions."""
    import shutil
    import tempfile

    bundle = json.loads(resource_document("framework/bundle/v1")["content"])
    validate_bundle(bundle)
    destination = Path(target).expanduser().absolute()
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if destination.exists() or destination.is_symlink():
        refuse("bundle_destination_exists")
    # A private adjacent tree is published once. Existing content is never replaced.
    stage = Path(tempfile.mkdtemp(prefix=".evidence-wiki-bundle-", dir=parent))
    descriptors = []
    try:
        for name, content in bundle["files"].items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        # mkdir reserves the destination; refusing existing names avoids replacing
        # a concurrent empty directory, which POSIX rename alone would allow.
        if os.open not in os.supports_dir_fd or os.rename not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            refuse("bundle_publication_platform_unsupported")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(parent, flags)
        descriptors.append(parent_fd)
        os.mkdir(destination.name, dir_fd=parent_fd)
        destination_fd = os.open(destination.name, flags, dir_fd=parent_fd)
        descriptors.append(destination_fd)
        stage_fd = os.open(stage, flags)
        descriptors.append(stage_fd)
        identity = os.fstat(destination_fd)
        for child in stage.iterdir():
            os.rename(child.name, child.name, src_dir_fd=stage_fd, dst_dir_fd=destination_fd)
        try:
            observed = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            refuse("bundle_destination_changed")
        if (observed.st_dev, observed.st_ino) != (identity.st_dev, identity.st_ino):
            refuse("bundle_destination_changed")
        return {"schema_version": "evidence-framework-bundle-result/v1", "status": "created",
                "instruction_sha256": bundle["instruction_sha256"], "files": bundle["sha256"]}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        shutil.rmtree(stage)


def main(argv: list[str]) -> int:
    import argparse
    import signal

    from .agent import _Parser
    from .errors import EvidenceWikiError

    parser = _Parser(prog="evidence-wiki agent")
    parser.add_argument("operation", choices=("frameworks", "bundle", "invoke"))
    parser.add_argument("--target")
    parser.add_argument("--framework")
    parser.add_argument("--version")
    parser.add_argument("--mode")
    parser.add_argument("--platform", dest="platform_id")
    parser.add_argument("--format", choices=("json",), default="json")
    try:
        args = parser.parse_args(argv)
        if args.operation == "frameworks":
            if args.target:
                refuse("framework_options")
            result = compatibility(framework=args.framework, version=args.version, mode=args.mode, platform_id=args.platform_id)
        else:
            if args.framework or args.version or args.mode or args.platform_id:
                refuse("framework_options")
            if args.operation == "bundle":
                result = export_bundle(args.target) if args.target else json.loads(resource_document("framework/bundle/v1")["content"])
            else:
                if not args.target:
                    refuse("workspace_scope_required")
                previous = signal.getsignal(signal.SIGTERM)
                def interrupted(signum, _frame):
                    raise SystemExit(128 + signum)
                try:
                    signal.signal(signal.SIGTERM, interrupted)
                    result = invoke(sys.stdin.buffer.read(MAX_INPUT + 1), target=args.target)
                finally:
                    signal.signal(signal.SIGTERM, previous)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(rendered.encode()) + 1 > MAX_OUTPUT:
            refuse("framework_output_bound")
        print(rendered)
        return 0
    except (EvidenceWikiError, OSError, ValueError, argparse.ArgumentError) as error:
        code = getattr(error, "error_code", "ONBOARDING_ENVIRONMENT_INCOMPATIBLE")
        print(json.dumps({"schema_version": "1.0", "error_code": code, "message": "Framework integration refused.",
                          "recoverable": False, "remediation": "Inspect the qualified framework contract and selected local paths.",
                          "details": getattr(error, "details", {"field": "framework_environment"})}))
        return 2
