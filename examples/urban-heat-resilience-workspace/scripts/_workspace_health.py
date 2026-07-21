#!/usr/bin/env python3
"""Shared, dependency-light workspace validity and readiness findings."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_INVALID = "invalid"
STATUS_PUBLICATION_BLOCKED = "publication_blocked"

REQUIRED_FILES = (
    "research.yml",
    "workspace-system.yml",
    "AGENTS.md",
    "index.md",
    "log.md",
)
REQUIRED_DIRECTORIES = ("scripts", "docs", "skills", "raw", "sources", "wiki")
REQUIRED_CONFIG_SECTIONS = ("project", "raw", "sources", "wiki", "taxonomy", "ingest", "lint", "outputs", "integrations")


def finding(
    code: str,
    severity: str,
    message: str,
    artifacts: list[str],
    remediation: str,
    readiness_effect: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "artifacts": artifacts,
        "remediation": remediation,
        "readiness_effect": readiness_effect,
    }


def _load_config(project_root: Path, findings: list[dict[str, Any]]) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        return {}
    try:
        import yaml
    except ImportError:
        findings.append(
            finding(
                "REQUIRED_DEPENDENCY_MISSING",
                "HIGH",
                "PyYAML is required to read the workspace contract.",
                ["research.yml"],
                "Install the package's required PyYAML dependency and rerun the command.",
                STATUS_INVALID,
            )
        )
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        findings.append(
            finding(
                "RESEARCH_CONFIG_INVALID",
                "HIGH",
                f"research.yml could not be parsed: {exc}",
                ["research.yml"],
                "Repair research.yml as valid YAML before running workspace automation.",
                STATUS_INVALID,
            )
        )
        return {}
    if not isinstance(document, dict):
        findings.append(
            finding(
                "RESEARCH_CONFIG_INVALID",
                "HIGH",
                "research.yml must contain a mapping.",
                ["research.yml"],
                "Restore a mapping-based research.yml from the starter or a reviewed backup.",
                STATUS_INVALID,
            )
        )
        return {}
    missing_sections = [section for section in REQUIRED_CONFIG_SECTIONS if not isinstance(document.get(section), dict)]
    if missing_sections:
        findings.append(
            finding(
                "RESEARCH_CONFIG_INCOMPLETE",
                "HIGH",
                f"research.yml lacks required mapping section(s): {', '.join(missing_sections)}.",
                ["research.yml"],
                "Restore the missing core sections from a compatible starter contract.",
                STATUS_PUBLICATION_BLOCKED,
            )
        )
    return document


def evaluate_workspace_health(
    project_root: Path,
    *,
    check_optional_tools: bool = True,
    optional_tool_availability: dict[str, bool] | None = None,
) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    findings: list[dict[str, Any]] = []
    config: dict[str, Any] = {}
    if not project_root.is_dir():
        findings.append(
            finding(
                "WORKSPACE_ROOT_MISSING",
                "HIGH",
                f"Workspace root is not a directory: {project_root}",
                [project_root.as_posix()],
                "Point the command at an initialized research workspace.",
                STATUS_INVALID,
            )
        )
    else:
        for relative in REQUIRED_FILES:
            if not (project_root / relative).is_file():
                effect = STATUS_INVALID if relative in {"research.yml", "workspace-system.yml"} else STATUS_PUBLICATION_BLOCKED
                findings.append(
                    finding(
                        "WORKSPACE_REQUIRED_FILE_MISSING",
                        "HIGH",
                        f"Required workspace file is missing: {relative}",
                        [relative],
                        "Restore the file from the compatible starter or rerun initialization in a clean target.",
                        effect,
                    )
                )
        for relative in REQUIRED_DIRECTORIES:
            if not (project_root / relative).is_dir():
                findings.append(
                    finding(
                        "WORKSPACE_REQUIRED_DIRECTORY_MISSING",
                        "HIGH",
                        f"Required workspace directory is missing: {relative}",
                        [relative],
                        "Restore the directory from the compatible starter before running automation.",
                        STATUS_PUBLICATION_BLOCKED,
                    )
                )
        config = _load_config(project_root, findings)
        wiki = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
        wiki_root = wiki.get("root", "wiki") if isinstance(wiki, dict) else "wiki"
        required_dirs = wiki.get("required_dirs", []) if isinstance(wiki, dict) else []
        if isinstance(wiki_root, str) and isinstance(required_dirs, list):
            for relative in required_dirs:
                if isinstance(relative, str) and not (project_root / wiki_root / relative).is_dir():
                    path = f"{wiki_root}/{relative}"
                    findings.append(
                        finding(
                            "WORKSPACE_CONFIGURED_DIRECTORY_MISSING",
                            "HIGH",
                            f"Configured wiki directory is missing: {path}",
                            [path, "research.yml"],
                            "Create the configured directory or correct wiki.required_dirs.",
                            STATUS_PUBLICATION_BLOCKED,
                        )
                    )

    if optional_tool_availability is not None and "pypdf" in optional_tool_availability:
        pypdf_available = optional_tool_availability["pypdf"]
    else:
        try:
            pypdf_available = importlib.util.find_spec("pypdf") is not None
        except (ImportError, ValueError):
            # A broken or partially initialized import system makes pypdf
            # unavailable for the purpose of this dependency-health check.
            pypdf_available = False
    if not pypdf_available:
        findings.append(
            finding(
                "REQUIRED_DEPENDENCY_MISSING",
                "HIGH",
                "Required pypdf dependency is unavailable; portable PDF normalization cannot run.",
                [],
                "Install the package's required dependencies, for example with "
                "`python3 -m pip install evidence-wiki`.",
                STATUS_INVALID,
            )
        )

    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    pdf_extractor = sources.get("pdf_extractor", "pypdf")
    pdftotext_available = (
        optional_tool_availability["pdftotext"]
        if optional_tool_availability is not None and "pdftotext" in optional_tool_availability
        else shutil.which("pdftotext") is not None
    )
    if pdf_extractor == "poppler" and not pdftotext_available:
        findings.append(
            finding(
                "REQUIRED_DEPENDENCY_MISSING",
                "HIGH",
                "The configured Poppler PDF extractor requires `pdftotext`, but it is unavailable.",
                ["research.yml"],
                "Install Poppler and expose `pdftotext` on PATH, or set sources.pdf_extractor to pypdf.",
                STATUS_INVALID,
            )
        )

    effects = {item["readiness_effect"] for item in findings}
    if STATUS_INVALID in effects:
        status = STATUS_INVALID
    elif STATUS_PUBLICATION_BLOCKED in effects:
        status = STATUS_PUBLICATION_BLOCKED
    elif STATUS_DEGRADED in effects:
        status = STATUS_DEGRADED
    else:
        status = STATUS_HEALTHY
    return {
        "schema_version": SCHEMA_VERSION,
        "project_root": project_root.as_posix(),
        "status": status,
        "materially_valid": status not in {STATUS_INVALID},
        "publication_blocked": status in {STATUS_INVALID, STATUS_PUBLICATION_BLOCKED},
        "finding_codes": sorted({item["code"] for item in findings}),
        "findings": findings,
    }


def health_exit_code(status: str) -> int:
    return {
        STATUS_HEALTHY: 0,
        STATUS_DEGRADED: 0,
        STATUS_INVALID: 2,
        STATUS_PUBLICATION_BLOCKED: 4,
    }.get(status, 2)
