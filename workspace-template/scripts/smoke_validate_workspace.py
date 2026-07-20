#!/usr/bin/env python3
"""Smoke validate a newly initialized research workspace."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to smoke validate a research workspace") from exc


REQUIRED_ROOT_FILES = (
    "research.yml",
    "workspace-system.yml",
    "AGENTS.md",
    "index.md",
    "log.md",
)
REQUIRED_ROOT_DIRS = ("scripts", "docs", "skills", "raw", "sources", "wiki")
REQUIRED_CONFIG_SECTIONS = (
    "project",
    "raw",
    "sources",
    "wiki",
    "taxonomy",
    "ingest",
    "lint",
    "outputs",
    "integrations",
)
REQUIRED_WORKSPACE_SYSTEM_FIELDS = (
    "starter_version",
    "schema_version",
    "created",
    "compatible_research_yml_contract",
)
STARTER_LOG_EXAMPLES = (
    "Template initialized",
    "Raw source inventory example",
    "Source normalization example",
    "Source note example",
    "Wiki health check example",
    "Cross-source synthesis example",
)
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _provider_registry import (
    ACQUISITION_PROVIDER_IDS,
    DISCOVERY_PROVIDER_IDS,
    ProviderListError,
    validate_provider_ids,
)
from _script_errors import handle_system_exit, json_mode_requested
from _workspace_health import evaluate_workspace_health

FORBIDDEN_TOP_LEVEL_DIRS = ("pilot-workspaces", "reports")
FORBIDDEN_METADATA_TOKENS = ("llm-research", "autonomo", "pilot-workspaces", "domain_pack")
FORBIDDEN_CODEBASE_AUTOMATION_KEYS = (
    "hooks",
    "install_hooks",
    "auto_commit",
    "auto_add",
    "background_agent",
    "background_agents",
    "background_automation",
    "background_sync",
    "auto_sync",
)
ACQUISITION_ALLOWED_PROVIDERS = ACQUISITION_PROVIDER_IDS
ACQUISITION_DEFAULT_TARGET_ROOT = "raw/papers"
FORBIDDEN_ACQUISITION_AUTOMATION_KEYS = (
    "hooks",
    "install_hooks",
    "auto_commit",
    "auto_add",
    "auto_fetch",
    "auto_download",
    "background_agent",
    "background_agents",
    "background_automation",
    "background_sync",
    "auto_sync",
)


@dataclass
class SmokeIssue:
    severity: str
    category: str
    message: str
    files: list[str]
    recommendation: str
    field: str | None = None
    expected: str | None = None
    actual: Any | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke validate a newly initialized research workspace.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root to validate. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser.parse_args(argv)


def project_relative(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def issue(
    results: dict[str, Any],
    severity: str,
    category: str,
    message: str,
    files: list[str] | None = None,
    recommendation: str = "",
    field: str | None = None,
    expected: str | None = None,
    actual: Any | None = None,
) -> None:
    item = asdict(
        SmokeIssue(
            severity=severity,
            category=category,
            message=message,
            files=files or [],
            recommendation=recommendation,
            field=field,
            expected=expected,
            actual=actual,
        )
    )
    results["issues"].append({key: value for key, value in item.items() if value is not None})


def load_yaml_file(project_root: Path, relative_path: str, label: str, results: dict[str, Any]) -> dict[str, Any] | None:
    path = project_root / relative_path
    if not path.exists():
        issue(
            results,
            "HIGH",
            "required_file",
            f"Missing {label}: {relative_path}",
            [relative_path],
            f"Create {relative_path} or initialize the workspace from the reusable starter.",
        )
        return None
    try:
        document = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        issue(
            results,
            "HIGH",
            "yaml",
            f"Invalid YAML in {relative_path}: {exc}",
            [relative_path],
            "Fix YAML syntax before running workspace automation.",
        )
        return None
    if not isinstance(document, dict):
        issue(
            results,
            "HIGH",
            "yaml",
            f"{relative_path} must contain a mapping",
            [relative_path],
            "Rewrite the file as a YAML mapping.",
        )
        return None
    return document


def config_mapping(config: dict[str, Any], key: str, results: dict[str, Any]) -> dict[str, Any] | None:
    value = config.get(key)
    if not isinstance(value, dict):
        issue(
            results,
            "HIGH",
            "config_shape",
            f"research.yml {key} must be a mapping",
            ["research.yml"],
            f"Set research.yml {key} to a mapping.",
            field=key,
            expected="mapping",
            actual=type(value).__name__,
        )
        return None
    return value


def config_list(value: Any, label: str, results: dict[str, Any]) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        issue(
            results,
            "HIGH",
            "config_shape",
            f"research.yml {label} must be a list of strings",
            ["research.yml"],
            f"Set {label} to a YAML list of relative paths.",
            field=label,
            expected="list of strings",
            actual=type(value).__name__,
        )
        return None
    return value


def validate_workspace_relative_path(
    relative_path: Any,
    results: dict[str, Any],
    label: str,
    *,
    under_sources: bool = False,
) -> str | None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        issue(
            results,
            "HIGH",
            "config_path",
            f"{label} must be a non-empty workspace-relative path",
            ["research.yml"],
            f"Set {label} to a workspace-relative path.",
            field=label,
            expected="non-empty workspace-relative path",
            actual=type(relative_path).__name__,
        )
        return None
    raw = relative_path.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    reason: str | None = None
    if "://" in normalized or parsed.scheme:
        reason = "must not be a URL"
    elif len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        reason = "must not be an absolute path"
    else:
        path = PurePosixPath(normalized)
        if path.is_absolute():
            reason = "must not be an absolute path"
        elif ".." in path.parts:
            reason = "must not contain '..'"
        elif under_sources and path.as_posix() != "sources" and not path.as_posix().startswith("sources/"):
            reason = "must stay under sources/"
    if reason is not None:
        issue(
            results,
            "HIGH",
            "config_path",
            f"{label} must be a workspace-relative path: {reason}",
            ["research.yml"],
            f"Set {label} to a safe workspace-relative path.",
            field=label,
            expected="workspace-relative path",
            actual=relative_path,
        )
        return None
    return PurePosixPath(normalized).as_posix()


def require_relative_dir(
    project_root: Path,
    relative_path: str,
    results: dict[str, Any],
    category: str,
    label: str | None = None,
    *,
    under_sources: bool = False,
) -> None:
    safe_path = validate_workspace_relative_path(
        relative_path,
        results,
        label or relative_path,
        under_sources=under_sources,
    )
    if safe_path is None:
        return
    path = project_root / safe_path
    if not path.is_dir():
        issue(
            results,
            "HIGH",
            category,
            f"Missing directory: {safe_path}",
            [safe_path],
            f"Create {safe_path} or update the corresponding research.yml setting.",
        )


def is_generated_sources_path(relative_path: str) -> bool:
    path = PurePosixPath(relative_path.replace("\\", "/"))
    parts = path.parts
    return not path.is_absolute() and ".." not in parts and len(parts) >= 2 and parts[0] == "sources"


def is_raw_target_path(relative_path: str) -> bool:
    path = PurePosixPath(relative_path.replace("\\", "/"))
    parts = path.parts
    return not path.is_absolute() and ".." not in parts and len(parts) >= 2 and parts[0] == "raw"


def validate_provider_list(value: Any, *, phase: str = "acquisition") -> tuple[list[str], str | None, list[str]]:
    try:
        validated = validate_provider_ids(value, phase=phase)
    except ProviderListError as exc:
        return [], str(exc), []
    return list(validated.providers), None, list(validated.legacy_strategies)


def check_codebase_analysis(project_root: Path, integrations_config: dict[str, Any], results: dict[str, Any]) -> None:
    codebase = integrations_config.get("codebase_analysis")
    if codebase is None:
        return
    if not isinstance(codebase, dict):
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.codebase_analysis must be a mapping",
            ["research.yml"],
            "Set integrations.codebase_analysis to a mapping or remove it.",
            field="integrations.codebase_analysis",
            expected="mapping",
            actual=type(codebase).__name__,
        )
        return
    enabled = codebase.get("enabled", False)
    if not isinstance(enabled, bool):
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.codebase_analysis.enabled must be a boolean",
            ["research.yml"],
            "Set integrations.codebase_analysis.enabled to true or false.",
            field="integrations.codebase_analysis.enabled",
            expected="boolean",
            actual=type(enabled).__name__,
        )
        return
    for key in FORBIDDEN_CODEBASE_AUTOMATION_KEYS:
        if codebase.get(key):
            issue(
                results,
                "HIGH",
                "integration_safety",
                f"research.yml integrations.codebase_analysis.{key} is not allowed during initialization",
                ["research.yml"],
                "Keep codebase analysis read-only and do not install hooks or background sync by default.",
                field=f"integrations.codebase_analysis.{key}",
                expected="false or unset",
                actual=codebase.get(key),
            )
    if "read_only" in codebase and codebase.get("read_only") is not True:
        issue(
            results,
            "HIGH",
            "integration_safety",
            "research.yml integrations.codebase_analysis.read_only must be true during initialization",
            ["research.yml"],
            "Keep codebase analysis read-only while initializing the workspace.",
            field="integrations.codebase_analysis.read_only",
            expected="true",
            actual=codebase.get("read_only"),
        )
    output_dir = codebase.get("output_dir", "sources/code_wikis")
    if not isinstance(output_dir, str) or not output_dir.strip() or not is_generated_sources_path(output_dir):
        issue(
            results,
            "HIGH",
            "integration_safety",
            "research.yml integrations.codebase_analysis.output_dir must be under sources/",
            ["research.yml"],
            "Set integrations.codebase_analysis.output_dir to a generated sources path such as sources/code_wikis.",
            field="integrations.codebase_analysis.output_dir",
            expected="workspace-relative path under sources/",
            actual=output_dir,
        )
        return
    if enabled:
        provider = codebase.get("provider")
        if not isinstance(provider, str) or not provider.strip() or provider.strip() == "none":
            issue(
                results,
                "HIGH",
                "config_shape",
                "enabled codebase analysis must name a provider",
                ["research.yml"],
                "Set integrations.codebase_analysis.provider to the adapter name, such as agent-wiki-cli.",
                field="integrations.codebase_analysis.provider",
                expected="non-empty provider",
                actual=provider,
            )
        require_relative_dir(
            project_root,
            output_dir,
            results,
            "configured_directory",
            "integrations.codebase_analysis.output_dir",
            under_sources=True,
        )


def check_acquisition(project_root: Path, integrations_config: dict[str, Any], results: dict[str, Any]) -> None:
    acquisition = integrations_config.get("acquisition")
    if acquisition is None:
        return
    if not isinstance(acquisition, dict):
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.acquisition must be a mapping",
            ["research.yml"],
            "Set integrations.acquisition to a mapping or remove it.",
            field="integrations.acquisition",
            expected="mapping",
            actual=type(acquisition).__name__,
        )
        return
    enabled = acquisition.get("enabled", False)
    if not isinstance(enabled, bool):
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.acquisition.enabled must be a boolean",
            ["research.yml"],
            "Set integrations.acquisition.enabled to true or false.",
            field="integrations.acquisition.enabled",
            expected="boolean",
            actual=type(enabled).__name__,
        )
        return
    providers, provider_error, _legacy = validate_provider_list(acquisition.get("providers", []))
    if provider_error is not None:
        issue(
            results,
            "HIGH",
            "config_shape",
            f"research.yml integrations.acquisition.providers {provider_error}",
            ["research.yml"],
            f"Use only supported acquisition providers: {', '.join(ACQUISITION_ALLOWED_PROVIDERS)}.",
            field="integrations.acquisition.providers",
            expected="list of supported provider identifiers",
            actual=acquisition.get("providers"),
        )
    elif enabled and not providers:
        issue(
            results,
            "HIGH",
            "config_shape",
            "enabled acquisition must list at least one provider",
            ["research.yml"],
            "Set integrations.acquisition.providers to one or more supported providers.",
            field="integrations.acquisition.providers",
            expected="non-empty provider list",
            actual=providers,
        )
    target_root = acquisition.get("target_root", ACQUISITION_DEFAULT_TARGET_ROOT)
    if not isinstance(target_root, str) or not target_root.strip() or not is_raw_target_path(target_root):
        issue(
            results,
            "HIGH",
            "integration_safety",
            "research.yml integrations.acquisition.target_root must be under raw/",
            ["research.yml"],
            "Set integrations.acquisition.target_root to a raw evidence path such as raw/papers.",
            field="integrations.acquisition.target_root",
            expected="workspace-relative path under raw/",
            actual=target_root,
        )
    elif enabled:
        require_relative_dir(
            project_root,
            target_root,
            results,
            "configured_directory",
            "integrations.acquisition.target_root",
        )
    max_downloads = acquisition.get("max_downloads_per_run", 10)
    if not isinstance(max_downloads, int) or isinstance(max_downloads, bool) or max_downloads < 1:
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.acquisition.max_downloads_per_run must be a positive integer",
            ["research.yml"],
            "Set integrations.acquisition.max_downloads_per_run to a positive integer.",
            field="integrations.acquisition.max_downloads_per_run",
            expected="positive integer",
            actual=max_downloads,
        )
    require_license_check = acquisition.get("require_license_check", True)
    if not isinstance(require_license_check, bool):
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.acquisition.require_license_check must be a boolean",
            ["research.yml"],
            "Set integrations.acquisition.require_license_check to true or false.",
            field="integrations.acquisition.require_license_check",
            expected="boolean",
            actual=type(require_license_check).__name__,
        )
    for key in FORBIDDEN_ACQUISITION_AUTOMATION_KEYS:
        if acquisition.get(key):
            issue(
                results,
                "HIGH",
                "integration_safety",
                f"research.yml integrations.acquisition.{key} is not allowed during initialization",
                ["research.yml"],
                "Keep acquisition explicit and do not install hooks, auto-fetch, or background sync by default.",
                field=f"integrations.acquisition.{key}",
                expected="false or unset",
                actual=acquisition.get(key),
            )


def check_discovery(project_root: Path, integrations_config: dict[str, Any], results: dict[str, Any]) -> None:
    discovery = integrations_config.get("discovery")
    if discovery is None:
        return
    if not isinstance(discovery, dict):
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.discovery must be a mapping",
            ["research.yml"],
            "Set integrations.discovery to a mapping or remove it.",
            field="integrations.discovery",
            expected="mapping",
            actual=type(discovery).__name__,
        )
        return
    enabled = discovery.get("enabled", False)
    if not isinstance(enabled, bool):
        issue(
            results,
            "HIGH",
            "config_shape",
            "research.yml integrations.discovery.enabled must be a boolean",
            ["research.yml"],
            "Set integrations.discovery.enabled to true or false.",
            field="integrations.discovery.enabled",
            expected="boolean",
            actual=type(enabled).__name__,
        )
        return

    providers, provider_error, legacy = validate_provider_list(
        discovery.get("providers", []),
        phase="discovery",
    )
    if provider_error is not None:
        issue(
            results,
            "HIGH",
            "config_shape",
            f"research.yml integrations.discovery.providers {provider_error}",
            ["research.yml"],
            f"Use only supported discovery providers: {', '.join(DISCOVERY_PROVIDER_IDS)}.",
            field="integrations.discovery.providers",
            expected="list of supported provider identifiers",
            actual=discovery.get("providers"),
        )
    elif enabled and not providers:
        issue(
            results,
            "HIGH",
            "config_shape",
            "enabled discovery must list at least one provider",
            ["research.yml"],
            "Set integrations.discovery.providers to one or more explicitly authorized providers.",
            field="integrations.discovery.providers",
            expected="non-empty provider list",
            actual=providers,
        )
    for strategy in legacy:
        issue(
            results,
            "LOW",
            "deprecated_config",
            f"integrations.discovery.providers contains legacy strategy id {strategy!r}",
            ["research.yml"],
            (
                "Remove the strategy id and authorize its concrete provider instead: legal requires search, "
                "author publication expansion requires openalex, and companions uses github and/or search."
            ),
            field="integrations.discovery.providers",
            expected="concrete provider identifiers",
            actual=strategy,
        )

    candidate_store = discovery.get("candidate_store_path", "sources/discovery/candidates.jsonl")
    safe_store = validate_workspace_relative_path(
        candidate_store,
        results,
        "integrations.discovery.candidate_store_path",
        under_sources=True,
    )
    if safe_store is not None:
        if not safe_store.lower().endswith(".jsonl"):
            issue(
                results,
                "HIGH",
                "config_path",
                "research.yml integrations.discovery.candidate_store_path must use the .jsonl extension",
                ["research.yml"],
                "Set candidate_store_path to a JSONL file under sources/.",
                field="integrations.discovery.candidate_store_path",
                expected="workspace-relative .jsonl path under sources/",
                actual=candidate_store,
            )
        elif enabled:
            parent = PurePosixPath(safe_store).parent.as_posix()
            require_relative_dir(
                project_root,
                parent,
                results,
                "configured_directory",
                "integrations.discovery.candidate_store_path parent",
                under_sources=True,
            )

    if "search" in providers:
        search = discovery.get("search")
        provider = search.get("provider") if isinstance(search, dict) else None
        if provider not in {"fixture", "command", "http"}:
            issue(
                results,
                "HIGH",
                "config_shape",
                "enabled search discovery must configure a fixture, command, or http backend",
                ["research.yml"],
                "Set integrations.discovery.search.provider and its provider-specific options.",
                field="integrations.discovery.search.provider",
                expected="fixture, command, or http",
                actual=provider,
            )


def require_relative_file(
    project_root: Path,
    relative_path: str,
    results: dict[str, Any],
    label: str | None = None,
    *,
    under_sources: bool = False,
) -> None:
    safe_path = validate_workspace_relative_path(
        relative_path,
        results,
        label or relative_path,
        under_sources=under_sources,
    )
    if safe_path is None:
        return
    path = project_root / safe_path
    if not path.is_file():
        issue(
            results,
            "HIGH",
            "required_file",
            f"Missing required file: {safe_path}",
            [safe_path],
            "Initialize the workspace from the reusable starter or restore the missing file.",
        )


def read_text(project_root: Path, relative_path: str, results: dict[str, Any]) -> str | None:
    path = project_root / relative_path
    try:
        text = path.read_text()
    except OSError as exc:
        issue(
            results,
            "HIGH",
            "readability",
            f"Cannot read {relative_path}: {exc}",
            [relative_path],
            "Fix file permissions or restore the file.",
        )
        return None
    if not text.strip():
        issue(
            results,
            "HIGH",
            "readability",
            f"{relative_path} is empty",
            [relative_path],
            "Restore starter content or regenerate the workspace.",
        )
    return text


def check_root_entries(project_root: Path, results: dict[str, Any]) -> None:
    for relative_path in REQUIRED_ROOT_FILES:
        require_relative_file(project_root, relative_path, results)
    for relative_path in REQUIRED_ROOT_DIRS:
        require_relative_dir(project_root, relative_path, results, "required_directory")


def check_workspace_system(project_root: Path, metadata: dict[str, Any] | None, results: dict[str, Any]) -> None:
    text = read_text(project_root, "workspace-system.yml", results) if (project_root / "workspace-system.yml").exists() else ""
    if text:
        lowered = text.lower()
        for token in FORBIDDEN_METADATA_TOKENS:
            if token in lowered:
                issue(
                    results,
                    "HIGH",
                    "domain_neutrality",
                    f"workspace-system.yml contains domain-specific token: {token}",
                    ["workspace-system.yml"],
                    "Keep starter metadata domain-neutral; move domain-specific settings to research.yml or a domain pack.",
                )
    if metadata is None:
        return
    workspace_system = metadata.get("workspace_system")
    if not isinstance(workspace_system, dict):
        issue(
            results,
            "HIGH",
            "metadata",
            "workspace-system.yml must contain a workspace_system mapping",
            ["workspace-system.yml"],
            "Restore workspace-system.yml from the reusable starter.",
            field="workspace_system",
            expected="mapping",
            actual=type(workspace_system).__name__,
        )
        return
    for field in REQUIRED_WORKSPACE_SYSTEM_FIELDS:
        value = workspace_system.get(field)
        if not isinstance(value, str) or not value.strip():
            issue(
                results,
                "HIGH",
                "metadata",
                f"workspace-system.yml missing readable field: {field}",
                ["workspace-system.yml"],
                f"Set workspace_system.{field} to a non-empty string.",
                field=f"workspace_system.{field}",
                expected="non-empty string",
                actual=type(value).__name__,
            )


def check_project_identity(config: dict[str, Any], project_config: dict[str, Any], results: dict[str, Any]) -> None:
    for field in ("name", "description", "owner_goal", "language"):
        value = project_config.get(field)
        if not isinstance(value, str) or not value.strip():
            issue(
                results,
                "HIGH",
                "project_identity",
                f"project.{field} must be a non-empty string",
                ["research.yml"],
                f"Set project.{field} during workspace initialization.",
                field=f"project.{field}",
                expected="non-empty string",
                actual=type(value).__name__,
            )
    if project_config.get("name") == "evidence-wiki":
        issue(
            results,
            "HIGH",
            "project_identity",
            "project.name still uses the reusable starter placeholder",
            ["research.yml"],
            "Initialize the workspace with a project-specific name.",
            field="project.name",
            expected="project-specific name",
            actual="evidence-wiki",
        )


def check_research_config(project_root: Path, config: dict[str, Any] | None, results: dict[str, Any]) -> None:
    if config is None:
        return
    for section in REQUIRED_CONFIG_SECTIONS:
        if section not in config:
            issue(
                results,
                "HIGH",
                "config_shape",
                f"research.yml missing top-level section: {section}",
                ["research.yml"],
                f"Add the {section} section or regenerate research.yml from the starter.",
                field=section,
                expected="top-level mapping",
                actual="missing",
            )

    project_config = config_mapping(config, "project", results)
    raw_config = config_mapping(config, "raw", results)
    sources_config = config_mapping(config, "sources", results)
    wiki_config = config_mapping(config, "wiki", results)
    outputs_config = config_mapping(config, "outputs", results)
    integrations_config = config_mapping(config, "integrations", results)
    if project_config is not None:
        check_project_identity(config, project_config, results)
    if raw_config is not None:
        raw_roots = config_list(raw_config.get("source_roots"), "raw.source_roots", results)
        if raw_roots is not None:
            for raw_root in raw_roots:
                require_relative_dir(project_root, raw_root, results, "configured_directory", "raw.source_roots")
    if sources_config is not None:
        manifest_path = sources_config.get("manifest_path")
        if isinstance(manifest_path, str) and manifest_path.strip():
            require_relative_file(project_root, manifest_path, results, "sources.manifest_path", under_sources=True)
        else:
            issue(
                results,
                "HIGH",
                "config_shape",
                "research.yml sources.manifest_path must be a non-empty string",
                ["research.yml"],
                "Set sources.manifest_path to a workspace-relative manifest file.",
                field="sources.manifest_path",
                expected="non-empty string",
                actual=type(manifest_path).__name__,
            )
        for field in ("normalized_dir", "cards_dir"):
            value = sources_config.get(field)
            if isinstance(value, str) and value.strip():
                require_relative_dir(
                    project_root,
                    value,
                    results,
                    "configured_directory",
                    f"sources.{field}",
                    under_sources=True,
                )
            else:
                issue(
                    results,
                    "HIGH",
                    "config_shape",
                    f"research.yml sources.{field} must be a non-empty string",
                    ["research.yml"],
                    f"Set sources.{field} to a workspace-relative directory.",
                    field=f"sources.{field}",
                    expected="non-empty string",
                    actual=type(value).__name__,
                )
    if wiki_config is not None:
        wiki_root = wiki_config.get("root")
        if isinstance(wiki_root, str) and wiki_root.strip():
            safe_wiki_root = validate_workspace_relative_path(wiki_root, results, "wiki.root")
            if safe_wiki_root is not None:
                require_relative_dir(project_root, safe_wiki_root, results, "configured_directory", "wiki.root")
            required_dirs = config_list(wiki_config.get("required_dirs"), "wiki.required_dirs", results)
            if safe_wiki_root is not None and required_dirs is not None:
                for subdir in required_dirs:
                    require_relative_dir(
                        project_root,
                        f"{safe_wiki_root}/{subdir}",
                        results,
                        "configured_directory",
                        "wiki.required_dirs",
                    )
        else:
            issue(
                results,
                "HIGH",
                "config_shape",
                "research.yml wiki.root must be a non-empty string",
                ["research.yml"],
                "Set wiki.root to the maintained wiki directory.",
                field="wiki.root",
                expected="non-empty string",
                actual=type(wiki_root).__name__,
            )
    if outputs_config is not None:
        default_dir = outputs_config.get("default_dir")
        if isinstance(default_dir, str) and default_dir.strip():
            require_relative_dir(project_root, default_dir, results, "configured_directory", "outputs.default_dir")
    if integrations_config is not None:
        check_codebase_analysis(project_root, integrations_config, results)
        check_acquisition(project_root, integrations_config, results)
        check_discovery(project_root, integrations_config, results)
    check_domain_pack(project_root, config, results)


def titleize_directory(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", "-").split("-") if part)


def check_index(project_root: Path, config: dict[str, Any] | None, results: dict[str, Any]) -> None:
    text = read_text(project_root, "index.md", results)
    if text is None or config is None:
        return
    project_config = config.get("project") if isinstance(config.get("project"), dict) else {}
    project_name = project_config.get("name") if isinstance(project_config, dict) else None
    if isinstance(project_name, str) and project_name and project_name not in text:
        issue(
            results,
            "MEDIUM",
            "index",
            "index.md does not contain the configured project name",
            ["index.md"],
            "Regenerate index.md during workspace initialization.",
            field="project.name",
            expected=project_name,
            actual="missing from index.md",
        )
    wiki_config = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    required_dirs = wiki_config.get("required_dirs") if isinstance(wiki_config, dict) else []
    if isinstance(required_dirs, list):
        for subdir in required_dirs:
            if isinstance(subdir, str) and f"## {titleize_directory(subdir)}" not in text:
                issue(
                    results,
                    "MEDIUM",
                    "index",
                    f"index.md missing section for wiki directory: {subdir}",
                    ["index.md"],
                    "Regenerate index.md from the configured wiki directories.",
                )


def check_log(project_root: Path, results: dict[str, Any]) -> None:
    text = read_text(project_root, "log.md", results)
    if text is None:
        return
    if "setup |" not in text:
        issue(
            results,
            "HIGH",
            "log",
            "log.md does not contain a setup entry",
            ["log.md"],
            "Add a setup entry documenting workspace initialization.",
        )
    for example in STARTER_LOG_EXAMPLES:
        if example in text:
            issue(
                results,
                "MEDIUM",
                "log",
                f"log.md still contains starter example text: {example}",
                ["log.md"],
                "Regenerate log.md during workspace initialization so it records this project, not starter examples.",
            )


def check_agent_instructions(project_root: Path, results: dict[str, Any]) -> None:
    read_text(project_root, "AGENTS.md", results)


def check_domain_neutral_root(project_root: Path, config: dict[str, Any] | None, results: dict[str, Any]) -> None:
    for dirname in FORBIDDEN_TOP_LEVEL_DIRS:
        if (project_root / dirname).exists():
            issue(
                results,
                "HIGH",
                "domain_neutrality",
                f"Generated workspace contains top-level non-starter directory: {dirname}",
                [dirname],
                "Remove copied repository/pilot artifacts from the generated workspace.",
            )
    domain_packs_dir = project_root / "domain-packs"
    has_domain_pack_config = isinstance(config, dict) and isinstance(config.get("domain_pack"), dict)
    if domain_packs_dir.exists() and not has_domain_pack_config:
        issue(
            results,
            "MEDIUM",
            "domain_pack",
            "domain-packs directory exists but research.yml has no domain_pack configuration",
            ["domain-packs"],
            "Remove the unused domain pack directory or configure research.yml domain_pack.",
        )


def check_domain_pack_path(project_root: Path, relative_path: Any, results: dict[str, Any], field: str) -> None:
    if relative_path in (None, ""):
        return
    if not isinstance(relative_path, str):
        issue(
            results,
            "HIGH",
            "domain_pack",
            f"domain_pack.{field} must be a string path",
            ["research.yml"],
            f"Set domain_pack.{field} to a workspace-relative path.",
            field=f"domain_pack.{field}",
            expected="string path",
            actual=type(relative_path).__name__,
        )
        return
    if not (project_root / relative_path).exists():
        issue(
            results,
            "HIGH",
            "domain_pack",
            f"Configured domain pack path does not exist: {relative_path}",
            [relative_path],
            "Copy the domain pack into the workspace or update the path in research.yml.",
            field=f"domain_pack.{field}",
        )


def check_domain_pack(project_root: Path, config: dict[str, Any], results: dict[str, Any]) -> None:
    domain_pack = config.get("domain_pack")
    if domain_pack is None:
        return
    if not isinstance(domain_pack, dict):
        issue(
            results,
            "HIGH",
            "domain_pack",
            "research.yml domain_pack must be a mapping when present",
            ["research.yml"],
            "Set domain_pack to a mapping or remove it.",
            field="domain_pack",
            expected="mapping",
            actual=type(domain_pack).__name__,
        )
        return
    for field in ("taxonomy_doc", "claims_doc"):
        check_domain_pack_path(project_root, domain_pack.get(field), results, field)
    scaffolds = domain_pack.get("scaffolds")
    if isinstance(scaffolds, dict):
        for key, value in scaffolds.items():
            check_domain_pack_path(project_root, value, results, f"scaffolds.{key}")
    elif scaffolds is not None:
        issue(
            results,
            "HIGH",
            "domain_pack",
            "research.yml domain_pack.scaffolds must be a mapping when present",
            ["research.yml"],
            "Set domain_pack.scaffolds to a mapping of scaffold names to paths.",
            field="domain_pack.scaffolds",
            expected="mapping",
            actual=type(scaffolds).__name__,
        )


def summarize(results: dict[str, Any]) -> None:
    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for item in results["issues"]:
        by_severity[item["severity"]] = by_severity.get(item["severity"], 0) + 1
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
    results["summary"] = {
        "issue_count": len(results["issues"]),
        "by_severity": dict(sorted(by_severity.items())),
        "by_category": dict(sorted(by_category.items())),
    }
    results["ok"] = not any(item["severity"] in {"HIGH", "MEDIUM"} for item in results["issues"])


def run_checks(project_root: Path) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    workspace_health = evaluate_workspace_health(project_root)
    results: dict[str, Any] = {
        "project_root": project_root.as_posix(),
        "ok": False,
        "issues": [],
        "summary": {},
        "workspace_health": workspace_health,
    }
    if not project_root.is_dir():
        issue(
            results,
            "HIGH",
            "project_root",
            f"Project root is not a directory: {project_root}",
            [project_root.as_posix()],
            "Point --project-root to an initialized research workspace directory.",
        )
        summarize(results)
        return results

    check_root_entries(project_root, results)
    config = load_yaml_file(project_root, "research.yml", "research.yml", results)
    metadata = load_yaml_file(project_root, "workspace-system.yml", "workspace-system.yml", results)
    check_workspace_system(project_root, metadata, results)
    check_research_config(project_root, config, results)
    check_agent_instructions(project_root, results)
    check_index(project_root, config, results)
    check_log(project_root, results)
    check_domain_neutral_root(project_root, config, results)
    summarize(results)
    results["ok"] = bool(results["ok"] and not workspace_health["publication_blocked"])
    return results


def render_text(results: dict[str, Any]) -> str:
    workspace_health = results.get("workspace_health", {})
    health_findings = workspace_health.get("findings", []) if isinstance(workspace_health, dict) else []
    if results["ok"] and not results["issues"] and not health_findings:
        return "Smoke validation passed.\n"

    heading = "Smoke validation passed." if results["ok"] else "Smoke validation failed."
    lines = [heading, ""]
    for finding_item in health_findings:
        lines.append(
            f"- {finding_item['severity']} {finding_item['code']}: {finding_item['message']}"
        )
        lines.append(f"  Remediation: {finding_item['remediation']}")
    for item in results["issues"]:
        files = ", ".join(item.get("files", [])) or "(none)"
        lines.append(f"- {item['severity']} {item['category']}: {item['message']}")
        lines.append(f"  Files: {files}")
        if item.get("recommendation"):
            lines.append(f"  Recommendation: {item['recommendation']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    try:
        results = run_checks(Path(args.project_root))
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=2)
    if args.format == "json":
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(render_text(results), end="")
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
