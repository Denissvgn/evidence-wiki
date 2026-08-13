"""Package-managed CLI shell for domain-pack adoption and refresh.

Mutation deliberately stays out of the embeddable library API.  The canonical
planner lives with the packaged workspace scripts so init, status, doctor, and
the CLI all interpret the same state document.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from . import domain_pack_validator
from ._script_host import _load_script
from .resources import STARTER_DIR, assets_root


def _parser(operation: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"evidence-wiki pack {operation}")
    parser.add_argument("--target", required=True, help="Existing research workspace path.")
    if operation == "refresh":
        parser.add_argument("--path", required=True, help="Candidate pack name or filesystem path.")
        parser.add_argument(
            "--keep-local",
            action="append",
            default=[],
            metavar="TARGET",
            help="Preserve and release one conflicting config:/ or file: target. Repeatable.",
        )
        parser.add_argument(
            "--accept-pack",
            action="append",
            default=[],
            metavar="TARGET",
            help="Apply the pack at one conflicting config:/ or file: target. Repeatable.",
        )
    else:
        parser.add_argument(
            "--accept-local-overrides",
            action="store_true",
            help="Adopt exact pack values and record differing workspace values as unowned.",
        )
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan without writing anything.")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Report format. Defaults to text.",
    )
    return parser


def _render_text(report: dict[str, Any]) -> str:
    pack = report.get("pack") if isinstance(report.get("pack"), dict) else {}
    lines = [
        f"Domain pack {report.get('operation')} report",
        "=" * 31,
        f"Target: {report.get('target')}",
        f"Mode: {report.get('mode')}",
        f"Status: {report.get('status')}",
        f"Pack: {pack.get('name') or 'unknown'}",
    ]
    if pack.get("installed_version"):
        version = str(pack["installed_version"])
        if pack.get("candidate_version") is not None:
            version += f" -> {pack['candidate_version']}"
        lines.append(f"Version: {version}")
    changes = report.get("changes") if isinstance(report.get("changes"), list) else []
    lines.append(f"Changes: {len(changes)}")
    for item in changes:
        lines.append(f"- {item.get('action')}: {item.get('path')}")
    conflicts = report.get("conflicts") if isinstance(report.get("conflicts"), list) else []
    lines.append(f"Conflicts: {len(conflicts)}")
    for item in conflicts:
        lines.append(f"- {item.get('target')}: {item.get('reason')}")
    for warning in report.get("warnings", []):
        lines.append(f"Warning: {warning}")
    lines.append(f"Log appended: {'yes' if report.get('log_appended') else 'no'}")
    return "\n".join(lines) + "\n"


def _print_report(report: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(_render_text(report), end="")


def _lifecycle_module(starter_root: Path) -> ModuleType:
    return _load_script(
        starter_root / "scripts" / "_domain_pack_lifecycle.py",
        "evidence_wiki_domain_pack_lifecycle",
    )


def _errors_module(starter_root: Path) -> ModuleType:
    return _load_script(starter_root / "scripts" / "_script_errors.py", "evidence_wiki_pack_errors")


def _invalid(lifecycle: ModuleType, message: str, *, details: dict[str, Any] | None = None) -> Exception:
    return lifecycle.LifecycleFailure("DOMAIN_PACK_INVALID", message, details=details)


def _candidate_location(selection: str, *, assets: Path) -> tuple[Path, str]:
    starter_root = assets / STARTER_DIR
    requested = Path(selection).expanduser()
    candidate = requested if requested.exists() else starter_root.parent / "domain-packs" / selection
    candidate = candidate.absolute()
    bundled_root = (assets / "domain-packs").absolute()
    source_kind = "bundled" if candidate.parent == bundled_root else "path"
    return candidate, source_kind


def _validate_candidate(
    candidate: Path,
    *,
    assets: Path,
    lifecycle: ModuleType,
) -> str:
    try:
        before = lifecycle.tree_sha256(candidate)
        payload = domain_pack_validator.validate_domain_pack(str(candidate), root=assets)
        after = lifecycle.tree_sha256(candidate)
    except SystemExit as exc:
        raise _invalid(lifecycle, str(exc)) from exc
    if before != after:
        raise _invalid(lifecycle, "Candidate domain pack changed during canonical validation")
    if not payload.get("ok"):
        failed = [
            item.get("id")
            for item in payload.get("checks", [])
            if isinstance(item, dict) and item.get("status") != "pass"
        ]
        raise _invalid(
            lifecycle,
            "Candidate domain pack did not pass canonical validation",
            details={"failed_checks": [value for value in failed if isinstance(value, str)]},
        )
    return after


def _installed_pack_path(target: Path, lifecycle: ModuleType) -> Path:
    try:
        document = yaml.safe_load((target / "research.yml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise lifecycle.LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED", f"Could not read the legacy workspace pack identity: {exc}"
        ) from exc
    pack = document.get("domain_pack") if isinstance(document, dict) else None
    name = pack.get("name") if isinstance(pack, dict) else None
    if (
        not isinstance(name, str)
        or not name.strip()
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
    ):
        raise lifecycle.LifecycleFailure(
            "DOMAIN_PACK_UNTRACKED", "research.yml does not declare a safe installed domain-pack name"
        )
    return target / "domain-packs" / name


def _emit_failure(
    failure: object,
    *,
    errors: ModuleType,
    output_format: str,
) -> int:
    details = getattr(failure, "details", None)
    report = details.get("report") if isinstance(details, dict) else None
    if isinstance(report, dict):
        _print_report(report, output_format)
    code = str(getattr(failure, "error_code", "DOMAIN_PACK_WRITE_FAILED"))
    refusal = errors.ScriptRefusal(
        code,
        str(getattr(failure, "message", str(failure))),
        exit_code=int(getattr(failure, "exit_code", 2)),
        remediation=getattr(failure, "remediation", None) or errors.remediation_for(code),
        details=details,
    )
    return int(errors.emit_refusal(refusal, json_mode=output_format == "json"))


def main(operation: str, argv: list[str] | None = None) -> int:
    args = _parser(operation).parse_args(argv)
    with assets_root() as assets:
        starter_root = assets / STARTER_DIR
        errors = _errors_module(starter_root)
        try:
            lifecycle = _lifecycle_module(starter_root)
        except (Exception, SystemExit) as exc:
            message = str(exc.code if isinstance(exc, SystemExit) else exc)
            refusal = errors.ScriptRefusal(
                "DOMAIN_PACK_WRITE_FAILED",
                f"Could not load domain-pack lifecycle support: {message}",
                remediation=errors.remediation_for("DOMAIN_PACK_WRITE_FAILED"),
            )
            return int(errors.emit_refusal(refusal, json_mode=args.format == "json"))
        try:
            try:
                target = Path(args.target).expanduser()
                # Preserve the unresolved path for the lifecycle's symlink
                # boundary check, but prove here that it has a stable resolved
                # spelling so resolution failures retain operation-specific
                # public error codes.
                target.resolve()
            except (OSError, RuntimeError) as exc:
                code = (
                    "DOMAIN_PACK_STATE_INVALID"
                    if operation == "refresh"
                    else "DOMAIN_PACK_UNTRACKED"
                )
                raise lifecycle.LifecycleFailure(
                    code,
                    f"Could not resolve workspace target safely: {exc}",
                    details={"target": args.target},
                ) from exc
            if operation == "refresh":
                try:
                    candidate, source_kind = _candidate_location(args.path, assets=assets)
                except (OSError, RuntimeError) as exc:
                    raise _invalid(
                        lifecycle,
                        f"Could not resolve candidate domain pack safely: {exc}",
                    ) from exc

                def validate_candidate(candidate_root: Path) -> str:
                    return _validate_candidate(
                        candidate_root,
                        assets=assets,
                        lifecycle=lifecycle,
                    )

                report = lifecycle.run_refresh(
                    target,
                    candidate,
                    keep_local=args.keep_local,
                    accept_pack=args.accept_pack,
                    dry_run=args.dry_run,
                    source_kind=source_kind,
                    candidate_validator=validate_candidate,
                )
            else:
                def validate_installed(root: Path) -> str:
                    installed = _installed_pack_path(root, lifecycle)
                    try:
                        before = lifecycle.tree_sha256(installed)
                        payload = domain_pack_validator.validate_domain_pack(str(installed), root=assets)
                        after = lifecycle.tree_sha256(installed)
                    except SystemExit as exc:
                        raise lifecycle.LifecycleFailure("DOMAIN_PACK_UNTRACKED", str(exc)) from exc
                    if before != after:
                        raise lifecycle.LifecycleFailure(
                            "DOMAIN_PACK_UNTRACKED",
                            "Installed domain pack changed during canonical validation",
                        )
                    if not payload.get("ok"):
                        failed = [
                            item.get("id")
                            for item in payload.get("checks", [])
                            if isinstance(item, dict) and item.get("status") != "pass"
                        ]
                        raise lifecycle.LifecycleFailure(
                            "DOMAIN_PACK_UNTRACKED",
                            "Installed domain pack did not pass canonical validation",
                            details={"failed_checks": failed},
                        )
                    return after

                report = lifecycle.run_adopt(
                    target,
                    accept_local_overrides=args.accept_local_overrides,
                    dry_run=args.dry_run,
                    validator=validate_installed,
                )
        except (Exception, SystemExit) as exc:
            if getattr(exc, "error_code", None) is None:
                if isinstance(exc, SystemExit) and not isinstance(exc.code, str):
                    raise
                exc = lifecycle.LifecycleFailure(
                    "DOMAIN_PACK_WRITE_FAILED",
                    str(exc.code if isinstance(exc, SystemExit) else exc),
                )
            return _emit_failure(exc, errors=errors, output_format=args.format)
    _print_report(report, args.format)
    return 0
