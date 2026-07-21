#!/usr/bin/env python3
"""Diagnose whether a runner can operate a research wiki workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
PYTHON_MINIMUM = (3, 10)
WORKSPACE_DIRS = {
    "root": ".",
    "raw": "raw",
    "sources": "sources",
    "wiki": "wiki",
    "scripts": "scripts",
    "docs": "docs",
}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import handle_system_exit, json_mode_requested
from _workspace_health import evaluate_workspace_health


@dataclass
class DoctorEnvironment:
    python_version: tuple[int, int, int] = sys.version_info[:3]

    def import_yaml(self):
        import yaml

        return yaml

    def import_pypdf(self):
        import pypdf

        return pypdf

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def command_version(self, command: list[str]) -> str | None:
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=10)  # noqa: S603
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        return text.splitlines()[0] if text else None

    def write_probe(self, directory: Path) -> tuple[bool, str | None]:
        try:
            with tempfile.NamedTemporaryFile(prefix=".evidence-wiki-doctor-", dir=directory, delete=True):
                pass
        except OSError as exc:
            return False, str(exc)
        return True, None

    def now_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check research workspace environment capabilities.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root to diagnose. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser.parse_args(argv)


def check_item(
    check_id: str,
    label: str,
    status: str,
    required: bool,
    message: str,
    implication: str,
    remediation: str,
    *,
    version: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": status,
        "required": required,
        "message": message,
        "implication": implication,
        "remediation": remediation,
    }
    if version is not None:
        item["version"] = version
    if details is not None:
        item["details"] = details
    return item


def python_check(env: DoctorEnvironment) -> dict[str, Any]:
    version_tuple = tuple(env.python_version[:3])
    version = ".".join(str(part) for part in version_tuple)
    ok = version_tuple >= PYTHON_MINIMUM
    return check_item(
        "python",
        "Python runtime",
        "ok" if ok else "missing",
        True,
        f"Python {version} is available." if ok else f"Python {version} is older than 3.10.",
        "All workspace scripts require Python 3.10 or newer.",
        "Run the tools with Python 3.10 or newer.",
        version=version,
    )


def pyyaml_check(env: DoctorEnvironment) -> tuple[dict[str, Any], Any | None]:
    try:
        yaml = env.import_yaml()
    except ImportError as exc:
        return (
            check_item(
                "pyyaml",
                "PyYAML import",
                "missing",
                True,
                f"PyYAML is not importable: {exc}",
                "YAML configuration, contract metadata, and workspace scripts cannot run.",
                "Install PyYAML, for example with `python3 -m pip install PyYAML`.",
            ),
            None,
        )
    version = getattr(yaml, "__version__", "unknown")
    return (
        check_item(
            "pyyaml",
            "PyYAML import",
            "ok",
            True,
            "PyYAML is importable.",
            "YAML configuration and workspace metadata can be read.",
            "No action required.",
            version=str(version),
        ),
        yaml,
    )


def pypdf_check(env: DoctorEnvironment) -> dict[str, Any]:
    try:
        pypdf = env.import_pypdf()
    except ImportError as exc:
        return check_item(
            "pypdf",
            "pypdf import",
            "missing",
            True,
            f"pypdf is not importable: {exc}",
            "The portable PDF normalization backend cannot run.",
            "Reinstall EvidenceWiki so its required pypdf dependency is present, for example with "
            "`python3 -m pip install --upgrade evidence-wiki`.",
        )
    version = getattr(pypdf, "__version__", "unknown")
    return check_item(
        "pypdf",
        "pypdf import",
        "ok",
        True,
        "pypdf is importable.",
        "PDF-only records can use the portable Python normalization backend.",
        "No action required.",
        version=str(version),
    )


def tool_check(
    env: DoctorEnvironment,
    *,
    name: str,
    label: str,
    version_args: list[str],
    missing_implication: str,
    ok_implication: str,
    remediation: str,
) -> dict[str, Any]:
    path = env.which(name)
    if not path:
        return check_item(
            name,
            label,
            "missing",
            False,
            f"`{name}` was not found on PATH.",
            missing_implication,
            remediation,
        )
    version = env.command_version([path, *version_args])
    return check_item(
        name,
        label,
        "ok",
        False,
        f"`{name}` is available at {path}.",
        ok_implication,
        "No action required.",
        version=version,
        details={"path": path},
    )


def poppler_check(env: DoctorEnvironment, *, required: bool = False) -> dict[str, Any]:
    path = env.which("pdftotext")
    if not path:
        remediation = (
            "Install Poppler with `apt install poppler-utils` on Debian/Ubuntu, `brew install poppler` on macOS, "
            "or `conda install conda-forge::poppler` on Windows, and expose `pdftotext` on PATH; "
            "or set sources.pdf_extractor to pypdf. pip does not install the Poppler executable."
            if required
            else "No action is required. To enable the explicit Poppler compatibility backend, install Poppler "
            "with `apt install poppler-utils` on Debian/Ubuntu, `brew install poppler` on macOS, or "
            "`conda install conda-forge::poppler` on Windows. pip does not install the Poppler executable."
        )
        return check_item(
            "pdftotext",
            "Poppler pdftotext",
            "missing" if required else "ok",
            required,
            (
                "Configured Poppler PDF extractor requires `pdftotext`, but it was not found on PATH."
                if required
                else "Optional `pdftotext` compatibility backend was not found on PATH."
            ),
            (
                "PDF normalization cannot run until the configured backend is available."
                if required
                else "The required pypdf backend remains available for PDF-only normalization."
            ),
            remediation,
            details={"available": False, "selected": required},
        )
    version = env.command_version([path, "-v"])
    return check_item(
        "pdftotext",
        "Poppler pdftotext",
        "ok",
        required,
        (
            f"Configured Poppler PDF extractor is available at {path}."
            if required
            else f"Optional `pdftotext` compatibility backend is available at {path}."
        ),
        (
            "PDF-only records can use the configured Poppler backend."
            if required
            else "The explicit Poppler compatibility backend can be selected."
        ),
        "No action required.",
        version=version,
        details={"available": True, "path": path, "selected": required},
    )


def workspace_write_check(project_root: Path, env: DoctorEnvironment) -> dict[str, Any]:
    checked: list[str] = []
    missing: list[str] = []
    unwritable: dict[str, str] = {}

    for label, relative in WORKSPACE_DIRS.items():
        path = project_root if relative == "." else project_root / relative
        checked.append(label)
        if not path.is_dir():
            missing.append(label)
            continue
        writable, error = env.write_probe(path)
        if not writable:
            unwritable[label] = error or "write probe failed"

    ok = not missing and not unwritable
    details = {"checked": checked}
    if missing:
        details["missing"] = missing
    if unwritable:
        details["unwritable"] = unwritable
    return check_item(
        "workspace_write",
        "Workspace write permissions",
        "ok" if ok else "degraded",
        False,
        "Workspace directories are writable." if ok else "Some workspace directories are missing or not writable.",
        (
            "Workspace automation can create manifests, normalized sources, wiki pages, and reports."
            if ok
            else "Workspace automation may fail when writing manifests, normalized sources, wiki pages, or reports."
        ),
        "Run from an initialized workspace and fix directory ownership or permissions.",
        details=details,
    )


def contract_check(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    metadata_path = project_root / "workspace-system.yml"
    if not metadata_path.is_file():
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            "workspace-system.yml was not found.",
            "Contract versions are unknown; orchestrators cannot confirm starter compatibility.",
            "Run from an initialized workspace or create one with `evidence-wiki init`.",
        )
    if yaml_module is None:
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            "workspace-system.yml exists but cannot be parsed without PyYAML.",
            "Contract versions are unknown until PyYAML is installed.",
            "Install PyYAML and rerun doctor.",
        )
    try:
        document = yaml_module.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            f"workspace-system.yml could not be parsed: {exc}",
            "Contract versions are unknown; upgrade compatibility cannot be checked.",
            "Fix workspace-system.yml or restore it from the starter.",
        )
    workspace_system = document.get("workspace_system") if isinstance(document, dict) else None
    if not isinstance(workspace_system, dict):
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            "workspace-system.yml does not contain a workspace_system mapping.",
            "Contract versions are unknown; upgrade compatibility cannot be checked.",
            "Restore workspace-system.yml from the reusable starter.",
        )
    details = {
        "starter_version": workspace_system.get("starter_version"),
        "schema_version": workspace_system.get("schema_version"),
        "compatible_research_yml_contract": workspace_system.get("compatible_research_yml_contract"),
    }
    missing = [key for key, value in details.items() if not isinstance(value, str) or not value.strip()]
    return check_item(
        "contract",
        "Workspace contract metadata",
        "ok" if not missing else "degraded",
        False,
        "Workspace contract metadata is readable." if not missing else "Workspace contract metadata is incomplete.",
        (
            "Starter and research.yml contract versions can be compared before upgrades."
            if not missing
            else "Upgrade compatibility cannot be checked reliably."
        ),
        "No action required." if not missing else "Restore missing workspace_system fields.",
        details=details | ({"missing": missing} if missing else {}),
    )


def load_research_config(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    path = project_root / "research.yml"
    if yaml_module is None or not path.is_file():
        return {}
    try:
        document = yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


def semantic_retrieval_check(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    config = load_research_config(project_root, yaml_module)
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    retrieval = integrations.get("retrieval") if isinstance(integrations.get("retrieval"), dict) else {}
    semantic = retrieval.get("semantic") if isinstance(retrieval.get("semantic"), dict) else {}
    if semantic.get("enabled") is not True:
        return check_item(
            "semantic_retrieval",
            "Semantic retrieval",
            "ok",
            False,
            "Semantic retrieval is disabled; lexical retrieval remains the default.",
            "Default retrieval stays deterministic and dependency-light.",
            "Enable integrations.retrieval.semantic only after configuring an operator-managed provider.",
            details={"enabled": False},
        )
    provider = semantic.get("provider")
    transport = semantic.get("transport", "command")
    details = {
        "enabled": True,
        "provider": provider if isinstance(provider, str) else None,
        "transport": transport if isinstance(transport, str) else None,
    }
    usable = isinstance(provider, str) and bool(provider.strip())
    if transport == "command":
        command = semantic.get("command")
        command_usable = (
            isinstance(command, str)
            and bool(command.strip())
            or isinstance(command, list)
            and all(isinstance(item, str) and item.strip() for item in command)
            and bool(command)
        )
        usable = usable and command_usable
    elif transport == "http":
        endpoint = semantic.get("endpoint")
        usable = usable and isinstance(endpoint, str) and endpoint.startswith(("http://", "https://"))
    else:
        usable = False
    return check_item(
        "semantic_retrieval",
        "Semantic retrieval",
        "ok" if usable else "degraded",
        False,
        "Semantic retrieval is configured." if usable else "Semantic retrieval is enabled but not usable.",
        (
            "Query mode can run best-effort hybrid lexical/semantic ranking."
            if usable
            else "Query mode will fall back to lexical retrieval until semantic provider settings are fixed."
        ),
        "Configure provider plus command or http endpoint, or disable integrations.retrieval.semantic.",
        details=details,
    )


def secret_exposure_check(project_root: Path) -> dict[str, Any]:
    candidates = [project_root / ".env"]
    readable: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore"):
                pass
        except OSError:
            continue
        try:
            label = path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            label = path.resolve().as_posix()
        if label not in readable:
            readable.append(label)
    return check_item(
        "secret_exposure",
        "Secret exposure",
        "degraded" if readable else "ok",
        False,
        (
            "Readable .env file(s) are present; values were not inspected or printed."
            if readable
            else "No readable .env file found at the workspace or invocation root."
        ),
        (
            "Provider credentials can leak into source/workspace state if .env files are treated as runtime configuration."
            if readable
            else "Operator-managed per-run environment injection remains the expected secret path."
        ),
        (
            "Move provider keys into the operator secret store, rotate exposed keys, and keep repo-root .env development-only."
            if readable
            else "No action required."
        ),
        details={"readable_env_files": readable},
    )


def verdict_for(checks: list[dict[str, Any]]) -> str:
    if any(check["required"] and check["status"] != "ok" for check in checks):
        return "missing"
    if any(check["status"] != "ok" for check in checks):
        return "degraded"
    return "ok"


def build_report(project_root: Path, env: DoctorEnvironment | None = None) -> dict[str, Any]:
    env = env or DoctorEnvironment()
    project_root = project_root.expanduser().resolve()
    pyyaml, yaml_module = pyyaml_check(env)
    pypdf = pypdf_check(env)
    config = load_research_config(project_root, yaml_module)
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    poppler_required = sources.get("pdf_extractor", "pypdf") == "poppler"
    workspace_health = evaluate_workspace_health(
        project_root,
        optional_tool_availability={
            "pypdf": pypdf["status"] == "ok",
            "pdftotext": env.which("pdftotext") is not None,
        },
    )
    health_codes = ", ".join(workspace_health["finding_codes"]) or "none"
    checks = [
        python_check(env),
        pyyaml,
        pypdf,
        poppler_check(env, required=poppler_required),
        tool_check(
            env,
            name="git",
            label="Git",
            version_args=["--version"],
            missing_implication="Git-backed version-control workflows and user-edit snapshots are unavailable.",
            ok_implication="Version-control workflows and user-edit snapshots can use git.",
            remediation="Install git or run snapshot workflows without commit integration.",
        ),
        workspace_write_check(project_root, env),
        contract_check(project_root, yaml_module),
        semantic_retrieval_check(project_root, yaml_module),
        secret_exposure_check(project_root),
        check_item(
            "workspace_health",
            "Shared workspace health",
            "ok" if not workspace_health["publication_blocked"] else "missing",
            True,
            f"Workspace health is {workspace_health['status']}; finding codes: {health_codes}.",
            "Every workspace-facing command consumes these same material validity findings.",
            (
                "Apply each finding's bounded remediation before treating the workspace as valid."
                if workspace_health["findings"]
                else "No action required."
            ),
            details=workspace_health,
        ),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": env.now_utc(),
        "project_root": project_root.as_posix(),
        "verdict": verdict_for(checks),
        "workspace_health": workspace_health,
        "checks": checks,
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Research Wiki Doctor",
        "====================",
        f"Project root: {report['project_root']}",
        f"Verdict: {report['verdict']}",
        "",
    ]
    for check in report["checks"]:
        marker = check["status"].upper()
        required = "required" if check["required"] else "optional"
        lines.append(f"- {marker} {check['label']} ({required}): {check['message']}")
        lines.append(f"  Implication: {check['implication']}")
        lines.append(f"  Remediation: {check['remediation']}")
        if check.get("version"):
            lines.append(f"  Version: {check['version']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None, env: DoctorEnvironment | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    try:
        report = build_report(Path(args.project_root), env=env)
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=1)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(render_text(report), end="")
    return 1 if report["verdict"] == "missing" else 0


if __name__ == "__main__":
    raise SystemExit(main())
