"""Command-line entry points for deploying research workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

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
    load_packaged_script,
    shared_assets_root,
)

# The base class of every failure the library API reports, and therefore the one
# thing the API-backed subcommands below catch. ``errors`` is a dependency-free
# leaf module, so importing it eagerly costs CLI startup nothing; the facade tree
# is what must stay lazy, and it is reached through function-local imports.
from .errors import EvidenceWikiError
from .resources import ORCHESTRATOR_SKILL, STARTER_DIR, assets_root, orchestrator_skill_path

if TYPE_CHECKING:  # pragma: no cover - for type checkers only; never executed
    from .workspace import Workspace


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
    # Already one code path with the library API: ``_contract.contract`` is the
    # exact callable ``evidence_wiki.contract`` re-exports, so there is nothing
    # here to route -- only a payload to render.
    print(json.dumps(_contract_payload(), indent=2, sort_keys=False))
    return 0


# ---------------------------------------------------------------------------
# The API-backed shell
#
# Every subcommand that operates on an *existing* workspace is built from the
# three helpers below and follows the same three steps:
#
#   1. parse with the packaged script's own ``parse_args``,
#   2. obtain the document from the library API,
#   3. render it with the packaged script's own renderer.
#
# No document is computed in this module and no rendering is reimplemented in
# it, so the bytes the CLI prints and the dict a host receives come from one
# ``run_<op>`` seam and cannot drift apart. What stays here is what the API
# deliberately omits because it concerns delivery rather than the document:
# ``--format``, ``--output``, ``--append-log``, the ``--target`` spelling, and
# the exit code each command derives from what it just printed.
#
# Subcommands that *create* a workspace (``init``/``deploy``), rewrite starter
# tooling in place (``upgrade``), validate a domain pack (``pack validate``),
# serve a long-running process (``serve-mcp``), or read a packaged asset
# (``orchestrator-guide``) are not operations on an open handle and keep their
# own paths. ``orchestrate start|next|submit|status`` is already shared through
# ``orchestration.protocol_*``; its passthrough branch streams the controller's
# raw bytes, and those bytes are the contract.
# ---------------------------------------------------------------------------


def _packaged_script(stem: str) -> ModuleType:
    """Load one packaged workspace script through the root the library API uses.

    Deliberately :func:`~evidence_wiki._script_host.shared_assets_root` rather
    than this module's per-command ``assets_root()``: the API loads from the
    process-wide root, and going through the same one is what makes the module
    the shell renders with the very module whose seam produced the document.
    """
    return load_packaged_script(shared_assets_root(), stem)


def _packaged_starter() -> Path:
    """Return the packaged starter workspace, which ``doctor`` diagnoses by default."""
    return shared_assets_root() / STARTER_DIR


def _handle(project_root: str | Path) -> Workspace:
    """Return a workspace handle on ``project_root`` without opening a gate on it.

    Deliberately not :meth:`Workspace.open`. ``open`` applies its own structural
    check with its own wording, and for the CLI an unusable workspace is
    described by the packaged script that refused -- that message and its exit
    code are this program's published contract. Skipping the gate changes
    nothing about where the document comes from; it only keeps the refusal the
    one the CLI has always printed.
    """
    from .workspace import Workspace

    return Workspace(Path(project_root))


def _emit_refusal(refusal: object, *, json_mode: bool) -> int:
    """Render one script refusal exactly as a packaged ``main`` renders it.

    ``_script_errors.emit_refusal`` reads a refusal *by shape* rather than by
    class, which is what lets a refusal raised inside one loaded script be
    rendered by another copy of the error module. That is also why the
    ``text_line`` distinction survives the trip: the bare-message form and the
    ``refused (CODE): message`` form are both carried on the object itself.
    """
    return int(_packaged_script("_script_errors").emit_refusal(refusal, json_mode=json_mode))


def _refuse(exc: EvidenceWikiError, *, json_mode: bool) -> int:
    """Turn a typed API error back into the refusal bytes the CLI has always printed.

    ``call_seam`` raises ``translated from exc``, so the original refusal object
    is still attached as ``__cause__`` -- carrying the ``exit_code`` and the
    ``text_line`` that the error envelope alone does not describe. It is
    recognized structurally, through the package's one recognizer, because the
    module loader gives every loaded script its own ``ScriptRefusal`` class and
    an ``isinstance`` check across that boundary matches nothing.

    A typed error with no refusal behind it came from a ``SystemExit(str)`` the
    API typed on the caller's behalf -- in practice a packaged script that could
    not be loaded at all. The CLI has always let that leave as ``SystemExit``,
    printing the bare message and exiting 1, so it is restored here rather than
    escaping as an exception the CLI never raised.
    """
    from ._facades._base import refusal_envelope

    refusal = exc.__cause__
    if refusal_envelope(refusal) is None:
        raise SystemExit(exc.message)
    return _emit_refusal(refusal, json_mode=json_mode)


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
    forwarded = _forward_target(args, prog="evidence-wiki doctor")
    # Absent ``--target`` the doctor diagnoses the packaged starter, which is a
    # CLI convenience with no library counterpart: a handle is always on some
    # workspace, so the API has no notion of "no target".
    if not any(arg == "--project-root" or arg.startswith("--project-root=") for arg in forwarded):
        forwarded = ["--project-root", str(_packaged_starter()), *forwarded]
    script = _packaged_script("doctor")
    parsed = script.parse_args(forwarded)
    json_mode = script.json_mode_requested(forwarded, default_json=parsed.format == "json")
    try:
        report = _handle(parsed.project_root).doctor()
    except EvidenceWikiError as exc:
        return _refuse(exc, json_mode=json_mode)
    if parsed.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(script.render_text(report), end="")
    # A workspace the doctor cannot read is diagnosed rather than refused, so the
    # exit code is read off the report it just printed.
    return 1 if report["verdict"] == "missing" else 0


def _run_serve_mcp(args: list[str]) -> int:
    with assets_root() as root:
        module = _load_script(root / STARTER_DIR / "scripts" / "serve_mcp.py", "evidence_wiki_serve_mcp")
        return int(module.main(args) or 0)


def _run_fleet_status(args: list[str]) -> int:
    from ._facades.diagnostics import fleet_status

    script = _packaged_script("fleet_status")
    parsed = script.parse_args(args)
    try:
        # Repeated ``--target`` is this command's own spelling and is not
        # rewritten to ``--project-root``: a fleet read is not scoped to one root.
        report = fleet_status(parsed.target, no_cache=parsed.no_cache)
    except EvidenceWikiError as exc:
        # Unreachable for a fleet finding -- an unreadable target is an
        # ``ok: False`` entry, never a refusal -- so this only ever fires for an
        # installation that cannot load the packaged scripts at all.
        return _refuse(exc, json_mode=parsed.format == "json")
    if parsed.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(script.render_text(report), end="")
    return int(script.EXIT_OK)


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
    script = _packaged_script("workspace_status")
    parsed = script.parse_args(forwarded)
    json_mode = script.json_mode_requested(forwarded, default_json=parsed.format == "json")
    project_root = script.resolved_project_root(parsed.project_root)
    try:
        document = _handle(project_root).status(
            no_cache=parsed.no_cache or parsed.append_log,
            questions_processed_this_run=parsed.questions_processed_this_run,
            source_requests_opened_this_run=parsed.source_requests_opened_this_run,
            releases_this_run=parsed.releases_this_run,
            discovery_results_this_run=parsed.discovery_results_this_run,
            acquisition_downloads_this_run=parsed.acquisition_downloads_this_run,
            github_archive_bytes_this_run=parsed.github_archive_bytes_this_run,
            academic_provider_requests_this_run=parsed.academic_provider_requests_this_run,
            web_downloads_this_run=parsed.web_downloads_this_run,
            manual_url_deliveries_this_run=parsed.manual_url_deliveries_this_run,
            run_id=parsed.run_id,
        )
    except EvidenceWikiError as exc:
        return _refuse(exc, json_mode=json_mode)

    if parsed.format == "json":
        print(json.dumps(document, indent=2, sort_keys=False))
    else:
        sys.stdout.write(script.render_text(document))

    # A materially invalid workspace is reported in full and *then* exits
    # non-zero: the verdict is part of the document, not a refusal about it.
    workspace_health = document.get("workspace_health")
    if isinstance(workspace_health, dict) and not workspace_health.get("materially_valid", False):
        return int(script.EXIT_WORKSPACE_UNREADABLE)

    # ``--append-log`` has no library counterpart: appending to ``log.md``
    # happens after the document is printed and is no part of producing it, so
    # the shell owns it. Its lock refusal still renders through the one refusal
    # path, so this command keeps exactly one refusal shape.
    if parsed.append_log:
        try:
            script.append_log_entry(project_root / "log.md", script.render_log_entry(document))
        except script.LockUnavailableError as error:
            return _emit_refusal(
                script.ScriptRefusal(
                    error.error_code,
                    str(error),
                    exit_code=script.EXIT_WORKSPACE_UNREADABLE,
                    details=error.details,
                ),
                json_mode=json_mode,
            )

    if parsed.check_complete:
        return int(script.CHECK_COMPLETE_EXIT_CODES[document["readiness"]["verdict"]])
    return int(script.EXIT_REPORTED)


def _run_export(args: list[str]) -> int:
    return _run_questions(["export", *args])


def _run_questions_add(forwarded: list[str]) -> int:
    script = _packaged_script("intake_questions")
    parsed = script.parse_args(forwarded)
    json_mode = script.json_mode_requested(forwarded, default_json=parsed.dry_run or parsed.format == "json")
    try:
        # Intake is not read-only, and the writes belong to the operation: the
        # pages, the ``index.md`` update and the ``log.md`` entry all happen
        # inside the seam, so an in-process host leaves the same audit trail.
        report = _handle(parsed.project_root).questions.add_batch(
            from_file=parsed.from_file,
            dry_run=parsed.dry_run,
        )
    except EvidenceWikiError as exc:
        return _refuse(exc, json_mode=json_mode)
    if parsed.dry_run or parsed.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        sys.stdout.write(script.render_text_report(report))
    return int(script.EXIT_OK)


def _run_questions_export(forwarded: list[str]) -> int:
    script = _packaged_script("export_answers")
    parsed = script.parse_args(forwarded)
    json_mode = script.json_mode_requested(forwarded, default_json=True)
    try:
        document = _handle(parsed.project_root).export_answers(status=parsed.status)
    except EvidenceWikiError as exc:
        return _refuse(exc, json_mode=json_mode)
    # ``jsonl`` is the same document reshaped by the script's own renderer, and
    # ``--output`` only chooses where the rendered bytes go.
    rendered = script.render_output(document, parsed.format)
    if parsed.output:
        Path(parsed.output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return int(script.EXIT_OK)


_QUESTIONS_SHELLS = {
    "add": _run_questions_add,
    "export": _run_questions_export,
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
    if subcommand not in _QUESTIONS_SHELLS:
        parser = argparse.ArgumentParser(prog="evidence-wiki questions")
        parser.error(f"unknown questions subcommand: {subcommand}")
        return 2
    forwarded = _forward_target(args, prog=f"evidence-wiki questions {subcommand}")
    return _QUESTIONS_SHELLS[subcommand](forwarded)


def _run_normalize_verify(forwarded: list[str]) -> int:
    script = _packaged_script("normalize_verify")
    parsed = script.parse_args(forwarded)
    json_mode = script.json_mode_requested(forwarded, default_json=parsed.format == "json")
    project_root = Path(parsed.project_root).expanduser().resolve()
    try:
        report = _handle(project_root).normalize.verify(parsed.source_id)
    except EvidenceWikiError as exc:
        return _refuse(exc, json_mode=json_mode)
    rendered = script.render_report(report, parsed.format)
    if parsed.output:
        Path(parsed.output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    # A record that breaches the contract is a verdict, not a refusal: the full
    # report goes to stdout and only the exit code carries the failure.
    return int(script.EXIT_OK if report["overall_result"] == script.RESULT_VERIFIED else script.EXIT_NOT_VERIFIED)


_NORMALIZE_SHELLS = {
    "verify": _run_normalize_verify,
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
    if subcommand not in _NORMALIZE_SHELLS:
        parser = argparse.ArgumentParser(prog="evidence-wiki normalize")
        parser.error(f"unknown normalize subcommand: {subcommand}")
        return 2
    forwarded = _forward_target(args, prog=f"evidence-wiki normalize {subcommand}")
    return _NORMALIZE_SHELLS[subcommand](forwarded)


def _print_pack_help() -> None:
    print(
        "evidence-wiki pack: domain pack utilities\n\n"
        "Usage:\n"
        "  evidence-wiki pack validate --path NAME_OR_PATH [--format json]\n"
        "  evidence-wiki pack refresh --target PATH --path NAME_OR_PATH [--dry-run] [--format text|json]\n"
        "      [--keep-local TARGET]... [--accept-pack TARGET]...\n"
        "  evidence-wiki pack adopt --target PATH [--dry-run] [--accept-local-overrides]\n"
        "      [--format text|json]\n\n"
        "`validate` checks a reusable domain pack overlay, declared pack files,\n"
        "initializer compatibility, and smoke validation in a temporary workspace.\n"
        "`refresh` safely reconciles a tracked pack revision; `adopt` records\n"
        "provenance for a reviewed legacy workspace without changing its config or tree."
    )


def _run_pack(args: list[str]) -> int:
    if not args or args[0] in {"-h", "--help"}:
        _print_pack_help()
        return 0
    subcommand = args.pop(0)
    if subcommand not in {"validate", "refresh", "adopt"}:
        parser = argparse.ArgumentParser(prog="evidence-wiki pack")
        parser.error(f"unknown pack subcommand: {subcommand}")
        return 2
    if subcommand in {"refresh", "adopt"}:
        from . import domain_pack_lifecycle

        return int(domain_pack_lifecycle.main(subcommand, args) or 0)

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
        "  evidence-wiki pack refresh --target PATH --path NAME_OR_PATH [options]\n"
        "  evidence-wiki pack adopt --target PATH [options]\n"
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
        "Write-mode upgrade refreshes starter-managed tooling (scripts/) in an\n"
        "existing workspace, may update workspace-system.yml, uses .locks/, and\n"
        "conditionally appends log.md when managed content or the starter version\n"
        "changes. It preserves research.yml, raw/, sources/, wiki/, index.md,\n"
        "prior log history, and all user data; --dry-run writes nothing.\n\n"
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
        "used during deployment. Pack refresh/adopt provide the explicit,\n"
        "recoverable lifecycle for existing workspaces; ordinary upgrade never\n"
        "mutates domain-pack configuration or files.\n\n"
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
