"""Command-line entry points for deploying research workspaces."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from . import __version__
from .resources import ORCHESTRATOR_SKILL, STARTER_DIR, assets_root, orchestrator_skill_path, required_asset_manifest

CONTRACT_SCHEMA_VERSION = "1.0"
_SCRIPT_MODULE_CACHE: dict[str, ModuleType] = {}
_LOADER_MODULE_CACHE: dict[str, ModuleType] = {}


def _load_workspace_loader(script_dir: Path) -> ModuleType:
    root = script_dir.expanduser().resolve()
    path = root / "_workspace_module_loader.py"
    if not path.is_file():
        raise SystemExit(f"Missing packaged script loader: {path}")
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    key = f"{root}\0{content_hash}"
    if key in _LOADER_MODULE_CACHE:
        return _LOADER_MODULE_CACHE[key]
    module_name = f"_evidence_wiki_loader_{abs(hash(key))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load packaged script loader: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    _LOADER_MODULE_CACHE[key] = module
    return module


def _load_script(script_path: Path, module_name: str) -> ModuleType:
    if not script_path.is_file():
        raise SystemExit(f"Missing packaged script: {script_path}")
    del module_name  # retained for the stable internal call signature
    loader = _load_workspace_loader(script_path.parent)
    return loader.load_workspace_module(script_path.parent, script_path.stem, cache=_SCRIPT_MODULE_CACHE)


def _load_initializer(starter_root: Path) -> ModuleType:
    return _load_script(
        starter_root / "scripts" / "init_research_workspace.py",
        "evidence_wiki_initializer",
    )


def _pack_policy_vocabularies(root: Path, coverage_module: ModuleType, yaml_module: ModuleType) -> dict[str, dict[str, dict[str, str]]]:
    domain_packs_root = root / "domain-packs"
    result: dict[str, dict[str, dict[str, str]]] = {}
    if not domain_packs_root.is_dir():
        return result
    for overlay_path in sorted(domain_packs_root.glob("*/research.overlay.yml")):
        try:
            document = yaml_module.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
        except yaml_module.YAMLError:
            continue
        if not isinstance(document, dict):
            continue
        domain_pack = document.get("domain_pack")
        if not isinstance(domain_pack, dict):
            continue
        name = domain_pack.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            vocabularies = coverage_module.domain_pack_policy_vocabularies({"domain_pack": domain_pack})
        except coverage_module.CoverageManifestError:
            continue
        if any(vocabularies.values()):
            result[name.strip()] = vocabularies
    return result


def _has_starter_root(args: list[str]) -> bool:
    return any(arg == "--starter-root" or arg.startswith("--starter-root=") for arg in args)


def _run_initializer(forwarded_args: list[str]) -> int:
    with assets_root() as root:
        starter_root = root / STARTER_DIR
        args = list(forwarded_args)
        if not _has_starter_root(args):
            args = ["--starter-root", str(starter_root), *args]
        initializer = _load_initializer(starter_root)
        try:
            return int(initializer.main(args) or 0)
        except SystemExit as exc:
            if not isinstance(exc.code, str):
                raise
            return int(initializer.emit_initializer_error(exc.code, operation="initialization"))


def _run_upgrader(forwarded_args: list[str]) -> int:
    with assets_root() as root:
        starter_root = root / STARTER_DIR
        args = list(forwarded_args)
        if not _has_starter_root(args):
            args = ["--starter-root", str(starter_root), *args]
        initializer = _load_initializer(starter_root)
        try:
            return int(initializer.upgrade_main(args) or 0)
        except initializer.UpgradeWriteError as exc:
            return int(
                initializer.emit_initializer_error(
                    str(exc),
                    operation="upgrade",
                    error_code=exc.error_code,
                    remediation=exc.remediation,
                    details=exc.details,
                )
            )
        except initializer.LockUnavailableError as exc:
            return int(
                initializer.emit_initializer_error(
                    str(exc),
                    operation="upgrade",
                    error_code=exc.error_code,
                    remediation=exc.remediation,
                    details=exc.details,
                )
            )
        except OSError as exc:
            reason = " ".join(str(exc.strerror or type(exc).__name__).split())[:160]
            return int(
                initializer.emit_initializer_error(
                    f"Upgrade filesystem operation failed: {reason}.",
                    operation="upgrade",
                    error_code="UPGRADE_WRITE_FAILED",
                    remediation=initializer.UpgradeWriteError.remediation,
                    details={
                        "reason": reason,
                        "preserved": (
                            "The upgrade did not report success; inspect the target before retrying."
                        ),
                    },
                )
            )
        except SystemExit as exc:
            if not isinstance(exc.code, str):
                raise
            return int(initializer.emit_initializer_error(exc.code, operation="upgrade"))


def _contract_payload() -> dict:
    import yaml

    from . import orchestration

    with assets_root() as root:
        starter_root = root / STARTER_DIR
        metadata = yaml.safe_load((starter_root / "workspace-system.yml").read_text()) or {}
        workspace_system = metadata.get("workspace_system") if isinstance(metadata, dict) else {}
        if not isinstance(workspace_system, dict):
            workspace_system = {}
        initializer = _load_initializer(starter_root)
        status_module = _load_script(
            starter_root / "scripts" / "workspace_status.py",
            "evidence_wiki_workspace_status",
        )
        intake_module = _load_script(
            starter_root / "scripts" / "intake_questions.py",
            "evidence_wiki_intake_questions",
        )
        export_module = _load_script(
            starter_root / "scripts" / "export_answers.py",
            "evidence_wiki_export_answers",
        )
        source_requests_module = _load_script(
            starter_root / "scripts" / "source_requests.py",
            "evidence_wiki_source_requests",
        )
        fetch_sources_module = _load_script(
            starter_root / "scripts" / "fetch_sources.py",
            "evidence_wiki_fetch_sources",
        )
        verify_citations_module = _load_script(
            starter_root / "scripts" / "verify_citations.py",
            "evidence_wiki_verify_citations",
        )
        verify_quotes_module = _load_script(
            starter_root / "scripts" / "verify_quotes.py",
            "evidence_wiki_verify_quotes",
        )
        discover_sources_module = _load_script(
            starter_root / "scripts" / "discover_sources.py",
            "evidence_wiki_discover_sources",
        )
        mcp_module = _load_script(
            starter_root / "scripts" / "serve_mcp.py",
            "evidence_wiki_serve_mcp",
        )
        script_errors_module = _load_script(
            starter_root / "scripts" / "_script_errors.py",
            "evidence_wiki_script_errors",
        )
        provider_registry_module = _load_script(
            starter_root / "scripts" / "_provider_registry.py",
            "evidence_wiki_provider_registry",
        )
        question_claim_module = _load_script(
            starter_root / "scripts" / "question_claim.py",
            "evidence_wiki_question_claim",
        )
        question_resolve_module = _load_script(
            starter_root / "scripts" / "question_resolve.py",
            "evidence_wiki_question_resolve",
        )
        run_report_module = _load_script(
            starter_root / "scripts" / "run_report.py",
            "evidence_wiki_run_report",
        )
        coverage_manifest_module = _load_script(
            starter_root / "scripts" / "coverage_manifest.py",
            "evidence_wiki_coverage_manifest",
        )
        publication_readiness_module = _load_script(
            starter_root / "scripts" / "publication_readiness.py",
            "evidence_wiki_publication_readiness",
        )
        fleet_status_module = _load_script(
            starter_root / "scripts" / "fleet_status.py",
            "evidence_wiki_fleet_status",
        )
        base_policy_definitions = coverage_manifest_module.base_policy_vocabularies()
        installed_pack_policy_definitions = _pack_policy_vocabularies(root, coverage_manifest_module, yaml)
        merged_policy_definitions = coverage_manifest_module.base_policy_vocabularies()
        for pack_vocabularies in installed_pack_policy_definitions.values():
            for field, definitions in pack_vocabularies.items():
                merged_policy_definitions.setdefault(field, {}).update(definitions)
        policy_vocabularies = {
            field: sorted(definitions)
            for field, definitions in merged_policy_definitions.items()
        }
        policy_vocabularies["artifact_kinds"] = sorted(coverage_manifest_module.ALLOWED_ARTIFACT_KINDS)
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "package": "evidence-wiki",
            "package_version": __version__,
            "starter_version": workspace_system.get("starter_version"),
            "starter_schema_version": workspace_system.get("schema_version"),
            "compatible_research_yml_contract": workspace_system.get("compatible_research_yml_contract"),
            "profile_schema_versions": [initializer.PROFILE_SCHEMA_VERSION],
            "upgrade_compatibility": {
                "workspace_schema_versions": list(initializer.SUPPORTED_WORKSPACE_SCHEMA_VERSIONS),
                "research_yml_contract_versions": list(initializer.SUPPORTED_RESEARCH_YML_CONTRACTS),
            },
            "required_asset_manifest": required_asset_manifest(),
            "source_providers": {
                "discovery": list(provider_registry_module.DISCOVERY_PROVIDER_IDS),
                "acquisition": list(provider_registry_module.ACQUISITION_PROVIDER_IDS),
                "legacy_discovery_strategy_aliases": list(
                    provider_registry_module.LEGACY_DISCOVERY_STRATEGY_IDS
                ),
            },
            "artifact_schemas": {
                "workspace_status": status_module.SCHEMA_VERSION,
                "question_intake": intake_module.SCHEMA_VERSION,
                "answer_export": export_module.SCHEMA_VERSION,
                "source_requests": source_requests_module.SCHEMA_VERSION,
                "fetch_sources": fetch_sources_module.SCHEMA_VERSION,
                "citation_verification": verify_citations_module.SCHEMA_VERSION,
                "quote_verification": verify_quotes_module.SCHEMA_VERSION,
                "discover_sources": discover_sources_module.SCHEMA_VERSION,
                "mcp_server": mcp_module.SCHEMA_VERSION,
                "question_claim": question_claim_module.SCHEMA_VERSION,
                "question_resolve": question_resolve_module.SCHEMA_VERSION,
                "run_state": "1.0",
                "orchestration_session": orchestration.ORCHESTRATION_SESSION_SCHEMA_VERSION,
                "orchestration_work_order": orchestration.ORCHESTRATION_WORK_ORDER_SCHEMA_VERSION,
                "orchestration_result": orchestration.ORCHESTRATION_RESULT_SCHEMA_VERSION,
                "run_report": run_report_module.SCHEMA_VERSION,
                "coverage_manifest": coverage_manifest_module.SCHEMA_VERSION,
                "publication_readiness": publication_readiness_module.SCHEMA_VERSION,
                "fleet_status": fleet_status_module.SCHEMA_VERSION,
                "error_envelope": script_errors_module.SCHEMA_VERSION,
            },
            "policy_vocabularies": policy_vocabularies,
            "policy_vocabulary_definitions": {
                "base": base_policy_definitions,
                "installed_domain_packs": installed_pack_policy_definitions,
                "merged": merged_policy_definitions,
            },
        }


def _run_contract() -> int:
    print(json.dumps(_contract_payload(), indent=2, sort_keys=False))
    return 0


def _run_orchestrate(args: list[str]) -> int:
    from . import orchestration

    return int(orchestration.main(args) or 0)


def _print_orchestrator_guide_help() -> None:
    print(
        "evidence-wiki orchestrator-guide: locate the PM/orchestrator playbook\n\n"
        "Usage:\n"
        "  evidence-wiki orchestrator-guide              print the resolved skill path\n"
        "  evidence-wiki orchestrator-guide --print      print the skill content\n"
        "  evidence-wiki orchestrator-guide --format json\n\n"
        "The orchestrator skill is the executable companion to the machine\n"
        "contract in the workspace's docs/orchestrator-handoff.md. It targets the\n"
        "external PM/parent agent that creates and manages research workspaces; it\n"
        "is never copied into a created workspace."
    )


def _run_orchestrator_guide(args: list[str]) -> int:
    print_content = False
    as_json = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-h", "--help"}:
            _print_orchestrator_guide_help()
            return 0
        if arg in {"--print", "--content"}:
            print_content = True
        elif arg == "--format":
            index += 1
            if index >= len(args) or args[index] != "json":
                parser = argparse.ArgumentParser(prog="evidence-wiki orchestrator-guide")
                parser.error("--format only supports json")
                return 2
            as_json = True
        elif arg == "--format=json":
            as_json = True
        else:
            parser = argparse.ArgumentParser(prog="evidence-wiki orchestrator-guide")
            parser.error(f"unrecognized argument: {arg}")
            return 2
        index += 1

    with assets_root() as root:
        skill_path = orchestrator_skill_path(root)
        if not skill_path.is_file():
            raise SystemExit(f"Missing packaged orchestrator skill: {skill_path}")
        content = skill_path.read_text(encoding="utf-8")
        resolved = str(skill_path.resolve())

    if as_json:
        print(
            json.dumps(
                {
                    "skill": ORCHESTRATOR_SKILL,
                    "path": resolved,
                    "package_version": __version__,
                },
                indent=2,
                sort_keys=False,
            )
        )
        return 0
    if print_content:
        print(content)
        return 0
    print(resolved)
    return 0


def _run_doctor(args: list[str]) -> int:
    forwarded: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--target":
            index += 1
            if index >= len(args):
                parser = argparse.ArgumentParser(prog="evidence-wiki doctor")
                parser.error("--target requires a path value")
                return 2
            forwarded.extend(["--project-root", args[index]])
        elif arg.startswith("--target="):
            forwarded.extend(["--project-root", arg.split("=", 1)[1]])
        else:
            forwarded.append(arg)
        index += 1
    with assets_root() as root:
        if not any(arg == "--project-root" or arg.startswith("--project-root=") for arg in forwarded):
            forwarded = ["--project-root", str(root / STARTER_DIR), *forwarded]
        module = _load_script(root / STARTER_DIR / "scripts" / "doctor.py", "evidence_wiki_doctor")
        return int(module.main(forwarded) or 0)


def _run_serve_mcp(args: list[str]) -> int:
    with assets_root() as root:
        module = _load_script(root / STARTER_DIR / "scripts" / "serve_mcp.py", "evidence_wiki_serve_mcp")
        return int(module.main(args) or 0)


def _run_fleet_status(args: list[str]) -> int:
    with assets_root() as root:
        module = _load_script(root / STARTER_DIR / "scripts" / "fleet_status.py", "evidence_wiki_fleet_status")
        return int(module.main(args) or 0)


def _forward_target(args: list[str], *, prog: str) -> list[str]:
    forwarded: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--target":
            index += 1
            if index >= len(args):
                parser = argparse.ArgumentParser(prog=prog)
                parser.error("--target requires a path value")
                return []
            forwarded.extend(["--project-root", args[index]])
        elif arg.startswith("--target="):
            forwarded.extend(["--project-root", arg.split("=", 1)[1]])
        else:
            forwarded.append(arg)
        index += 1
    return forwarded


def _run_status(args: list[str]) -> int:
    forwarded = _forward_target(args, prog="evidence-wiki status")
    with assets_root() as root:
        module = _load_script(
            root / STARTER_DIR / "scripts" / "workspace_status.py",
            "evidence_wiki_workspace_status",
        )
        return int(module.main(forwarded) or 0)


def _run_export(args: list[str]) -> int:
    return _run_questions(["export", *args])


_QUESTIONS_SCRIPTS = {
    "add": ("intake_questions.py", "evidence_wiki_intake_questions"),
    "export": ("export_answers.py", "evidence_wiki_export_answers"),
}


def _print_questions_help() -> None:
    print(
        "evidence-wiki questions: machine question intake and answer export\n\n"
        "Usage:\n"
        "  evidence-wiki questions add --target PATH --from-file batch.yaml [options]\n"
        "  evidence-wiki questions export --target PATH [options]\n\n"
        "--target points at the workspace root (forwarded as --project-root;\n"
        "defaults to the current directory). Remaining options are forwarded to\n"
        "scripts/intake_questions.py or scripts/export_answers.py. Run with\n"
        "--help after the subcommand for the full option list."
    )


def _run_questions(args: list[str]) -> int:
    if not args or args[0] in {"-h", "--help"}:
        _print_questions_help()
        return 0
    subcommand = args.pop(0)
    if subcommand not in _QUESTIONS_SCRIPTS:
        parser = argparse.ArgumentParser(prog="evidence-wiki questions")
        parser.error(f"unknown questions subcommand: {subcommand}")
        return 2
    forwarded: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--target":
            index += 1
            if index >= len(args):
                parser = argparse.ArgumentParser(prog=f"evidence-wiki questions {subcommand}")
                parser.error("--target requires a path value")
                return 2
            forwarded.extend(["--project-root", args[index]])
        elif arg.startswith("--target="):
            forwarded.extend(["--project-root", arg.split("=", 1)[1]])
        else:
            forwarded.append(arg)
        index += 1
    script_name, module_name = _QUESTIONS_SCRIPTS[subcommand]
    with assets_root() as root:
        module = _load_script(root / STARTER_DIR / "scripts" / script_name, module_name)
        return int(module.main(forwarded) or 0)


def _print_pack_help() -> None:
    print(
        "evidence-wiki pack: domain pack utilities\n\n"
        "Usage:\n"
        "  evidence-wiki pack validate --path NAME_OR_PATH [--format json]\n\n"
        "`validate` checks a reusable domain pack overlay, declared pack files,\n"
        "initializer compatibility, and smoke validation in a temporary workspace."
    )


def _run_pack(args: list[str]) -> int:
    if not args or args[0] in {"-h", "--help"}:
        _print_pack_help()
        return 0
    subcommand = args.pop(0)
    if subcommand != "validate":
        parser = argparse.ArgumentParser(prog="evidence-wiki pack")
        parser.error(f"unknown pack subcommand: {subcommand}")
        return 2
    from . import domain_pack_validator

    return int(domain_pack_validator.main(args) or 0)


def _print_help() -> None:
    print(
        "evidence-wiki: deploy source-grounded research workspaces\n\n"
        "Usage:\n"
        "  evidence-wiki init [initializer options]\n"
        "  evidence-wiki deploy [initializer options]\n"
        "  evidence-wiki upgrade [upgrade options]\n"
        "  evidence-wiki questions add|export [--target PATH] [options]\n"
        "  evidence-wiki status [--target PATH] [--format text|json]\n"
        "  evidence-wiki export [--target PATH] [--format json]\n"
        "  evidence-wiki pack validate --path NAME_OR_PATH [--format json]\n"
        "  evidence-wiki doctor [--target PATH] [--format text|json]\n"
        "  evidence-wiki fleet-status --target PATH [--target PATH ...] [--format text|json]\n"
        "  evidence-wiki serve-mcp --target PATH\n"
        "  evidence-wiki orchestrate start|next|submit|status [options]\n"
        "  evidence-wiki orchestrate run|resume --runner codex|claude [options]\n"
        "  evidence-wiki contract\n"
        "  evidence-wiki orchestrator-guide [--print] [--format json]\n\n"
        "Common initializer options:\n"
        "  --target PATH\n"
        "  --project-name NAME\n"
        "  --project-description TEXT\n"
        "  --owner-goal TEXT\n"
        "  --profile PATH\n"
        "  --scope-root PATH\n"
        "  --domain-pack NAME_OR_PATH\n"
        "  --discovery-provider ID (repeatable)\n"
        "  --acquisition-provider ID (repeatable)\n"
        "  --dry-run\n\n"
        "Upgrade refreshes starter-managed tooling (scripts/) in an existing\n"
        "workspace from the installed package. It never touches research.yml,\n"
        "raw/, sources/, wiki/, index.md, or log.md.\n\n"
        "Common upgrade options:\n"
        "  --target PATH\n"
        "  --include skills|docs\n"
        "  --force-optional\n"
        "  --dry-run\n"
        "Optional skills/docs refreshes refuse local edits unless --force-optional\n"
        "is set; forced replacements are preserved under .replaced/<path>.\n\n"
        "Contract prints the supported contract and schema versions as JSON so\n"
        "orchestrators can negotiate compatibility before deploy or upgrade.\n\n"
        "Doctor checks local runtime dependencies, optional tools, workspace\n"
        "write permissions, and contract metadata before an unattended run.\n\n"
        "Fleet-status aggregates workspace status for multiple local targets and\n"
        "continues reporting when one target is unreadable.\n\n"
        "Questions forwards to the packaged question lifecycle scripts: `add`\n"
        "injects a validated question batch into a workspace, `export` emits\n"
        "structured answers with citations for downstream agents.\n\n"
        "Pack validation checks reusable domain packs before they are shipped or\n"
        "used during deployment.\n\n"
        "Serve-mcp starts an optional stdio MCP server exposing read/append-only\n"
        "workspace tools while preserving the CLI scripts as the canonical contract.\n\n"
        "Orchestrate creates a durable parent session. Protocol subcommands let\n"
        "any external agent obtain and submit bounded work orders; run and resume\n"
        "can launch a fresh Codex or Claude process for each action.\n\n"
        "Orchestrator-guide locates the PM/orchestrator playbook skill that drives\n"
        "deploy, question intake, the run loop, blocked-source routing, and result\n"
        "collection for a parent agent managing workspaces.\n\n"
        "Run `evidence-wiki init --help` or `evidence-wiki upgrade --help` for full help."
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        _print_help()
        return 0
    if args[0] == "--version":
        print(f"evidence-wiki {__version__}")
        return 0

    command = args.pop(0)
    if command in {"init", "deploy"}:
        return _run_initializer(args)
    if command == "upgrade":
        return _run_upgrader(args)
    if command == "questions":
        return _run_questions(args)
    if command == "pack":
        return _run_pack(args)
    if command == "doctor":
        return _run_doctor(args)
    if command == "fleet-status":
        return _run_fleet_status(args)
    if command == "status":
        return _run_status(args)
    if command == "export":
        return _run_export(args)
    if command == "serve-mcp":
        return _run_serve_mcp(args)
    if command == "orchestrate":
        return _run_orchestrate(args)
    if command == "contract":
        return _run_contract()
    if command == "orchestrator-guide":
        return _run_orchestrator_guide(args)

    parser = argparse.ArgumentParser(prog="evidence-wiki")
    parser.error(f"unknown command: {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
