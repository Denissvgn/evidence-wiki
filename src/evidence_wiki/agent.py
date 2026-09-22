"""Read-only installed-agent bootstrap, independent of workspace execution."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

from . import __version__
from ._agent_catalog import COMPUTATION_SCHEMAS, COMPUTATION_SCRIPTS, STRICT_SCHEMAS
from .agent_resources import (
    _asset_tree,
    _read,
    installation_metadata,
    resource_availability,
    resource_document,
    resource_index,
    runtime_available,
)
from .errors import EvidenceWikiError, UsageError
from .onboarding_contract import encode_document

SUMMARY_BYTES = 32_768
OPERATIONS = (
    ("agent", "read", False), ("agent summary", "read", False), ("agent resources", "read", False),
    ("agent resource", "read", False), ("contract", "read", False),
    ("agent frameworks", "read", False), ("agent bundle", "depends_on_options", False), ("agent invoke", "read", True),
    ("init", "depends_on_options", False), ("deploy", "depends_on_options", False),
    ("pack validate", "temporary_write", False), ("pack refresh", "depends_on_options", True),
    ("pack list", "depends_on_options", False), ("pack show", "depends_on_options", False),
    ("pack schemas", "read", False), ("pack guide", "read", False),
    ("pack catalog init", "write", False), ("pack catalog register", "write", False),
    ("pack catalog list", "read", False), ("pack decide", "temporary_write", False),
    ("doctor", "temporary_write", True), ("status", "depends_on_options", True), ("questions add", "write", True),
    ("strict check", "read", True), ("strict prepare-review", "read", True),
    ("strict review", "write", True), ("strict export", "read", True),
    *((f"computation {operation}", "read", operation != "schemas")
      for operation in ("schemas", "check", "aggregate", "evaluate", "verify", "schedule")),
    *((f"computation {operation}", "write", True) for operation in ("write", "apply-warnings", "dispatch")),
)


def _refuse(reason: str, code: str = "ONBOARDING_ENVIRONMENT_INCOMPATIBLE") -> None:
    raise UsageError(code, "Agent bootstrap request refused.", recoverable=False,
                     remediation="Consult agent summary and select an implemented, independently verified route.",
                     details={"field": reason})


def _checker(ids: tuple[str, ...], availability: dict[str, bool]) -> dict:
    if not all(availability.get(resource_id, False) for resource_id in ids):
        return {"available": False, "basis": "required checker asset unavailable", "runtime_verified": False}
    return {"available": True, "basis": "required packaged checker files present; not executed", "runtime_verified": False}


def capabilities() -> dict:
    """Return a bounded capability payload without building the full contract."""
    index = resource_index()
    availability = resource_availability()
    if not all(availability.values()):
        _refuse("required_resource_missing")
    if not runtime_available():
        _refuse("required_runtime_missing")
    installation = installation_metadata()
    if installation["package_version"] != __version__:
        _refuse("installation_catalog_version_mismatch")
    from .frameworks import compatibility

    matrix = compatibility()
    return {
        "installation": installation, "resources": index["resources"],
        "schema_ids": [entry["id"] for entry in index["resources"] if entry["media_type"] == "application/schema+json"],
        "operations": [{"name": name, "effect": effect, "workspace_required": workspace} for name, effect, workspace in OPERATIONS],
        "strict": {
            "capability": "strict-evidence/v1", "schemas": list(STRICT_SCHEMAS),
            "checker": _checker(("script/_strict_contract/v1", "script/_strict_evidence/v1", "script/strict_evidence/v1"), availability),
            "default_request_schema": "onboarding/research_request/v2",
            "assurance_modes": ["artifact_checked", "host_enforced"],
            "host_api": "evidence_wiki.strict_host.StrictResearchHost", "host_platform": "darwin-sbpl",
            "host_probe": "not_run", "protected_parent_orders": False, "semantic_truth_guarantee": False,
        },
        "computation": {
            "capability": "declarative-computation/v1", "schemas": list(COMPUTATION_SCHEMAS),
            "checker": _checker(tuple(f"script/{stem}/v1" for stem in COMPUTATION_SCRIPTS), availability),
            "numeric_wire": "finite Decimal strings; bounded control integers; explicit exact/rounded policy",
            "clock": "explicit as_of; tzdata version and zone bytes pinned in results; explicit DST ambiguity policy",
            "evidence_limit": "arithmetic and lineage do not prove source truth, units or semantic support",
        },
        "frameworks": {"qualified": [row["id"] + "/" + row["version"] + "/" + platform_id + "/" + name for row in matrix["frameworks"]
                                      for platform_id in row["platforms_observed"]
                                      for name, mode in row["modes"].items() if mode["status"] == "supported"],
                       "basis": "per-version and per-mode observed conformance only; retrieve framework/compatibility/v1"},
        "limits": {"summary_bytes": SUMMARY_BYTES, "resource_bytes": 1_048_576},
    }


def _negotiate(summary: dict, requirements: list[str], assurance: str) -> None:
    if not isinstance(requirements, list) or len(requirements) > 64 or any(not isinstance(item, str) or len(item) > 128 for item in requirements):
        _refuse("requirements_bound", "ONBOARDING_LIMIT")
    if assurance == "host_enforced":
        _refuse("host_enforcement_not_verified")
    if assurance != "artifact_checked":
        _refuse("assurance_unsupported")
    supported = {row["name"] for row in summary["operations"]} | set(summary["schema_ids"])
    supported.add("installed-agent/v1")
    supported.add("pack-discovery/v1")
    for key in ("strict", "computation"):
        if summary[key]["checker"]["available"]:
            supported.add(summary[key]["capability"])
    for index, requirement in enumerate(requirements):
        if requirement not in supported:
            _refuse(f"unsupported_requirement/{index}")
        if requirement in summary["schema_ids"]:
            resource_document(requirement)
        for prefix, owner in (("strict ", "strict"), ("computation ", "computation")):
            if requirement.startswith(prefix) and not summary[owner]["checker"]["available"]:
                _refuse(f"checker_unavailable/{index}")


def _workspace_observation(target: str) -> tuple[str, dict]:
    environment = {"implementation": platform.python_implementation(), "platform": platform.system(),
                   "workspace_version": None, "workspace_schema_version": None,
                   "observation": "workspace markers only; configuration, policy and readiness not inspected"}
    try:
        root = Path(target).expanduser().resolve()
        tree = _asset_tree()
        if isinstance(tree, Path) and root == (tree / "workspace-template").resolve():
            environment["observation"] = "installed starter is a template, not a research workspace"
            return "not_inspected", environment
        if not root.exists():
            return "absent", environment
        if not root.is_dir():
            return "invalid", environment
        markers = [root / name for name in ("research.yml", "workspace-system.yml")]
        if not any(path.exists() or path.is_symlink() for path in markers):
            return "absent", environment
        if any(path.is_symlink() or not path.is_file() for path in markers):
            return "invalid", environment
        data = _read(root, "workspace-system.yml")
        if len(data) > 8192:
            return "invalid", environment
        import yaml

        document = yaml.safe_load(data)
        if not isinstance(document, dict) or not isinstance(document.get("workspace_system"), dict):
            return "invalid", environment
        metadata = document["workspace_system"]
        values = [metadata.get(key) for key in ("starter_version", "schema_version")]
        if any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in values):
            return "invalid", environment
        environment.update(workspace_version=values[0], workspace_schema_version=values[1])
        return "present", environment
    except (OSError, ValueError, KeyError, TypeError, ImportError, RecursionError, EvidenceWikiError):
        return "invalid", environment
    except yaml.YAMLError:
        return "invalid", environment


def bootstrap(target: str = ".", *, requirements: list[str] | None = None,
              assurance: str = "artifact_checked") -> dict:
    """Observe the explicit target and return guide/template identity, without writes."""
    summary = capabilities()
    _negotiate(summary, requirements or [], assurance)
    if not summary["strict"]["checker"]["available"]:
        _refuse("strict_checker_unavailable")
    guide = resource_document("guide/bootstrap/v1")
    template = resource_document("example/strict-policy/v1")
    policy = json.loads(template["content"])
    observed = {entry["id"]: entry for entry in summary["resources"]}
    for document in (guide, template):
        if observed[document["id"]] != {key: value for key, value in document.items() if key != "content"}:
            _refuse("resource_snapshot_changed")
    if policy["instructions"] != {"docs/installed-agent.md": "sha256:" + guide["sha256"]}:
        _refuse("policy_instruction_identity_mismatch")
    state, environment = _workspace_observation(target)
    return {
        "installation": summary["installation"], "python_version": platform.python_version(), "workspace": state,
        "guide": guide, "schema_ids": summary["schema_ids"],
        "supported_operations": [row["name"] for row in summary["operations"]],
        "resources": summary["resources"], "environment": environment,
        "strict_selection": {
            "mode": "strict", "requested_assurance": assurance, "effective_assurance": None,
            "selection_scope": "new_workspace_template", "policy_id": policy["policy_id"],
            "policy_revision": policy["revision"], "policy_sha256": template["sha256"],
            "instruction": {key: value for key, value in guide.items() if key != "content"},
            "route": "configure strict policy; check, independently review, export through strict owner",
            "reason": "template selected only; existing policy, authority and protected host not inspected",
        },
        "limitations": ["Bootstrap does not initialize, validate readiness, run research or confer execution authority.",
                        "No truth guarantee; framework modes need separate qualification and host enforcement needs protected execution."],
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _refuse("arguments", "ONBOARDING_INVALID")


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] in {"frameworks", "bundle", "invoke"}:
        from .frameworks import main as frameworks_main

        return frameworks_main(argv)
    parser = _Parser(prog="evidence-wiki agent", description=__doc__,
                     epilog="Integrations: agent frameworks; agent bundle [--target NEW_DIRECTORY]; agent invoke --target WORKSPACE (JSON stdin).")
    parser.add_argument("operation", nargs="?", default="bootstrap", choices=("bootstrap", "summary", "resources", "resource"))
    parser.add_argument("resource_id", nargs="?")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--target", default=None)
    parser.add_argument("--require", action="append", default=[])
    parser.add_argument("--assurance", default="artifact_checked", choices=("artifact_checked", "host_enforced"))
    parser.add_argument("--request-id", default="bootstrap")
    try:
        args = parser.parse_args(argv)
        if ((args.operation == "resource") != bool(args.resource_id)
                or args.operation != "bootstrap" and args.target is not None
                or args.operation in {"resource", "resources"} and (args.require or args.assurance != "artifact_checked")):
            _refuse("arguments", "ONBOARDING_INVALID")
        if args.operation == "bootstrap":
            kind, version, payload = "bootstrap", "2", bootstrap(args.target or ".", requirements=args.require, assurance=args.assurance)
        elif args.operation == "summary":
            kind, version, payload = "capabilities", "1", capabilities()
            _negotiate(payload, args.require, args.assurance)
        elif args.operation == "resources":
            kind, version, payload = "resources", "1", resource_index()
            payload.pop("schema_version")
        else:
            kind, version, payload = "resource", "2", resource_document(args.resource_id)
        document = {"schema_version": version + ".0", "kind": kind, "request_id": args.request_id, "payload": payload}
        rendered = encode_document(f"onboarding/{kind}/v{version}", document)
        if kind in {"bootstrap", "capabilities", "resources"} and len(rendered) + 1 > SUMMARY_BYTES:
            _refuse("summary_output_bound", "ONBOARDING_LIMIT")
        if args.format == "json":
            print(rendered.decode())
        elif kind == "bootstrap":
            print(f"EvidenceWiki {__version__}; workspace: {payload['workspace']}; assurance: not established\n")
            print(payload["guide"]["content"], end="")
        elif kind == "resource":
            print(payload["content"], end="")
        else:
            print(json.dumps(document, indent=2))
        return 0
    except EvidenceWikiError as error:
        # Refusals use one document on stdout even for malformed JSON-mode arguments.
        print(json.dumps({"schema_version": "1.0", "error_code": error.error_code, "message": error.message,
                          "recoverable": error.recoverable, "remediation": error.remediation, "details": error.details}))
        return error.exit_code
