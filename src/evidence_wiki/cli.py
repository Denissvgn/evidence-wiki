"""Command-line entry points for deploying research workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType

from . import __version__

# The capability payload now lives in ``_contract`` so the library API can return
# it without spawning this CLI. The names it used to own stay reachable here:
# in-tree callers and the contract test suite reach for ``cli._contract_payload``
# and ``cli.CONTRACT_SCHEMA_VERSION``.
from ._contract import CONTRACT_SCHEMA_VERSION  # noqa: F401
from ._contract import contract as _contract_payload

# The script loader and its caches now live in ``_script_host`` so the library
# API can share them without importing this argparse-shaped module. They stay
# reachable under their original ``cli`` names: in-tree callers and the CLI test
# suite reach for ``cli._load_script`` and clear the caches through ``cli``.
from ._script_host import (
    _LOADER_MODULE_CACHE,  # noqa: F401
    _SCRIPT_MODULE_CACHE,  # noqa: F401
    _load_script,
    _load_workspace_loader,  # noqa: F401
)
from .resources import ORCHESTRATOR_SKILL, STARTER_DIR, assets_root, orchestrator_skill_path


def _load_initializer(starter_root: Path) -> ModuleType:
    return _load_script(
        starter_root / "scripts" / "init_research_workspace.py",
        "evidence_wiki_initializer",
    )


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


_NORMALIZE_SCRIPTS = {
    "verify": ("normalize_verify.py", "evidence_wiki_normalize_verify"),
}


def _print_normalize_help() -> None:
    print(
        "evidence-wiki normalize: normalized-record contract utilities\n\n"
        "Usage:\n"
        "  evidence-wiki normalize verify [--target PATH] [--source-id ID ...] [--format json|text]\n\n"
        "`verify` checks normalized records against the published record contract\n"
        "(docs/normalized-source-format.md) and reports each breach with a stable\n"
        "code. Records written by an external normalizer are checked exactly as\n"
        "records this package wrote. Exits 1 when any record fails.\n\n"
        "--target points at the workspace root (forwarded as --project-root;\n"
        "defaults to the current directory)."
    )


def _run_normalize(args: list[str]) -> int:
    if not args or args[0] in {"-h", "--help"}:
        _print_normalize_help()
        return 0
    subcommand = args.pop(0)
    if subcommand not in _NORMALIZE_SCRIPTS:
        parser = argparse.ArgumentParser(prog="evidence-wiki normalize")
        parser.error(f"unknown normalize subcommand: {subcommand}")
        return 2
    forwarded = _forward_target(args, prog=f"evidence-wiki normalize {subcommand}")
    script_name, module_name = _NORMALIZE_SCRIPTS[subcommand]
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
    from . import orchestration

    managed_runner_ids = orchestration.managed_runner_names()
    managed_runners = "|".join(managed_runner_ids)
    managed_runner_prose = ", ".join(managed_runner_ids)
    print(
        "evidence-wiki: deploy source-grounded research workspaces\n\n"
        "Usage:\n"
        "  evidence-wiki init [initializer options]\n"
        "  evidence-wiki deploy [initializer options]\n"
        "  evidence-wiki upgrade [upgrade options]\n"
        "  evidence-wiki questions add|export [--target PATH] [options]\n"
        "  evidence-wiki status [--target PATH] [--format text|json]\n"
        "  evidence-wiki export [--target PATH] [--format json]\n"
        "  evidence-wiki normalize verify [--target PATH] [--source-id ID] [--format json|text]\n"
        "  evidence-wiki pack validate --path NAME_OR_PATH [--format json]\n"
        "  evidence-wiki doctor [--target PATH] [--format text|json]\n"
        "  evidence-wiki fleet-status --target PATH [--target PATH ...] [--format text|json]\n"
        "  evidence-wiki serve-mcp --target PATH\n"
        "  evidence-wiki orchestrate start|next|submit|status [options]\n"
        f"  evidence-wiki orchestrate run|resume --runner {managed_runners} [options]\n"
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
        "write permissions, contract metadata, and which external normalizer\n"
        "adapters a workspace is authorized to execute, before an unattended run.\n\n"
        "Fleet-status aggregates workspace status for multiple local targets and\n"
        "continues reporting when one target is unreadable.\n\n"
        "Questions forwards to the packaged question lifecycle scripts: `add`\n"
        "injects a validated question batch into a workspace, `export` emits\n"
        "structured answers with citations for downstream agents.\n\n"
        "Normalize verify checks normalized records against the published record\n"
        "contract, so a host writing records with its own normalizer can prove they\n"
        "conform instead of matching an internal format by inspection.\n\n"
        "Pack validation checks reusable domain packs before they are shipped or\n"
        "used during deployment.\n\n"
        "Serve-mcp starts an optional stdio MCP server exposing read/append-only\n"
        "workspace tools while preserving the CLI scripts as the canonical contract.\n\n"
        "Orchestrate creates a durable parent session. Protocol subcommands let\n"
        "any external agent obtain and submit bounded work orders; run and resume\n"
        f"launch only registered package-managed adapters ({managed_runner_prose}). OpenCode, Pi, and\n"
        "other harnesses drive start/next/submit/status from an external host.\n\n"
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
    if command == "normalize":
        return _run_normalize(args)
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
