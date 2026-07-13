#!/usr/bin/env python3
"""Create a configured research workspace from the reusable starter."""

from __future__ import annotations

import argparse
import copy
import os
import re
import shutil
import sys
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to initialize a research workspace") from exc

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
# Shared containment definition (SEC-E1-T03): refuses a symlink, then requires the
# resolved path to stay inside an already-resolved root. Reused here for the
# init/upgrade *writer* paths so the readers and writers cannot drift (SEC-E1-T04).
from _handoff_signature import handoff_secret, sign_handoff
from _script_errors import error_envelope
from _workspace_locks import LockUnavailableError, workspace_lock
from source_inventory import is_contained_nonsymlink

REQUIRED_STARTER_FILES = (
    "research.yml",
    "workspace-system.yml",
    "AGENTS.md",
    "index.md",
    "log.md",
)
EXCLUDED_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
PROFILE_ROOT_KEY = "workspace_init"
PROFILE_SCHEMA_VERSION = "0.1"
SUPPORTED_WORKSPACE_SCHEMA_VERSIONS = ("0.1",)
SUPPORTED_RESEARCH_YML_CONTRACTS = ("0.1",)
PROFILE_CONFIG_SECTIONS = (
    "raw",
    "sources",
    "wiki",
    "taxonomy",
    "ingest",
    "run",
    "lint",
    "outputs",
    "integrations",
)
ALLOWED_PACK_FILE_SUFFIXES = {".csv", ".json", ".md", ".txt", ".yaml", ".yml"}
WINDOWS_RESERVED_PACK_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
PROFILE_ALLOWED_KEYS = frozenset(
    (
        "schema_version",
        "target_path",
        "project",
        "domain_guidance",
        "domain_pack",
        "initial_sources",
        "existing_wiki_path",
        "claim_strictness",
        "questions",
        "questions_asked",
        "inferred_answers",
        "validation",
        "assumptions",
        "skipped_decisions",
        "next_actions",
        "raw",
        "sources",
        "wiki",
        "taxonomy",
        "ingest",
        "run",
        "lint",
        "outputs",
        "integrations",
        "research_yml",
        "init_report",
        "handoff",
    )
)
PROFILE_REQUIRED_PROJECT_FIELDS = ("name", "description", "owner_goal", "language")
PROFILE_PROJECT_FIELDS = frozenset(PROFILE_REQUIRED_PROJECT_FIELDS)
PROFILE_RAW_FIELDS = frozenset(("immutable", "source_roots"))
PROFILE_INGEST_FIELDS = frozenset(
    ("source_note_required", "claim_extraction", "ask_before_large_wiki_update", "large_update_page_threshold", "update_log")
)
PROFILE_OUTPUT_FIELDS = frozenset(("default_dir", "supported_formats"))
PROFILE_CLAIM_STRICTNESS_VALUES = ("none", "source_notes", "structured_claims")
PROFILE_DOMAIN_GUIDANCE_MODES = ("none", "domain_pack", "project_local", "deferred")
QUESTION_WIKI_DIR = "questions"
QUESTION_DEFAULT_PRIORITY = "medium"
QUESTION_DEFAULT_ORIGIN = "parent_agent"
QUESTION_PRIORITIES = ("high", "medium", "low")
SEED_QUESTION_ITEM_KEYS = frozenset(("id", "question", "text", "priority", "origin"))
QUESTION_PAGE_DEFAULT_INTRO = "Seeded during workspace initialization."
# Frontmatter keys emitted by render_question_page. Kept explicit so intake
# tooling can verify config-required fields are covered before writing pages.
QUESTION_PAGE_FRONTMATTER_KEYS = (
    "type",
    "created",
    "updated",
    "status",
    "priority",
    "origin",
    "source_ids",
    "summary",
    "question",
)
PROJECT_LOCAL_GUIDANCE_DEFAULT_PATH = "docs/project-domain-guidance.md"
PROJECT_LOCAL_GUIDANCE_LIST_FIELDS = (
    "extraction_targets",
    "source_priorities",
    "claim_types",
    "output_scaffolds",
    "filing_rules",
)
PROJECT_LOCAL_GUIDANCE_OPTIONAL_LIST_FIELDS = ("promotion_notes",)
PROJECT_LOCAL_GUIDANCE_FIELDS = frozenset(
    ("mode", "rationale", "path", "scope")
    + PROJECT_LOCAL_GUIDANCE_LIST_FIELDS
    + PROJECT_LOCAL_GUIDANCE_OPTIONAL_LIST_FIELDS
)
INIT_REPORT_DEFAULT_PATH = "docs/workspace-init-report.md"
INIT_REPORT_FIELDS = frozenset(("path",))
HANDOFF_FIELDS = ("task_id", "requested_by", "chain_run_id")
RUN_BUDGET_FIELDS = (
    "max_questions_per_run",
    "max_source_requests_per_run",
    "max_releases_per_run",
    "max_open_questions_total",
    "max_intake_per_hour",
    "max_mcp_intake_batch_questions",
    "max_discovery_results_per_run",
    "max_academic_provider_requests_per_run",
    "max_manual_url_deliveries_per_run",
    "claim_staleness_hours",
)
DEFAULT_RELEASES_PER_QUESTION = 3
VALIDATION_FIELDS = frozenset(("commands", "results"))
VALIDATION_RESULT_FIELDS = frozenset(("command", "status", "summary"))
VALIDATION_STATUSES = ("pending", "passed", "failed", "blocked", "not_run")
DEFAULT_VALIDATION_COMMANDS = (
    "python3 scripts/smoke_validate_workspace.py --format text",
    "python3 scripts/source_inventory.py --dry-run --report",
    "python3 scripts/normalize_sources.py --all --dry-run",
    "python3 scripts/lint.py --format text",
)
CODEBASE_ANALYSIS_DEFAULT_OUTPUT_DIR = "sources/code_wikis"
ACQUISITION_ALLOWED_PROVIDERS = ("arxiv", "openalex", "github", "web")
ACQUISITION_DEFAULT_TARGET_ROOT = "raw/papers"
OUTPUT_SUPPORTED_FORMATS = ("markdown", "marp", "csv", "json", "presentation_outline")
SOURCE_LIFECYCLE_STATUSES = (
    "discovered",
    "normalized",
    "noted",
    "integrated",
    "deferred",
    "superseded",
    "rejected",
)
WINDOWS_RESERVED_PATH_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
RESTRICTIVE_FILE_MODE = 0o600
RESTRICTIVE_DIR_MODE = 0o700
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
FORBIDDEN_GIT_AUTOMATION_KEYS = (
    "hooks",
    "install_hooks",
    "auto_commit",
    "auto_add",
    "background_agent",
    "background_agents",
    "background_automation",
    "auto_sync",
)
DEFAULT_SECTION_DESCRIPTIONS = {
    "sources": "Source notes that summarize and cite normalized source records.",
    "entities": "Organizations, people, projects, tools, labs, or other named things.",
    "concepts": "Reusable ideas, definitions, taxonomies, and conceptual distinctions.",
    "methods": "Techniques, workflows, algorithms, or research methods.",
    "systems": "Concrete systems, implementations, products, or architectures.",
    "benchmarks": "Evaluation suites, tasks, leaderboards, and scoring protocols.",
    "datasets": "Datasets, corpora, test sets, and generated data resources.",
    "claims": "Structured evidence statements extracted from sources.",
    "synthesis": "Cross-source maps, comparisons, literature reviews, and summaries.",
    "questions": "Open questions, research gaps, and planned investigations.",
    "decisions": "Decision records about project direction or implementation choices.",
    "outputs": "Reusable generated artifacts such as reports, decks, tables, and exports.",
}


@dataclass(frozen=True)
class InitOptions:
    starter_root: Path
    target: Path
    project_name: str
    project_description: str
    owner_goal: str
    language: str
    domain_pack: str | None
    profile_path: Path | None
    profile: dict[str, Any]
    dry_run: bool
    force: bool
    scope_root: Path | None = None


@dataclass(frozen=True)
class DomainPackSelection:
    name: str
    source_path: Path
    target_relative: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a research workspace from the reusable starter.")
    parser.add_argument("--target", help="Target workspace path to create.")
    parser.add_argument("--project-name", help="Short stable project name.")
    parser.add_argument("--project-description", help="One-sentence project description.")
    parser.add_argument("--owner-goal", help="Practical goal the workspace should support.")
    parser.add_argument("--language", help="Default language for generated project text.")
    parser.add_argument(
        "--domain-pack",
        help="Optional domain pack name or path. Example: llm-research or /path/to/domain-pack.",
    )
    parser.add_argument(
        "--profile",
        help="Optional YAML or JSON setup profile produced by research-init.",
    )
    parser.add_argument(
        "--scope-root",
        help="Optional trusted root; when set, --profile and the effective target must resolve under it.",
    )
    parser.add_argument(
        "--starter-root",
        help="Reusable starter root. Defaults to the parent directory of this script.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without creating or modifying files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow creation inside an existing non-empty target and overwrite starter-managed files.",
    )
    return parser.parse_args(argv)


def default_starter_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_yaml(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Missing {label}: {path}")
    try:
        document = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"Invalid {label}: {path} must contain a mapping")
    return document


def load_profile(profile_path: Path | None) -> dict[str, Any]:
    if profile_path is None:
        return {}
    document = load_yaml(profile_path, "setup profile")
    if set(document) != {PROFILE_ROOT_KEY}:
        raise SystemExit("setup profile must contain only a workspace_init mapping at the document root")
    profile = document[PROFILE_ROOT_KEY]
    if not isinstance(profile, dict):
        raise SystemExit("setup profile workspace_init must be a mapping")
    validate_profile(profile)
    return profile


def profile_mapping(profile: dict[str, Any], key: str) -> dict[str, Any]:
    value = profile.get(key) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"setup profile {key} must be a mapping")
    return value


def string_value(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SystemExit(f"{label} must be a string")
    value = value.strip()
    return value or None


def require_profile_string(mapping: dict[str, Any], key: str, label: str) -> str:
    if key not in mapping:
        raise SystemExit(f"Missing required setup profile field: {label}")
    value = string_value(mapping.get(key), f"setup profile {label}")
    if value is None:
        raise SystemExit(f"Missing required setup profile field: {label}")
    return value


def require_profile_mapping(mapping: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    if key not in mapping:
        raise SystemExit(f"Missing required setup profile field: {label}")
    value = mapping[key]
    if not isinstance(value, dict):
        raise SystemExit(f"setup profile {label} must be a mapping")
    return value


def require_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SystemExit(f"setup profile {label} must be a non-empty list of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SystemExit(f"setup profile {label} must be a non-empty list of strings")
        normalized.append(item.strip())
    return normalized


def reject_unknown_keys(mapping: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(key for key in mapping if key not in allowed and not key.startswith("x-"))
    if unknown:
        raise SystemExit(f"{label} has unknown keys: {', '.join(unknown)}")


def canonical_workspace_relative_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{label} must be a non-empty workspace-relative path")
    value = value.strip()
    if "://" in value:
        raise SystemExit(f"{label} must be a workspace-relative path, not a URL: {value}")
    if "\x00" in value or value.startswith("~"):
        raise SystemExit(f"{label} contains an unsafe path value: {value}")
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if windows.drive or windows.root or windows.anchor or posix.is_absolute():
        raise SystemExit(f"{label} must be a workspace-relative path without drive, UNC, or absolute roots: {value}")
    if "\\" in value:
        raise SystemExit(f"{label} must use portable '/' separators: {value}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SystemExit(f"{label} must be a canonical workspace-relative path without empty, '.', or '..' parts: {value}")
    for part in parts:
        if part.endswith((" ", ".")) or ":" in part or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_PATH_NAMES:
            raise SystemExit(f"{label} contains a non-portable path component {part!r}: {value}")
    return "/".join(parts)


def validate_workspace_relative_path(value: str, label: str) -> None:
    canonical_workspace_relative_path(value, label)


def validate_string_path_list(values: list[str], label: str) -> None:
    for value in values:
        validate_workspace_relative_path(value, label)


def validate_source_roots(values: list[str], label: str) -> None:
    canonical: list[tuple[str, tuple[str, ...]]] = []
    portable_identities: dict[str, str] = {}
    for value in values:
        normalized = canonical_workspace_relative_path(value, label)
        portable_identity = normalized.casefold()
        prior = portable_identities.get(portable_identity)
        if prior is not None:
            raise SystemExit(f"{label} has duplicate or case-colliding roots: {prior!r} and {value!r}")
        portable_identities[portable_identity] = value
        canonical.append((value, tuple(normalized.split("/"))))
    for index, (left_value, left_parts) in enumerate(canonical):
        for right_value, right_parts in canonical[index + 1 :]:
            shorter, longer = sorted((left_parts, right_parts), key=len)
            if len(shorter) < len(longer) and longer[: len(shorter)] == shorter:
                raise SystemExit(f"{label} has overlapping roots that would scan evidence twice: {left_value!r} and {right_value!r}")


def validate_profile_config_sections(profile: dict[str, Any]) -> None:
    research_yml = profile.get("research_yml") or {}
    if research_yml and not isinstance(research_yml, dict):
        raise SystemExit("setup profile research_yml must be a mapping")

    for section in PROFILE_CONFIG_SECTIONS:
        if section in profile and not isinstance(profile[section], dict):
            raise SystemExit(f"setup profile {section} must be a mapping")
        if section in research_yml and not isinstance(research_yml[section], dict):
            raise SystemExit(f"setup profile research_yml.{section} must be a mapping")


def validate_profile_core_keys(profile: dict[str, Any]) -> None:
    project = profile_mapping(profile, "project")
    raw = profile_mapping(profile, "raw")
    ingest = profile_mapping(profile, "ingest")
    outputs = profile_mapping(profile, "outputs")
    reject_unknown_keys(project, PROFILE_PROJECT_FIELDS, "setup profile project")
    reject_unknown_keys(raw, PROFILE_RAW_FIELDS, "setup profile raw")
    reject_unknown_keys(ingest, PROFILE_INGEST_FIELDS, "setup profile ingest")
    reject_unknown_keys(outputs, PROFILE_OUTPUT_FIELDS, "setup profile outputs")
    if "immutable" in raw and raw["immutable"] is not True:
        raise SystemExit("setup profile raw.immutable must be true")
    formats = require_string_list(outputs.get("supported_formats"), "outputs.supported_formats")
    unknown_formats = sorted(set(formats) - set(OUTPUT_SUPPORTED_FORMATS))
    if unknown_formats:
        allowed = ", ".join(OUTPUT_SUPPORTED_FORMATS)
        raise SystemExit(
            f"setup profile outputs.supported_formats has unknown format(s): {', '.join(unknown_formats)}. "
            f"Allowed formats: {allowed}"
        )


def domain_pack_from_mapping(value: dict[str, Any]) -> str | None:
    enabled = value.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise SystemExit("setup profile domain_pack.enabled must be a boolean")
    name = string_value(value.get("name"), "setup profile domain_pack.name")
    path = string_value(value.get("path"), "setup profile domain_pack.path")
    if enabled is False:
        if name or path:
            raise SystemExit("setup profile domain_pack cannot disable domain packs and select a name or path")
        return None
    if name and path:
        raise SystemExit("setup profile domain_pack must choose either name or path, not both")
    if name:
        return name
    if path:
        return path
    if enabled is True:
        raise SystemExit("setup profile domain_pack enabled true requires name or path")
    return None


def validate_domain_decision(profile: dict[str, Any]) -> None:
    has_domain_guidance = "domain_guidance" in profile
    has_domain_pack = "domain_pack" in profile
    if not has_domain_guidance and not has_domain_pack:
        raise SystemExit("setup profile must include domain_guidance or domain_pack")

    selected_pack = domain_pack_from_profile(profile)
    if has_domain_guidance:
        guidance = require_profile_mapping(profile, "domain_guidance", "domain_guidance")
        mode = require_profile_string(guidance, "mode", "domain_guidance.mode")
        if mode not in PROFILE_DOMAIN_GUIDANCE_MODES:
            allowed = ", ".join(PROFILE_DOMAIN_GUIDANCE_MODES)
            raise SystemExit(f"setup profile domain_guidance.mode must be one of: {allowed}")
        require_profile_string(guidance, "rationale", "domain_guidance.rationale")
        if mode == "domain_pack" and selected_pack is None:
            raise SystemExit("setup profile domain_guidance.mode domain_pack requires domain_pack name or path")
        if mode != "domain_pack" and selected_pack is not None:
            raise SystemExit("setup profile domain_guidance and domain_pack selection disagree")
        validate_project_local_guidance(guidance, mode)
    elif selected_pack is None:
        raise SystemExit("setup profile domain_pack must select a pack or explicitly set enabled: false")


def validate_project_local_guidance(guidance: dict[str, Any], mode: str) -> None:
    unknown = sorted(set(guidance) - PROJECT_LOCAL_GUIDANCE_FIELDS)
    if unknown:
        raise SystemExit(f"setup profile domain_guidance has unknown keys: {', '.join(unknown)}")

    local_fields = (
        set(PROJECT_LOCAL_GUIDANCE_LIST_FIELDS)
        | set(PROJECT_LOCAL_GUIDANCE_OPTIONAL_LIST_FIELDS)
        | {"path", "scope"}
    )
    if mode != "project_local" and any(field in guidance for field in local_fields):
        raise SystemExit("setup profile domain_guidance local fields require mode: project_local")

    if "path" in guidance:
        path = string_value(guidance.get("path"), "setup profile domain_guidance.path")
        if path is None:
            raise SystemExit("setup profile domain_guidance.path must be a non-empty string")
        validate_workspace_relative_path(path, "setup profile domain_guidance.path")
    if "scope" in guidance:
        require_profile_string(guidance, "scope", "domain_guidance.scope")

    if mode != "project_local":
        return

    for field in PROJECT_LOCAL_GUIDANCE_LIST_FIELDS:
        require_string_list(guidance.get(field), f"domain_guidance.{field}")
    for field in PROJECT_LOCAL_GUIDANCE_OPTIONAL_LIST_FIELDS:
        if field in guidance:
            require_string_list(guidance.get(field), f"domain_guidance.{field}")


def validate_git_integration(profile: dict[str, Any]) -> None:
    integrations = require_profile_mapping(profile, "integrations", "integrations")
    git = require_profile_mapping(integrations, "git", "integrations.git")
    snapshot_policy = require_profile_string(git, "snapshot_user_edits", "integrations.git.snapshot_user_edits")
    if snapshot_policy != "explicit":
        raise SystemExit("setup profile integrations.git.snapshot_user_edits must be explicit")
    for key in FORBIDDEN_GIT_AUTOMATION_KEYS:
        if git.get(key):
            raise SystemExit(f"setup profile integrations.git.{key} is not allowed during initialization")
    validate_codebase_analysis_integration(integrations, "setup profile integrations.codebase_analysis")
    validate_acquisition_integration(integrations, "setup profile integrations.acquisition")


def validate_generated_sources_path(value: str, label: str) -> None:
    validate_workspace_relative_path(value, label)
    parts = Path(value).parts
    if len(parts) < 2 or parts[0] != "sources":
        raise SystemExit(f"{label} must be under the generated sources/ directory: {value}")


def validate_raw_target_path(value: str, label: str) -> None:
    validate_workspace_relative_path(value, label)
    parts = Path(value).parts
    if len(parts) < 2 or parts[0] != "raw":
        raise SystemExit(f"{label} must be under the raw/ evidence directory: {value}")


def validate_provider_list(value: Any, label: str, *, require_non_empty: bool = False) -> list[str]:
    if value is None:
        providers: list[str] = []
    elif isinstance(value, list):
        providers = []
        for item in value:
            provider = string_value(item, f"{label}[]")
            if provider is None:
                raise SystemExit(f"{label} must be a list of non-empty provider identifiers")
            providers.append(provider)
    else:
        raise SystemExit(f"{label} must be a list of provider identifiers")
    if require_non_empty and not providers:
        raise SystemExit(f"{label} must include at least one provider when acquisition is enabled")
    duplicates = sorted({provider for provider in providers if providers.count(provider) > 1})
    if duplicates:
        raise SystemExit(f"{label} has duplicate provider(s): {', '.join(duplicates)}")
    unknown = sorted(set(providers) - set(ACQUISITION_ALLOWED_PROVIDERS))
    if unknown:
        allowed = ", ".join(ACQUISITION_ALLOWED_PROVIDERS)
        raise SystemExit(f"{label} has unknown provider(s): {', '.join(unknown)}. Allowed providers: {allowed}")
    return providers


def validate_command_value(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            return
        raise SystemExit(f"{label} must be null, a non-empty string, or a non-empty list of strings")
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return
    raise SystemExit(f"{label} must be null, a non-empty string, or a non-empty list of strings")


def validate_codebase_analysis_integration(integrations: dict[str, Any], label: str) -> None:
    codebase = integrations.get("codebase_analysis")
    if codebase is None:
        return
    if not isinstance(codebase, dict):
        raise SystemExit(f"{label} must be a mapping")
    enabled = codebase.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SystemExit(f"{label}.enabled must be a boolean")
    provider = string_value(codebase.get("provider"), f"{label}.provider")
    if enabled and (provider is None or provider == "none"):
        raise SystemExit(f"{label}.provider must name an adapter when enabled")
    output_dir = string_value(codebase.get("output_dir"), f"{label}.output_dir") or CODEBASE_ANALYSIS_DEFAULT_OUTPUT_DIR
    validate_generated_sources_path(output_dir, f"{label}.output_dir")
    validate_command_value(codebase.get("command"), f"{label}.command")
    if "read_only" in codebase and codebase.get("read_only") is not True:
        raise SystemExit(f"{label}.read_only must be true during initialization")
    for key in FORBIDDEN_CODEBASE_AUTOMATION_KEYS:
        if codebase.get(key):
            raise SystemExit(f"{label}.{key} is not allowed during initialization")


def validate_acquisition_integration(integrations: dict[str, Any], label: str) -> None:
    acquisition = integrations.get("acquisition")
    if acquisition is None:
        return
    if not isinstance(acquisition, dict):
        raise SystemExit(f"{label} must be a mapping")
    enabled = acquisition.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SystemExit(f"{label}.enabled must be a boolean")
    validate_provider_list(acquisition.get("providers", []), f"{label}.providers", require_non_empty=enabled)
    target_root = string_value(acquisition.get("target_root"), f"{label}.target_root") or ACQUISITION_DEFAULT_TARGET_ROOT
    validate_raw_target_path(target_root, f"{label}.target_root")
    max_downloads = acquisition.get("max_downloads_per_run", 10)
    if not isinstance(max_downloads, int) or isinstance(max_downloads, bool) or max_downloads < 1:
        raise SystemExit(f"{label}.max_downloads_per_run must be a positive integer")
    require_license_check = acquisition.get("require_license_check", True)
    if not isinstance(require_license_check, bool):
        raise SystemExit(f"{label}.require_license_check must be a boolean")
    for key in FORBIDDEN_ACQUISITION_AUTOMATION_KEYS:
        if acquisition.get(key):
            raise SystemExit(f"{label}.{key} is not allowed during initialization")


def validate_handoff(profile: dict[str, Any]) -> None:
    if "handoff" not in profile:
        return
    handoff = profile["handoff"]
    if not isinstance(handoff, dict):
        raise SystemExit("setup profile handoff must be a mapping")
    unknown = sorted(set(handoff) - set(HANDOFF_FIELDS))
    if unknown:
        allowed = ", ".join(HANDOFF_FIELDS)
        raise SystemExit(
            f"setup profile handoff has unknown keys: {', '.join(unknown)}. Allowed keys: {allowed}"
        )
    if not handoff:
        allowed = ", ".join(HANDOFF_FIELDS)
        raise SystemExit(f"setup profile handoff must include at least one of: {allowed}")
    for field in HANDOFF_FIELDS:
        if field in handoff:
            value = string_value(handoff.get(field), f"setup profile handoff.{field}")
            if value is None:
                raise SystemExit(f"setup profile handoff.{field} must be a non-empty string")


def profile_handoff(profile: dict[str, Any]) -> dict[str, str]:
    handoff = profile.get("handoff")
    if not isinstance(handoff, dict):
        return {}
    normalized: dict[str, str] = {}
    for field in HANDOFF_FIELDS:
        value = handoff.get(field)
        if isinstance(value, str) and value.strip():
            normalized[field] = value.strip()
    return normalized


def validate_optional_string_list(profile: dict[str, Any], key: str) -> None:
    if key in profile:
        require_string_list(profile.get(key), key)


def validate_inferred_answers(profile: dict[str, Any]) -> None:
    if "inferred_answers" not in profile:
        return
    value = profile["inferred_answers"]
    if not isinstance(value, dict):
        raise SystemExit("setup profile inferred_answers must be a mapping")
    for key, answer in value.items():
        if not isinstance(key, str) or not key.strip():
            raise SystemExit("setup profile inferred_answers keys must be non-empty strings")
        if isinstance(answer, str):
            if not answer.strip():
                raise SystemExit(f"setup profile inferred_answers.{key} must not be empty")
            continue
        if isinstance(answer, list) and answer and all(isinstance(item, str) and item.strip() for item in answer):
            continue
        raise SystemExit(f"setup profile inferred_answers.{key} must be a string or non-empty list of strings")


def validate_init_report(profile: dict[str, Any]) -> None:
    init_report = profile.get("init_report") or {}
    if "init_report" in profile and not isinstance(profile["init_report"], dict):
        raise SystemExit("setup profile init_report must be a mapping")
    unknown = sorted(set(init_report) - INIT_REPORT_FIELDS)
    if unknown:
        raise SystemExit(f"setup profile init_report has unknown keys: {', '.join(unknown)}")
    if "path" in init_report:
        path = string_value(init_report.get("path"), "setup profile init_report.path")
        if path is None:
            raise SystemExit("setup profile init_report.path must be a non-empty string")
        validate_workspace_relative_path(path, "setup profile init_report.path")


def validate_validation_metadata(profile: dict[str, Any]) -> None:
    validation = profile.get("validation") or {}
    if "validation" in profile and not isinstance(profile["validation"], dict):
        raise SystemExit("setup profile validation must be a mapping")
    unknown = sorted(set(validation) - VALIDATION_FIELDS)
    if unknown:
        raise SystemExit(f"setup profile validation has unknown keys: {', '.join(unknown)}")
    if "commands" in validation:
        require_string_list(validation.get("commands"), "validation.commands")
    if "results" not in validation:
        return
    results = validation["results"]
    if not isinstance(results, list):
        raise SystemExit("setup profile validation.results must be a list of mappings")
    for index, result in enumerate(results, start=1):
        if not isinstance(result, dict):
            raise SystemExit("setup profile validation.results must be a list of mappings")
        unknown_result_keys = sorted(set(result) - VALIDATION_RESULT_FIELDS)
        if unknown_result_keys:
            raise SystemExit(
                f"setup profile validation.results[{index}] has unknown keys: {', '.join(unknown_result_keys)}"
            )
        for field in ("command", "status", "summary"):
            require_profile_string(result, field, f"validation.results[{index}].{field}")
        status = result["status"].strip()
        if status not in VALIDATION_STATUSES:
            allowed = ", ".join(VALIDATION_STATUSES)
            raise SystemExit(f"setup profile validation.results[{index}].status must be one of: {allowed}")


def validate_profile(profile: dict[str, Any]) -> None:
    unknown = sorted(set(profile) - PROFILE_ALLOWED_KEYS)
    if unknown:
        raise SystemExit(f"setup profile has unknown top-level keys: {', '.join(unknown)}")

    schema_version = require_profile_string(profile, "schema_version", "schema_version")
    if schema_version != PROFILE_SCHEMA_VERSION:
        raise SystemExit(f"setup profile schema_version must be {PROFILE_SCHEMA_VERSION}")
    require_profile_string(profile, "target_path", "target_path")

    project = require_profile_mapping(profile, "project", "project")
    for field in PROFILE_REQUIRED_PROJECT_FIELDS:
        require_profile_string(project, field, f"project.{field}")

    validate_domain_decision(profile)

    raw = require_profile_mapping(profile, "raw", "raw")
    source_roots = require_string_list(raw.get("source_roots"), "raw.source_roots")
    validate_source_roots(source_roots, "setup profile raw.source_roots")

    claim_strictness = require_profile_string(profile, "claim_strictness", "claim_strictness")
    if claim_strictness not in PROFILE_CLAIM_STRICTNESS_VALUES:
        allowed = ", ".join(PROFILE_CLAIM_STRICTNESS_VALUES)
        raise SystemExit(f"setup profile claim_strictness must be one of: {allowed}")
    ingest = require_profile_mapping(profile, "ingest", "ingest")
    if not isinstance(ingest.get("claim_extraction"), bool):
        raise SystemExit("setup profile ingest.claim_extraction must be a boolean")

    require_profile_mapping(profile, "outputs", "outputs")
    validate_git_integration(profile)
    validate_init_report(profile)
    validate_handoff(profile)
    validate_optional_string_list(profile, "initial_sources")
    if "existing_wiki_path" in profile:
        require_profile_string(profile, "existing_wiki_path", "existing_wiki_path")
    validate_optional_string_list(profile, "questions_asked")
    validate_inferred_answers(profile)
    validate_validation_metadata(profile)
    validate_optional_string_list(profile, "next_actions")
    require_string_list(profile.get("assumptions"), "assumptions")
    require_string_list(profile.get("skipped_decisions"), "skipped_decisions")
    validate_profile_config_sections(profile)
    validate_profile_core_keys(profile)
    normalize_seed_questions(profile)


def slug_from_path(path: Path) -> str:
    slug = path.name.lower().replace("_", "-")
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or "research-workspace"


def slugify_text(value: str) -> str:
    lowered = value.strip().lower()
    cleaned = []
    for char in lowered:
        if char.isalnum():
            cleaned.append(char)
        elif char in {" ", "-", "_", "/"}:
            cleaned.append("-")
    slug = "".join(cleaned)
    slug = "-".join(part for part in slug.split("-") if part)
    if len(slug) > 60:
        slug = slug[:60].rstrip("-")
    return slug or "question"


def normalize_question_items(
    raw: Any,
    *,
    allowed_keys: frozenset[str] = SEED_QUESTION_ITEM_KEYS,
    error_prefix: str = "setup profile",
    used_slugs: set[str] | None = None,
) -> list[dict[str, str]]:
    """Validate and normalize question items into page-creation dicts.

    Shared by profile-driven seeding and the batch question intake API
    (``intake_questions.py``). ``used_slugs`` lets callers reserve slugs that
    already exist in the workspace so new slugs never collide.
    """
    if not isinstance(raw, list):
        raise SystemExit(f"{error_prefix} questions must be a list")
    normalized: list[dict[str, str]] = []
    used = set(used_slugs) if used_slugs is not None else set()
    for index, item in enumerate(raw):
        label = f"questions[{index}]"
        if not isinstance(item, dict):
            raise SystemExit(f"{error_prefix} {label} must be a mapping")
        unknown = set(item) - allowed_keys
        if unknown:
            allowed = ", ".join(sorted(allowed_keys))
            raise SystemExit(
                f"{error_prefix} {label} has unsupported keys: {', '.join(sorted(unknown))}. "
                f"Allowed keys: {allowed}"
            )
        text = string_value(item.get("question"), f"{label}.question") or string_value(
            item.get("text"), f"{label}.text"
        )
        if text is None:
            raise SystemExit(f"{error_prefix} {label} must include a non-empty question")
        priority = string_value(item.get("priority"), f"{label}.priority") or QUESTION_DEFAULT_PRIORITY
        if priority not in QUESTION_PRIORITIES:
            allowed = ", ".join(QUESTION_PRIORITIES)
            raise SystemExit(f"{error_prefix} {label}.priority must be one of: {allowed}")
        origin = string_value(item.get("origin"), f"{label}.origin") or QUESTION_DEFAULT_ORIGIN
        base_slug = string_value(item.get("id"), f"{label}.id")
        slug = slugify_text(base_slug) if base_slug else slugify_text(text)
        candidate = slug
        suffix = 2
        while candidate in used:
            candidate = f"{slug}-{suffix}"
            suffix += 1
        used.add(candidate)
        normalized_item = {
            "slug": candidate,
            "question": text,
            "priority": priority,
            "origin": origin,
        }
        if "summary" in allowed_keys:
            summary = string_value(item.get("summary"), f"{label}.summary")
            if summary is not None:
                normalized_item["summary"] = summary
        if "context" in allowed_keys:
            context = item.get("context")
            if context is not None and not isinstance(context, str):
                raise SystemExit(f"{error_prefix} {label}.context must be a string")
            if isinstance(context, str) and context.strip():
                normalized_item["context"] = context.strip()
        normalized.append(normalized_item)
    return normalized


def normalize_seed_questions(profile: dict[str, Any]) -> list[dict[str, str]]:
    raw = profile.get("questions")
    if raw is None:
        return []
    return normalize_question_items(raw)


def domain_pack_from_profile(profile: dict[str, Any]) -> str | None:
    value = profile.get("domain_pack")
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        raise SystemExit("setup profile domain_pack must be a string or mapping")
    return domain_pack_from_mapping(value)


def resolve_scope_root(value: str | None) -> Path | None:
    if value is None:
        return None
    scope_root = Path(value).expanduser().resolve()
    if not scope_root.is_dir():
        raise SystemExit(f"--scope-root must be an existing directory: {scope_root}")
    return scope_root


def validate_path_under_scope(path: Path, scope_root: Path | None, label: str) -> None:
    if scope_root is None:
        return
    if not is_relative_to(path.resolve(), scope_root):
        raise SystemExit(f"{label} must be under --scope-root: {path} (scope root: {scope_root})")


def resolve_options(args: argparse.Namespace) -> InitOptions:
    starter_root = Path(args.starter_root).expanduser().resolve() if args.starter_root else default_starter_root()
    scope_root = resolve_scope_root(args.scope_root)
    profile_path = Path(args.profile).expanduser().resolve() if args.profile else None
    if profile_path is not None:
        validate_path_under_scope(profile_path, scope_root, "--profile")
    profile = load_profile(profile_path)
    project_profile = profile_mapping(profile, "project")

    target_value = string_value(args.target, "--target") or string_value(profile.get("target_path"), "target_path")
    if target_value is None:
        raise SystemExit("Missing target path. Provide --target or workspace_init.target_path in the profile.")
    target = Path(target_value).expanduser().resolve()
    validate_path_under_scope(target, scope_root, "target path")

    project_name = (
        string_value(args.project_name, "--project-name")
        or string_value(project_profile.get("name"), "project.name")
        or slug_from_path(target)
    )
    project_description = string_value(args.project_description, "--project-description") or string_value(
        project_profile.get("description"), "project.description"
    )
    if project_description is None:
        raise SystemExit("Missing project description. Provide --project-description or project.description in the profile.")
    owner_goal = (
        string_value(args.owner_goal, "--owner-goal")
        or string_value(project_profile.get("owner_goal"), "project.owner_goal")
        or project_description
    )
    language = string_value(args.language, "--language") or string_value(project_profile.get("language"), "project.language") or "en"
    domain_pack = string_value(args.domain_pack, "--domain-pack") or domain_pack_from_profile(profile)

    return InitOptions(
        starter_root=starter_root,
        target=target,
        scope_root=scope_root,
        project_name=project_name,
        project_description=project_description,
        owner_goal=owner_goal,
        language=language,
        domain_pack=domain_pack,
        profile_path=profile_path,
        profile=profile,
        dry_run=bool(args.dry_run),
        force=bool(args.force),
    )


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_starter_root(starter_root: Path) -> None:
    if not starter_root.is_dir():
        raise SystemExit(f"Starter root is not a directory: {starter_root}")
    missing = [name for name in REQUIRED_STARTER_FILES if not (starter_root / name).exists()]
    if missing:
        raise SystemExit(f"Starter root is missing required files: {', '.join(missing)}")


def validate_target(options: InitOptions) -> None:
    if options.target == options.starter_root or is_relative_to(options.target, options.starter_root):
        raise SystemExit("Refusing to initialize a workspace inside the reusable starter root.")
    if options.target.exists() and not options.target.is_dir():
        raise SystemExit(f"Target exists and is not a directory: {options.target}")
    if options.target.exists() and any(options.target.iterdir()) and not options.force:
        raise SystemExit(f"Refusing to overwrite non-empty target without --force: {options.target}")


def should_skip(relative_path: Path) -> bool:
    if any(part in EXCLUDED_NAMES for part in relative_path.parts):
        return True
    return relative_path.suffix in EXCLUDED_SUFFIXES


def path_entries_under_root(path: Path, root: Path) -> list[Path]:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise SystemExit(f"Refusing to write outside workspace: {path}") from exc
    entries = [root]
    current = root
    for part in relative.parts:
        current = current / part
        entries.append(current)
    return entries


def validate_private_path(path: Path, root: Path) -> None:
    root_resolved = root.resolve()
    for entry in path_entries_under_root(path, root):
        if (entry.exists() or entry.is_symlink()) and not is_contained_nonsymlink(entry, root_resolved):
            raise SystemExit(f"Refusing to write through symlink in workspace: {entry}")
    if not path.resolve().is_relative_to(root_resolved):
        raise SystemExit(f"Refusing to write outside workspace: {path}")


def apply_restrictive_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        if os.name == "posix":
            raise


def ensure_private_dir(path: Path, root: Path | None = None) -> None:
    if root is not None:
        validate_private_path(path, root)
    path.mkdir(parents=True, exist_ok=True, mode=RESTRICTIVE_DIR_MODE)
    entries = path_entries_under_root(path, root) if root is not None else [path]
    for entry in entries:
        if entry.is_dir():
            apply_restrictive_mode(entry, RESTRICTIVE_DIR_MODE)


def write_private_text(path: Path, text: str, root: Path) -> None:
    ensure_private_dir(path.parent, root)
    validate_private_path(path, root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, RESTRICTIVE_FILE_MODE)
    except OSError as exc:
        raise SystemExit(f"Cannot write private workspace file: {path}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    apply_restrictive_mode(path, RESTRICTIVE_FILE_MODE)


def copy_starter_tree(starter_root: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()
    for source in sorted(starter_root.rglob("*"), key=lambda path: path.as_posix()):
        relative = source.relative_to(starter_root)
        if should_skip(relative):
            continue
        destination = target / relative
        # Fail closed before touching the destination: refuse a planted symlink at
        # the destination itself, or any symlinked parent that redirects the
        # resolved path outside the workspace. This matters under `--force` into an
        # existing target, where writing through such a link would let init escape
        # the workspace. Directories are visited before their children (sorted by
        # posix path), so a symlinked ancestor is caught at its own entry before any
        # child write. is_contained_nonsymlink is the shared containment definition.
        if not is_contained_nonsymlink(destination, target_resolved):
            raise SystemExit(f"Refusing to write through symlink in workspace: {destination}")
        if source.is_dir():
            if destination.exists() and not destination.is_dir():
                raise SystemExit(f"Cannot create directory over existing file: {destination}")
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if source.is_file():
            if destination.exists() and destination.is_dir():
                raise SystemExit(f"Cannot copy file over existing directory: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # follow_symlinks=False keeps a (trusted) starter symlink from being
            # traversed into a copied file; the destination symlink guard above is
            # what prevents writing *through* a planted link.
            shutil.copy2(source, destination, follow_symlinks=False)


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def derive_run_release_budget_override(overrides: dict[str, Any]) -> None:
    run_config = overrides.get("run")
    if not isinstance(run_config, dict):
        return
    if "max_questions_per_run" not in run_config or "max_releases_per_run" in run_config:
        return
    max_questions = run_config.get("max_questions_per_run")
    if isinstance(max_questions, bool) or not isinstance(max_questions, int) or max_questions < 1:
        return
    run_config["max_releases_per_run"] = max_questions * DEFAULT_RELEASES_PER_QUESTION


def profile_config_overrides(profile: dict[str, Any]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    research_yml = profile.get("research_yml") or {}
    if research_yml and not isinstance(research_yml, dict):
        raise SystemExit("setup profile research_yml must be a mapping")

    for section in PROFILE_CONFIG_SECTIONS:
        if section in profile:
            value = profile[section]
            if not isinstance(value, dict):
                raise SystemExit(f"setup profile {section} must be a mapping")
            overrides[section] = copy.deepcopy(value)
        if section in research_yml:
            value = research_yml[section]
            if not isinstance(value, dict):
                raise SystemExit(f"setup profile research_yml.{section} must be a mapping")
            existing = overrides.get(section, {})
            overrides[section] = deep_merge(existing, value)
    derive_run_release_budget_override(overrides)
    return overrides


def validate_domain_pack_tree(source_path: Path) -> None:
    """Reject active, binary, special, or linked content before a pack is copied."""
    findings: list[str] = []
    root_name = source_path.name
    if (
        any(ord(character) < 32 or character in '<>:"|?*' for character in root_name)
        or root_name.endswith((" ", "."))
        or root_name.split(".", 1)[0].casefold() in WINDOWS_RESERVED_PACK_NAMES
    ):
        findings.append(f".: non-portable domain-pack root name {root_name!r}")
    portable_paths: dict[str, str] = {}
    for path in sorted(source_path.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source_path).as_posix()
        relative_parts = PurePosixPath(relative).parts
        portable_identity = "/".join(unicodedata.normalize("NFC", part).casefold() for part in relative_parts)
        previous = portable_paths.get(portable_identity)
        if previous is not None:
            findings.append(f"{relative}: portably collides with {previous}")
        else:
            portable_paths[portable_identity] = relative
        for part in relative_parts:
            if (
                any(ord(character) < 32 or character in '<>:"|?*' for character in part)
                or part.endswith((" ", "."))
                or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_PACK_NAMES
            ):
                findings.append(f"{relative}: non-portable path component {part!r}")
                break
        if path.is_symlink():
            findings.append(f"{relative}: symbolic links are not allowed")
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            findings.append(f"{relative}: special filesystem entries are not allowed")
            continue
        suffix = path.suffix.casefold()
        if suffix not in ALLOWED_PACK_FILE_SUFFIXES:
            findings.append(f"{relative}: executable or unsupported file type {suffix or '<none>'}")
            continue
        try:
            mode = path.stat().st_mode
            content = path.read_bytes()
        except OSError as exc:
            findings.append(f"{relative}: could not be read safely ({exc})")
            continue
        if mode & 0o111:
            findings.append(f"{relative}: executable permission bits are not allowed")
        if b"\x00" in content:
            findings.append(f"{relative}: binary content is not allowed")
        else:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                findings.append(f"{relative}: content must be UTF-8 text")
    if findings:
        raise SystemExit("Unsafe domain pack content: " + "; ".join(findings))


def validate_domain_pack_references(source_path: Path) -> None:
    """Require every declared, deployable pack path to exist inside the pack."""
    overlay = load_yaml(source_path / "research.overlay.yml", "domain pack overlay")
    domain_pack = overlay.get("domain_pack")
    if not isinstance(domain_pack, dict):
        raise SystemExit("domain pack overlay must declare a domain_pack mapping")

    references: list[tuple[str, Any]] = []
    for field in ("taxonomy_doc", "claims_doc"):
        if field in domain_pack:
            references.append((f"domain_pack.{field}", domain_pack[field]))
    for field in ("scaffolds", "coverage_templates"):
        values = domain_pack.get(field)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise SystemExit(f"domain_pack.{field} must be a mapping")
        references.extend((f"domain_pack.{field}.{key}", value) for key, value in values.items())
    implemented = domain_pack.get("implemented_files")
    if implemented is not None:
        if not isinstance(implemented, list):
            raise SystemExit("domain_pack.implemented_files must be a list")
        references.extend((f"domain_pack.implemented_files[{index}]", value) for index, value in enumerate(implemented))

    missing: list[str] = []
    for label, value in references:
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"{label} must be a non-empty pack-relative path")
        relative = canonical_workspace_relative_path(value.strip(), label)
        referenced = source_path.joinpath(*PurePosixPath(relative).parts)
        if not referenced.is_file() or referenced.is_symlink():
            missing.append(relative)
    if missing:
        raise SystemExit(f"Domain pack is missing declared file(s): {', '.join(sorted(set(missing)))}")


def resolve_domain_pack(selection: str | None, starter_root: Path) -> DomainPackSelection | None:
    if selection is None:
        return None
    candidate = Path(selection).expanduser()
    if candidate.exists():
        if candidate.is_symlink():
            raise SystemExit(f"Unsafe domain pack content: .: symbolic-link domain-pack roots are not allowed: {candidate}")
        source_path = candidate.resolve()
    else:
        source_path = (starter_root.parent / "domain-packs" / selection).resolve()
    if not source_path.is_dir():
        raise SystemExit(f"Domain pack not found: {selection}")
    validate_domain_pack_tree(source_path)
    overlay_path = source_path / "research.overlay.yml"
    if not overlay_path.exists():
        raise SystemExit(f"Domain pack is missing research.overlay.yml: {source_path}")
    validate_domain_pack_references(source_path)
    return DomainPackSelection(name=source_path.name, source_path=source_path, target_relative=f"domain-packs/{source_path.name}")


def prefix_domain_pack_path(value: Any, pack_relative: str) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    if "://" in value or value.startswith("/") or value.startswith(f"{pack_relative}/"):
        return value
    return f"{pack_relative}/{value}"


def normalize_domain_pack_paths(config: dict[str, Any], pack_relative: str) -> dict[str, Any]:
    domain_pack = config.get("domain_pack")
    if not isinstance(domain_pack, dict):
        return config
    for field in ("taxonomy_doc", "claims_doc"):
        if field in domain_pack:
            domain_pack[field] = prefix_domain_pack_path(domain_pack[field], pack_relative)
    scaffolds = domain_pack.get("scaffolds")
    if isinstance(scaffolds, dict):
        for key, value in list(scaffolds.items()):
            scaffolds[key] = prefix_domain_pack_path(value, pack_relative)
    coverage_templates = domain_pack.get("coverage_templates")
    if isinstance(coverage_templates, dict):
        for key, value in list(coverage_templates.items()):
            coverage_templates[key] = prefix_domain_pack_path(value, pack_relative)
    for field in ("implemented_files", "planned_files"):
        values = domain_pack.get(field)
        if isinstance(values, list):
            domain_pack[field] = [prefix_domain_pack_path(value, pack_relative) for value in values]
    return config


def build_config(options: InitOptions, domain_pack: DomainPackSelection | None) -> dict[str, Any]:
    config = load_yaml(options.starter_root / "research.yml", "starter research.yml")
    if domain_pack is not None:
        overlay = load_yaml(domain_pack.source_path / "research.overlay.yml", "domain pack overlay")
        config = deep_merge(config, overlay)
    config = deep_merge(config, profile_config_overrides(options.profile))

    project_config = config.get("project") or {}
    if not isinstance(project_config, dict):
        raise SystemExit("research.yml project must be a mapping")
    project_config = deep_merge(
        project_config,
        {
            "name": options.project_name,
            "description": options.project_description,
            "owner_goal": options.owner_goal,
            "language": options.language,
        },
    )
    handoff = profile_handoff(options.profile)
    if handoff:
        project_config["handoff"] = handoff
        secret = handoff_secret(options.target)
        if secret is not None:
            project_config["handoff_signature"] = sign_handoff(handoff, secret)
    config["project"] = project_config
    if domain_pack is not None:
        config = normalize_domain_pack_paths(config, domain_pack.target_relative)
    validate_config_paths(config)
    return config


def config_mapping(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"research.yml {key} must be a mapping")
    return value


def config_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"research.yml {label} must be a list of strings")
    return value


def validate_config_paths(config: dict[str, Any]) -> None:
    raw_config = config_mapping(config, "raw")
    sources_config = config_mapping(config, "sources")
    wiki_config = config_mapping(config, "wiki")
    outputs_config = config_mapping(config, "outputs")
    integrations_config = config_mapping(config, "integrations")
    run_config = config_mapping(config, "run")
    for field in RUN_BUDGET_FIELDS:
        value = run_config.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SystemExit(f"research.yml run.{field} must be a positive integer")

    validate_source_roots(
        config_list(raw_config.get("source_roots"), "raw.source_roots"),
        "research.yml raw.source_roots",
    )
    lifecycle_statuses = config_list(sources_config.get("lifecycle_statuses"), "sources.lifecycle_statuses")
    unknown_statuses = sorted(set(lifecycle_statuses) - set(SOURCE_LIFECYCLE_STATUSES))
    if unknown_statuses:
        allowed = ", ".join(SOURCE_LIFECYCLE_STATUSES)
        raise SystemExit(
            f"research.yml sources.lifecycle_statuses has unknown status(es): {', '.join(unknown_statuses)}. "
            f"Allowed statuses: {allowed}"
        )
    default_status = sources_config.get("default_status")
    if default_status is not None and default_status not in lifecycle_statuses:
        raise SystemExit("research.yml sources.default_status must be listed in sources.lifecycle_statuses")
    for key in ("manifest_path", "normalized_dir", "cards_dir"):
        value = sources_config.get(key)
        if isinstance(value, str) and value.strip():
            validate_workspace_relative_path(value, f"research.yml sources.{key}")
    for key in ("manifest_path", "source_requests_path"):
        value = sources_config.get(key)
        if isinstance(value, str) and value.strip() and not value.lower().endswith(".jsonl"):
            raise SystemExit(f"research.yml sources.{key} must use the .jsonl extension: {value}")
    wiki_root_value = wiki_config.get("root") or "wiki"
    if not isinstance(wiki_root_value, str):
        raise SystemExit("research.yml wiki.root must be a string")
    validate_workspace_relative_path(wiki_root_value, "research.yml wiki.root")
    validate_string_path_list(
        config_list(wiki_config.get("required_dirs"), "wiki.required_dirs"),
        "research.yml wiki.required_dirs",
    )
    default_output_dir = outputs_config.get("default_dir")
    if isinstance(default_output_dir, str) and default_output_dir.strip():
        validate_workspace_relative_path(default_output_dir, "research.yml outputs.default_dir")
    supported_formats = config_list(outputs_config.get("supported_formats"), "outputs.supported_formats")
    unknown_formats = sorted(set(supported_formats) - set(OUTPUT_SUPPORTED_FORMATS))
    if unknown_formats:
        allowed = ", ".join(OUTPUT_SUPPORTED_FORMATS)
        raise SystemExit(
            f"research.yml outputs.supported_formats has unknown format(s): {', '.join(unknown_formats)}. "
            f"Allowed formats: {allowed}"
        )
    validate_codebase_analysis_integration(integrations_config, "research.yml integrations.codebase_analysis")
    validate_acquisition_integration(integrations_config, "research.yml integrations.acquisition")


def domain_guidance_config(profile: dict[str, Any]) -> dict[str, Any]:
    guidance = profile.get("domain_guidance") or {}
    if not isinstance(guidance, dict):
        return {}
    return guidance


def domain_guidance_mode(profile: dict[str, Any]) -> str | None:
    guidance = domain_guidance_config(profile)
    value = guidance.get("mode")
    return value if isinstance(value, str) else None


def project_domain_guidance_path(profile: dict[str, Any]) -> str | None:
    if domain_guidance_mode(profile) != "project_local":
        return None
    guidance = domain_guidance_config(profile)
    return string_value(guidance.get("path"), "setup profile domain_guidance.path") or PROJECT_LOCAL_GUIDANCE_DEFAULT_PATH


def guidance_list_items(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [item.strip() for item in values if isinstance(item, str) and item.strip()]


def append_guidance_section(lines: list[str], heading: str, items: list[str]) -> None:
    lines.extend([f"## {heading}", ""])
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("- None recorded.")
    lines.append("")


def render_project_domain_guidance(config: dict[str, Any], options: InitOptions) -> str:
    guidance = domain_guidance_config(options.profile)
    project = config_mapping(config, "project")
    scope = string_value(guidance.get("scope"), "setup profile domain_guidance.scope") or str(
        project.get("description") or "Project-specific research scope."
    )
    rationale = string_value(guidance.get("rationale"), "setup profile domain_guidance.rationale") or "No reusable domain pack matched."
    path = project_domain_guidance_path(options.profile) or PROJECT_LOCAL_GUIDANCE_DEFAULT_PATH

    lines = [
        f"# {project.get('name', 'Project')} Domain Guidance",
        "",
        "Project-local guidance for agents working in this research workspace.",
        "Keep this document local unless the user explicitly asks to promote it",
        "to a reusable domain pack.",
        "",
        "## Scope",
        "",
        scope,
        "",
        "## Domain Decision",
        "",
        "- Mode: `project_local`.",
        f"- Rationale: {rationale}",
        f"- Generated path: `{path}`.",
        "",
    ]
    append_guidance_section(lines, "Source Priorities", guidance_list_items(guidance.get("source_priorities")))
    append_guidance_section(lines, "Extraction Targets", guidance_list_items(guidance.get("extraction_targets")))
    append_guidance_section(lines, "Claim Types", guidance_list_items(guidance.get("claim_types")))
    append_guidance_section(lines, "Filing Rules", guidance_list_items(guidance.get("filing_rules")))
    append_guidance_section(lines, "Output Scaffolds", guidance_list_items(guidance.get("output_scaffolds")))
    append_guidance_section(lines, "Promotion Notes", guidance_list_items(guidance.get("promotion_notes")))
    append_guidance_section(lines, "Assumptions", guidance_list_items(options.profile.get("assumptions")))
    append_guidance_section(lines, "Skipped Decisions", guidance_list_items(options.profile.get("skipped_decisions")))
    lines.extend(
        [
            "## Guardrails",
            "",
            "- Do not copy pilot data or prototype assumptions into reusable starter files.",
            "- Do not mutate raw source evidence while applying this guidance.",
            "- Do not create a reusable domain pack unless the user requests one.",
            "- Revisit this guidance after the first source cycle and promote only repeated, reusable patterns.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_project_domain_guidance(target: Path, config: dict[str, Any], options: InitOptions) -> None:
    relative = project_domain_guidance_path(options.profile)
    if relative is None:
        return
    path = target / relative
    write_private_text(path, render_project_domain_guidance(config, options), target)


def init_report_path(profile: dict[str, Any]) -> str | None:
    if not profile:
        return None
    init_report = profile.get("init_report") or {}
    if not isinstance(init_report, dict):
        return INIT_REPORT_DEFAULT_PATH
    return string_value(init_report.get("path"), "setup profile init_report.path") or INIT_REPORT_DEFAULT_PATH


def scalar_text(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten_mapping(mapping: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for key, value in sorted(mapping.items(), key=lambda item: str(item[0])):
        label = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            items.extend(flatten_mapping(value, label))
        elif isinstance(value, list):
            items.append((label, ", ".join(scalar_text(item) for item in value) or "none"))
        else:
            items.append((label, scalar_text(value)))
    return items


def append_report_list_section(lines: list[str], heading: str, items: list[str]) -> None:
    lines.extend([f"## {heading}", ""])
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append("- None recorded.")
    lines.append("")


def append_report_mapping_section(lines: list[str], heading: str, mapping: dict[str, Any]) -> None:
    lines.extend([f"## {heading}", ""])
    if not mapping:
        lines.extend(["- None recorded.", ""])
        return
    for key, value in flatten_mapping(mapping):
        lines.append(f"- {key}: {value}")
    lines.append("")


def validation_config(profile: dict[str, Any]) -> dict[str, Any]:
    validation = profile.get("validation") or {}
    return validation if isinstance(validation, dict) else {}


def render_validation_section(lines: list[str], profile: dict[str, Any]) -> None:
    validation = validation_config(profile)
    commands = guidance_list_items(validation.get("commands")) or list(DEFAULT_VALIDATION_COMMANDS)
    append_report_list_section(lines, "Validation Commands", commands)

    lines.extend(["## Validation Results", ""])
    results = validation.get("results") if isinstance(validation.get("results"), list) else []
    if not results:
        lines.extend(["- Pending. Run the validation commands and update this report with results.", ""])
        return
    for result in results:
        if not isinstance(result, dict):
            continue
        command = string_value(result.get("command"), "validation command") or "unknown command"
        status = string_value(result.get("status"), "validation status") or "pending"
        summary = string_value(result.get("summary"), "validation summary") or "No summary recorded."
        lines.append(f"- `{command}`: `{status}` - {summary}")
    lines.append("")


def recommended_acquisition_from_config(config: dict[str, Any]) -> list[str]:
    domain_pack = config.get("domain_pack")
    if not isinstance(domain_pack, dict):
        return []
    values = domain_pack.get("recommended_acquisition")
    if not isinstance(values, list):
        return []
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def render_domain_report_lines(
    profile: dict[str, Any],
    config: dict[str, Any],
    domain_pack: DomainPackSelection | None,
) -> list[str]:
    guidance = domain_guidance_config(profile)
    mode = string_value(guidance.get("mode"), "domain_guidance.mode") or "none"
    rationale = string_value(guidance.get("rationale"), "domain_guidance.rationale") or "None recorded."
    lines = [f"- Mode: `{mode}`.", f"- Rationale: {rationale}"]
    if domain_pack is not None:
        lines.append(f"- Reusable domain pack: `{domain_pack.name}`.")
    recommended_acquisition = recommended_acquisition_from_config(config)
    if recommended_acquisition:
        lines.append(f"- Recommended acquisition providers: {', '.join(recommended_acquisition)}.")
        lines.append("- Acquisition remains disabled unless integrations.acquisition.enabled is explicitly true.")
    local_guidance = project_domain_guidance_path(profile)
    if local_guidance is not None:
        lines.append(f"- Project-local guidance: `{local_guidance}`.")
    return lines


def render_init_report(config: dict[str, Any], options: InitOptions, domain_pack: DomainPackSelection | None) -> str:
    project = config_mapping(config, "project")
    raw_config = config_mapping(config, "raw")
    outputs_config = config_mapping(config, "outputs")
    ingest_config = config_mapping(config, "ingest")
    integrations_config = config_mapping(config, "integrations")
    report_path = init_report_path(options.profile) or INIT_REPORT_DEFAULT_PATH
    profile_label = options.profile_path.as_posix() if options.profile_path is not None else "none"
    initial_sources = guidance_list_items(options.profile.get("initial_sources"))
    existing_wiki = string_value(options.profile.get("existing_wiki_path"), "existing_wiki_path")

    lines = [
        f"# {project.get('name', 'Research Workspace')} Init Report",
        "",
        "Generated setup report for agent-assisted research workspace initialization.",
        "",
        "## Report Metadata",
        "",
        f"- Report path: `{report_path}`.",
        f"- Setup profile: `{profile_label}`.",
        f"- Generated: {datetime.now(timezone.utc).date().isoformat()}.",
        "",
        "## Project Identity",
        "",
        f"- Name: `{project.get('name')}`.",
        f"- Description: {project.get('description')}.",
        f"- Owner goal: {project.get('owner_goal')}.",
        f"- Language: `{project.get('language')}`.",
        "",
    ]
    append_report_list_section(lines, "Questions Asked", guidance_list_items(options.profile.get("questions_asked")))
    append_report_mapping_section(lines, "Inferred Answers", options.profile.get("inferred_answers") or {})
    lines.extend(["## Domain Guidance Decision", ""])
    lines.extend(render_domain_report_lines(options.profile, config, domain_pack))
    lines.append("")
    append_report_list_section(lines, "Source Roots", config_list(raw_config.get("source_roots"), "raw.source_roots"))
    append_report_list_section(lines, "Supplied Sources", initial_sources)
    lines.extend(["## Existing Wiki", "", f"- {existing_wiki or 'None recorded.'}", ""])
    lines.extend(
        [
            "## Output Types And Claim Strictness",
            "",
            f"- Supported formats: {', '.join(config_list(outputs_config.get('supported_formats'), 'outputs.supported_formats'))}.",
            f"- Claim strictness: `{options.profile.get('claim_strictness')}`.",
            f"- Claim extraction: `{scalar_text(ingest_config.get('claim_extraction'))}`.",
            "",
        ]
    )
    append_report_mapping_section(lines, "Integrations", integrations_config)
    render_validation_section(lines, options.profile)
    append_report_list_section(lines, "Assumptions", guidance_list_items(options.profile.get("assumptions")))
    append_report_list_section(lines, "Skipped Decisions", guidance_list_items(options.profile.get("skipped_decisions")))
    append_report_list_section(lines, "Next Actions", guidance_list_items(options.profile.get("next_actions")))
    return "\n".join(lines).rstrip() + "\n"


def write_init_report(target: Path, config: dict[str, Any], options: InitOptions, domain_pack: DomainPackSelection | None) -> None:
    relative = init_report_path(options.profile)
    if relative is None:
        return
    path = target / relative
    write_private_text(path, render_init_report(config, options, domain_pack), target)


def write_yaml(path: Path, value: dict[str, Any], root: Path) -> None:
    write_private_text(path, yaml.safe_dump(value, sort_keys=False, allow_unicode=False), root)


def ensure_configured_directories(target: Path, config: dict[str, Any]) -> None:
    raw_config = config_mapping(config, "raw")
    sources_config = config_mapping(config, "sources")
    wiki_config = config_mapping(config, "wiki")
    outputs_config = config_mapping(config, "outputs")
    integrations_config = config_mapping(config, "integrations")

    for relative in config_list(raw_config.get("source_roots"), "raw.source_roots"):
        ensure_private_dir(target / relative, target)
    for key in ("normalized_dir", "cards_dir"):
        value = sources_config.get(key)
        if isinstance(value, str) and value.strip():
            ensure_private_dir(target / value, target)
    wiki_root_value = wiki_config.get("root") or "wiki"
    if not isinstance(wiki_root_value, str):
        raise SystemExit("research.yml wiki.root must be a string")
    wiki_root = target / wiki_root_value
    ensure_private_dir(wiki_root, target)
    for subdir in config_list(wiki_config.get("required_dirs"), "wiki.required_dirs"):
        ensure_private_dir(wiki_root / subdir, target)
    default_output_dir = outputs_config.get("default_dir")
    if isinstance(default_output_dir, str) and default_output_dir.strip():
        ensure_private_dir(target / default_output_dir, target)
    codebase = integrations_config.get("codebase_analysis")
    if isinstance(codebase, dict) and codebase.get("enabled") is True:
        output_dir = string_value(codebase.get("output_dir"), "research.yml integrations.codebase_analysis.output_dir")
        ensure_private_dir(target / (output_dir or CODEBASE_ANALYSIS_DEFAULT_OUTPUT_DIR), target)
    acquisition = integrations_config.get("acquisition")
    if isinstance(acquisition, dict) and acquisition.get("enabled") is True:
        target_root = string_value(acquisition.get("target_root"), "research.yml integrations.acquisition.target_root")
        ensure_private_dir(target / (target_root or ACQUISITION_DEFAULT_TARGET_ROOT), target)


def titleize_directory(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", "-").split("-") if part)


def render_index(config: dict[str, Any], seed_questions: list[dict[str, str]] | None = None) -> str:
    project = config_mapping(config, "project")
    wiki_config = config_mapping(config, "wiki")
    domain_pack = config.get("domain_pack") if isinstance(config.get("domain_pack"), dict) else {}
    page_taxonomy = domain_pack.get("page_taxonomy") if isinstance(domain_pack, dict) else {}
    if not isinstance(page_taxonomy, dict):
        page_taxonomy = {}
    seed_questions = seed_questions or []
    wiki_root_value = wiki_config.get("root") if isinstance(wiki_config.get("root"), str) else "wiki"
    updated = datetime.now(timezone.utc).date().isoformat()

    lines = [
        f"# {project.get('name', 'Research Workspace')} Index",
        "",
        "Static catalog for maintained wiki pages.",
        "",
        "Update this file when adding or changing wiki pages. Scripts and agents",
        "should derive index sections from research.yml instead of hardcoding wiki",
        "directory names.",
        "",
    ]
    for subdir in config_list(wiki_config.get("required_dirs"), "wiki.required_dirs"):
        rows: list[str] = []
        if subdir == QUESTION_WIKI_DIR and seed_questions:
            for question in seed_questions:
                page_rel = f"{wiki_root_value}/{QUESTION_WIKI_DIR}/{question['slug']}.md"
                summary = escape_table_cell(question["question"])
                rows.append(f"| [{page_rel}]({page_rel}) | {summary} | {updated} | |")
        if not rows:
            rows.append("| (none yet) | | | |")
        lines.extend(
            [
                f"## {titleize_directory(subdir)}",
                "",
                str(page_taxonomy.get(subdir) or DEFAULT_SECTION_DESCRIPTIONS.get(subdir) or "Maintained wiki pages."),
                "",
                "| Page | Summary | Updated | Source IDs |",
                "|------|---------|---------|------------|",
                *rows,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def escape_table_cell(value: str) -> str:
    return escape_markdown_inline(value)


def escape_markdown_inline(value: str) -> str:
    collapsed = " ".join(value.split())
    return escape_leading_heading_marker(escape_markdown_delimiters(collapsed))


def escape_markdown_body(value: str) -> str:
    escaped = escape_markdown_delimiters(value)
    return "\n".join(escape_leading_heading_marker(line) for line in escaped.splitlines())


def escape_markdown_delimiters(value: str) -> str:
    escaped = value.replace("<", "&lt;").replace(">", "&gt;")
    return "".join(f"\\{char}" if char in "[]()|" else char for char in escaped)


def escape_leading_heading_marker(value: str) -> str:
    stripped = value.lstrip(" \t")
    if not stripped.startswith("#"):
        return value
    offset = len(value) - len(stripped)
    return f"{value[:offset]}\\{value[offset:]}"


def markdown_fence_for(value: str) -> str:
    runs = re.findall(r"`+", value)
    longest = max((len(run) for run in runs), default=0)
    return "`" * max(3, longest + 1)


def render_untrusted_evidence_block(label: str, value: str) -> str:
    escaped = escape_markdown_body(value)
    fence = markdown_fence_for(escaped)
    return "\n".join(
        [
            f"=== BEGIN UNTRUSTED EVIDENCE: {label} ===",
            "",
            f"{fence}text",
            escaped,
            fence,
            "",
            f"=== END UNTRUSTED EVIDENCE: {label} ===",
        ]
    )


def render_question_page(question: dict[str, str]) -> str:
    timestamp = datetime.now(timezone.utc).date().isoformat()
    text = question["question"]
    summary = question.get("summary") or text
    visible_title = escape_markdown_inline(text)
    frontmatter = {
        "type": "question",
        "created": timestamp,
        "updated": timestamp,
        "status": "open",
        "priority": question["priority"],
        "origin": question["origin"],
        "source_ids": [],
        "summary": escape_table_cell(summary),
        "question": text,
    }
    rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    intro = question.get("intro") or QUESTION_PAGE_DEFAULT_INTRO
    context = question.get("context")
    summary_block = (
        "## Submitted Summary\n\n"
        + render_untrusted_evidence_block("Submitted Summary", summary)
        + "\n\n"
    )
    context_block = (
        "## Context\n\n" + render_untrusted_evidence_block("Context", context) + "\n\n" if context else ""
    )
    body = (
        f"# {visible_title}\n\n"
        "## Task\n\n"
        "- Status: open\n"
        f"- Priority: {question['priority']}\n"
        f"- Origin: {question['origin']}\n\n"
        f"{intro} Resolve with the `research-answer`\n"
        "skill using maintained workspace knowledge. When answered, set `status:\n"
        "answered` and link the answer via `answer_page`. If it cannot be answered\n"
        "from current evidence, set `status: blocked` and record `blocked_reason`\n"
        "plus the sources needed.\n\n"
        f"{summary_block}"
        f"{context_block}"
        "## Answer\n\n"
        "_Not yet answered._\n"
    )
    return f"---\n{rendered}\n---\n\n{body}"


def write_question_pages(target: Path, config: dict[str, Any], seed_questions: list[dict[str, str]]) -> None:
    if not seed_questions:
        return
    wiki_config = config_mapping(config, "wiki")
    required_dirs = config_list(wiki_config.get("required_dirs"), "wiki.required_dirs")
    if QUESTION_WIKI_DIR not in required_dirs:
        raise SystemExit(
            f"Cannot seed questions: wiki.required_dirs must include '{QUESTION_WIKI_DIR}'."
        )
    wiki_root_value = wiki_config.get("root") if isinstance(wiki_config.get("root"), str) else "wiki"
    questions_dir = target / wiki_root_value / QUESTION_WIKI_DIR
    ensure_private_dir(questions_dir, target)
    for question in seed_questions:
        page_path = questions_dir / f"{question['slug']}.md"
        write_private_text(page_path, render_question_page(question), target)



def starter_version(target: Path) -> str:
    metadata = load_yaml(target / "workspace-system.yml", "workspace-system.yml")
    workspace_system = metadata.get("workspace_system") or {}
    if isinstance(workspace_system, dict) and isinstance(workspace_system.get("starter_version"), str):
        return workspace_system["starter_version"]
    return "unknown"


def render_log(config: dict[str, Any], options: InitOptions, domain_pack: DomainPackSelection | None, target: Path) -> str:
    project = config_mapping(config, "project")
    date = datetime.now(timezone.utc).date().isoformat()
    domain_pack_label = domain_pack.name if domain_pack is not None else "none"
    profile_label = options.profile_path.as_posix() if options.profile_path is not None else "none"
    guidance_path = project_domain_guidance_path(options.profile)
    guidance_line = f"- Project-local domain guidance: {guidance_path}.\n" if guidance_path is not None else ""
    report_path = init_report_path(options.profile)
    report_line = f"- Init report: {report_path}.\n" if report_path is not None else ""
    seed_questions = normalize_seed_questions(options.profile)
    question_line = (
        f"- Seeded {len(seed_questions)} open question task(s) in wiki/{QUESTION_WIKI_DIR}.\n"
        if seed_questions
        else ""
    )
    return (
        "# Research Wiki Activity Log\n\n"
        "Append-only record of project operations.\n\n"
        "Canonical entry heading: `## [YYYY-MM-DD] operation | Description`\n\n"
        f"## [{date}] setup | Workspace initialized\n\n"
        f"- Created workspace from starter version `{starter_version(target)}`.\n"
        f"- Project: `{project.get('name')}` - {project.get('description')}.\n"
        f"- Owner goal: {project.get('owner_goal')}.\n"
        f"- Domain pack: {domain_pack_label}.\n"
        f"{guidance_line}"
        f"{report_line}"
        f"{question_line}"
        f"- Setup profile: {profile_label}.\n"
        "- Raw source roots are configured as immutable evidence boundaries.\n"
    )


def write_workspace_files(target: Path, config: dict[str, Any], options: InitOptions, domain_pack: DomainPackSelection | None) -> None:
    seed_questions = normalize_seed_questions(options.profile)
    write_yaml(target / "research.yml", config, target)
    ensure_configured_directories(target, config)
    write_question_pages(target, config, seed_questions)
    write_private_text(target / "index.md", render_index(config, seed_questions), target)
    write_private_text(target / "log.md", render_log(config, options, domain_pack, target), target)
    write_project_domain_guidance(target, config, options)
    write_init_report(target, config, options, domain_pack)


def print_plan(options: InitOptions, domain_pack: DomainPackSelection | None) -> None:
    domain_pack_label = domain_pack.name if domain_pack is not None else "none"
    guidance_path = project_domain_guidance_path(options.profile)
    report_path = init_report_path(options.profile)
    mode = "dry run" if options.dry_run else "write"
    print("Workspace initialization plan:")
    print(f"- mode: {mode}")
    print(f"- starter root: {options.starter_root}")
    if options.scope_root is not None:
        print(f"- scope root: {options.scope_root}")
    print(f"- target: {options.target}")
    print(f"- project name: {options.project_name}")
    print(f"- project description: {options.project_description}")
    print(f"- language: {options.language}")
    print(f"- domain pack: {domain_pack_label}")
    if guidance_path is not None:
        print(f"- project-local domain guidance: {guidance_path}")
    if report_path is not None:
        print(f"- init report: {report_path}")
    if options.profile_path is not None:
        print(f"- setup profile: {options.profile_path}")
    seed_questions = normalize_seed_questions(options.profile)
    if seed_questions:
        print(f"- seeded questions: {len(seed_questions)}")
    if options.dry_run:
        print("- writes: none")


def initialize_workspace(options: InitOptions) -> None:
    validate_starter_root(options.starter_root)
    validate_target(options)
    domain_pack = resolve_domain_pack(options.domain_pack, options.starter_root)
    config = build_config(options, domain_pack)
    print_plan(options, domain_pack)
    if options.dry_run:
        return

    options.target.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_dir(options.target, options.target)
    copy_starter_tree(options.starter_root, options.target)
    if domain_pack is not None:
        copy_starter_tree(domain_pack.source_path, options.target / domain_pack.target_relative)
    write_workspace_files(options.target, config, options, domain_pack)
    print(f"Created research workspace: {options.target}")


UPGRADE_DEFAULT_PATHS = ("scripts",)
UPGRADE_OPTIONAL_PATHS = ("skills", "docs")


class UpgradeWriteError(OSError):
    """Bounded, retryable failure while atomically refreshing managed content."""

    error_code = "UPGRADE_WRITE_FAILED"
    remediation = (
        "Restore write access and free space for the target workspace, preview "
        "the same command with --dry-run, then retry the upgrade."
    )

    def __init__(self, relative_path: Path, exc: OSError):
        reason = " ".join(str(exc.strerror or type(exc).__name__).split())
        if not reason:
            reason = "operating system write failure"
        reason = reason[:160]
        path_text = relative_path.as_posix()
        self.details = {
            "path": path_text,
            "reason": reason,
            "preserved": (
                "The managed path is either the prior complete file or the complete replacement; "
                "the workspace version marker and upgrade log were not advanced. Inspect the "
                "target and retry the upgrade."
            ),
        }
        super().__init__(f"Could not write starter-managed path {path_text}: {reason}.")
WORKSPACE_MARKER_FILES = ("research.yml", "workspace-system.yml")


def parse_upgrade_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="evidence-wiki upgrade",
        description=(
            "Refresh starter-managed tooling in an existing research workspace from the "
            "current starter. Never touches research.yml, raw/, sources/, wiki/, index.md, or log.md."
        ),
    )
    parser.add_argument("--target", help="Existing workspace path. Defaults to the current directory.")
    parser.add_argument(
        "--starter-root",
        help="Reusable starter root to upgrade from. Defaults to the parent directory of this script.",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        choices=UPGRADE_OPTIONAL_PATHS,
        help="Additionally refresh an optional managed path. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing files.",
    )
    parser.add_argument(
        "--force-optional",
        action="store_true",
        help=(
            "Allow --include skills/docs to overwrite user-edited optional files. "
            "The displaced file is preserved under .replaced/<path>."
        ),
    )
    return parser.parse_args(argv)


def is_research_workspace(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in WORKSPACE_MARKER_FILES)


def managed_upgrade_paths(include: list[str]) -> list[str]:
    paths = list(UPGRADE_DEFAULT_PATHS)
    for name in include:
        if name not in paths:
            paths.append(name)
    return paths


def iter_managed_files(starter_root: Path, relative_dir: str) -> list[Path]:
    base = starter_root / relative_dir
    if not base.is_dir():
        return []
    files: list[Path] = []
    for source in base.rglob("*"):
        if not source.is_file():
            continue
        if should_skip(source.relative_to(starter_root)):
            continue
        files.append(source)
    return sorted(files, key=lambda path: path.as_posix())


def optional_backup_path(target: Path, relative: Path) -> Path:
    return target / ".replaced" / relative


def optional_upgrade_replacements(
    starter_root: Path,
    target: Path,
    paths: list[str],
    force_optional: bool,
) -> list[str]:
    target_resolved = target.resolve()
    replacements: list[str] = []
    for relative_dir in paths:
        if relative_dir not in UPGRADE_OPTIONAL_PATHS or not (starter_root / relative_dir).is_dir():
            continue
        for source in iter_managed_files(starter_root, relative_dir):
            relative = source.relative_to(starter_root)
            destination = target / relative
            if not is_contained_nonsymlink(destination, target_resolved):
                raise SystemExit(f"Refusing to write through symlink in workspace: {destination}")
            if destination.exists() and destination.is_dir():
                raise SystemExit(f"Cannot upgrade file over existing directory: {destination}")
            if not destination.is_file() or destination.read_bytes() == source.read_bytes():
                continue
            relative_posix = relative.as_posix()
            if not force_optional:
                raise SystemExit(
                    "Refusing to overwrite user-edited optional file without "
                    f"--force-optional: {relative_posix}"
                )
            backup = optional_backup_path(target, relative)
            if not is_contained_nonsymlink(backup, target_resolved):
                raise SystemExit(f"Refusing to write through symlink in workspace: {backup}")
            if backup.exists():
                raise SystemExit(
                    f"Refusing to overwrite existing optional backup: .replaced/{relative_posix}"
                )
            if backup.parent.exists() and not backup.parent.is_dir():
                raise SystemExit(f"Cannot create optional backup directory over existing file: {backup.parent}")
            replacements.append(relative_posix)
    return replacements


def atomic_upgrade_write(destination: Path, contents: bytes, relative_path: Path) -> None:
    """Write one managed file without exposing partial destination contents."""
    temporary_path = destination.with_name(f".{destination.name}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_bytes(contents)
        temporary_path.replace(destination)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpgradeWriteError(relative_path, exc) from None


def refresh_managed_path(
    starter_root: Path,
    target: Path,
    relative_dir: str,
    dry_run: bool,
    force_optional: bool = False,
    replaced: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    target_resolved = target.resolve()
    created: list[str] = []
    updated: list[str] = []
    is_optional = relative_dir in UPGRADE_OPTIONAL_PATHS
    for source in iter_managed_files(starter_root, relative_dir):
        relative = source.relative_to(starter_root)
        destination = target / relative
        backup_created: Path | None = None
        # Fail closed before reading or writing the destination: refuse a planted
        # symlink at the destination, or a symlinked parent directory. A symlinked
        # parent would otherwise redirect both the `.tmp` write and the atomic
        # `replace` below outside the workspace; a symlinked leaf would be read
        # through by `is_file()`/`read_bytes()`. is_contained_nonsymlink is the
        # shared containment definition (SEC-E1-T03/T04).
        if not is_contained_nonsymlink(destination, target_resolved):
            raise SystemExit(f"Refusing to write through symlink in workspace: {destination}")
        if destination.exists() and destination.is_dir():
            raise SystemExit(f"Cannot upgrade file over existing directory: {destination}")
        new_bytes = source.read_bytes()
        if destination.is_file():
            old_bytes = destination.read_bytes()
            if old_bytes == new_bytes:
                continue
            if is_optional and force_optional:
                backup = optional_backup_path(target, relative)
                if not is_contained_nonsymlink(backup, target_resolved):
                    raise SystemExit(f"Refusing to write through symlink in workspace: {backup}")
                if backup.exists():
                    raise SystemExit(
                        f"Refusing to overwrite existing optional backup: .replaced/{relative.as_posix()}"
                    )
                if not dry_run:
                    atomic_upgrade_write(backup, old_bytes, Path(".replaced") / relative)
                    backup_created = backup
                if replaced is not None:
                    replaced.append(relative.as_posix())
            updated.append(relative.as_posix())
        else:
            created.append(relative.as_posix())
        if not dry_run:
            try:
                atomic_upgrade_write(destination, new_bytes, relative)
            except UpgradeWriteError:
                if backup_created is not None:
                    try:
                        backup_created.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
    return created, updated


def starter_version_value(meta_path: Path) -> str | None:
    metadata = load_yaml(meta_path, "workspace-system.yml")
    workspace_system = metadata.get("workspace_system")
    if isinstance(workspace_system, dict) and isinstance(workspace_system.get("starter_version"), str):
        return workspace_system["starter_version"]
    return None


def workspace_contract_values(meta_path: Path, label: str) -> dict[str, str]:
    metadata = load_yaml(meta_path, label)
    workspace_system = metadata.get("workspace_system")
    if not isinstance(workspace_system, dict):
        raise SystemExit(f"{label} workspace_system must be a mapping; restore compatible metadata before upgrade")
    values: dict[str, str] = {}
    for field in ("starter_version", "schema_version", "compatible_research_yml_contract"):
        value = workspace_system.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(
                f"{label} workspace_system.{field} rejected value {value!r}; "
                "restore a non-empty version string before upgrade"
            )
        values[field] = value.strip()
    return values


def validate_upgrade_compatibility(starter_root: Path, target: Path) -> dict[str, dict[str, str]]:
    starter = workspace_contract_values(starter_root / "workspace-system.yml", "starter workspace-system.yml")
    workspace = workspace_contract_values(target / "workspace-system.yml", "target workspace-system.yml")
    checks = (
        (
            "schema_version",
            SUPPORTED_WORKSPACE_SCHEMA_VERSIONS,
            "workspace schema",
        ),
        (
            "compatible_research_yml_contract",
            SUPPORTED_RESEARCH_YML_CONTRACTS,
            "research.yml contract",
        ),
    )
    for field, supported, description in checks:
        for label, values in (("starter", starter), ("target", workspace)):
            if values[field] not in supported:
                allowed = ", ".join(supported)
                raise SystemExit(
                    f"{label} workspace-system.yml workspace_system.{field} rejected value "
                    f"{values[field]!r}; supported {description} version(s): {allowed}. "
                    "Use a compatible package release or an explicit reviewed migration."
                )
    if workspace["compatible_research_yml_contract"] != starter["compatible_research_yml_contract"]:
        raise SystemExit(
            "target workspace-system.yml workspace_system.compatible_research_yml_contract "
            f"rejected value {workspace['compatible_research_yml_contract']!r}; starter requires "
            f"{starter['compatible_research_yml_contract']!r}. Use a compatible intermediate upgrade."
        )
    return {"starter": starter, "target": workspace}


def sync_starter_version(target: Path, starter_root: Path, dry_run: bool) -> str | None:
    starter_meta = starter_root / "workspace-system.yml"
    target_meta = target / "workspace-system.yml"
    if not starter_meta.is_file() or not target_meta.is_file():
        return None
    new_version = starter_version_value(starter_meta)
    if new_version is None or new_version == starter_version_value(target_meta):
        return None
    if not dry_run:
        text = target_meta.read_text()
        # Line-targeted replacement preserves the file's comments and formatting.
        updated = re.sub(
            r'(?m)^(\s*starter_version:\s*)"[^"]*"\s*$',
            lambda match: f'{match.group(1)}"{new_version}"',
            text,
            count=1,
        )
        if updated != text:
            target_meta.write_text(updated)
    return new_version


def append_upgrade_log(
    target: Path,
    paths: list[str],
    created: list[str],
    updated: list[str],
    replaced: list[str],
    version: str | None,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    log_path = target / "log.md"
    if not log_path.is_file():
        return
    date = datetime.now(timezone.utc).date().isoformat()
    version_line = f"- Starter version: `{version}`.\n" if version else ""
    entry = (
        f"## [{date}] upgrade | Refreshed starter-managed tooling\n\n"
        f"- Paths: {', '.join(paths)}.\n"
        f"- Created: {len(created)} file(s); updated: {len(updated)} file(s).\n"
        f"- Replaced optional files: {len(replaced)} file(s).\n"
        f"{version_line}"
    )
    append_log_entry(log_path, entry)


def append_log_entry(log_path: Path, entry: str) -> None:
    """Append a rendered log entry atomically under the workspace log lock."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with workspace_lock(log_path.parent / ".locks" / "log.lock", purpose="activity log append"):
        handle = log_path.open("a+", encoding="utf-8")
        try:
            handle.seek(0)
            content = handle.read()
            if not content:
                prefix = "# Research Wiki Activity Log\n\n"
            elif content.endswith("\n\n"):
                prefix = ""
            elif content.endswith("\n"):
                prefix = "\n"
            else:
                prefix = "\n\n"
            handle.seek(0, 2)
            handle.write(prefix + entry + "\n")
        finally:
            handle.close()


def upgrade_workspace(
    starter_root: Path,
    target: Path,
    paths: list[str],
    dry_run: bool,
    force_optional: bool = False,
) -> dict[str, Any]:
    validate_starter_root(starter_root)
    if not is_research_workspace(target):
        raise SystemExit(
            f"Not a research workspace (missing {' / '.join(WORKSPACE_MARKER_FILES)}): {target}"
        )
    if target == starter_root or is_relative_to(target, starter_root):
        raise SystemExit("Refusing to upgrade the reusable starter root itself.")
    validate_upgrade_compatibility(starter_root, target)
    lock_path = target / ".locks" / "upgrade.lock"
    lock_existed = lock_path.exists()
    lock_dir_existed = lock_path.parent.exists()
    lock_context = (
        nullcontext()
        if dry_run
        else workspace_lock(lock_path, purpose="workspace upgrade")
    )
    try:
        with lock_context:
            validate_upgrade_compatibility(starter_root, target)
            optional_upgrade_replacements(starter_root, target, paths, force_optional)
            created_all: list[str] = []
            updated_all: list[str] = []
            replaced_all: list[str] = []
            for relative_dir in paths:
                if not (starter_root / relative_dir).is_dir():
                    continue
                created, updated = refresh_managed_path(
                    starter_root,
                    target,
                    relative_dir,
                    dry_run,
                    force_optional=force_optional,
                    replaced=replaced_all,
                )
                created_all.extend(created)
                updated_all.extend(updated)
            version = sync_starter_version(target, starter_root, dry_run)
            if created_all or updated_all or version:
                append_upgrade_log(target, paths, created_all, updated_all, replaced_all, version, dry_run)
    except BaseException:
        if not dry_run and not lock_existed:
            try:
                lock_path.unlink(missing_ok=True)
                if not lock_dir_existed:
                    lock_path.parent.rmdir()
            except OSError:
                pass
        raise
    return {"created": created_all, "updated": updated_all, "replaced": replaced_all, "starter_version": version}


def print_upgrade_summary(
    target: Path,
    starter_root: Path,
    paths: list[str],
    result: dict[str, Any],
    dry_run: bool,
) -> None:
    print("Workspace upgrade:")
    print(f"- mode: {'dry run' if dry_run else 'write'}")
    print(f"- starter root: {starter_root}")
    print(f"- target: {target}")
    print(f"- managed paths: {', '.join(paths)}")
    for path in result["created"]:
        print(f"- {'would create' if dry_run else 'create'}: {path}")
    for path in result["updated"]:
        print(f"- {'would update' if dry_run else 'update'}: {path}")
    for path in result["replaced"]:
        print(f"- {'would replace optional file' if dry_run else 'replaced optional file'}: {path}")
    if not result["created"] and not result["updated"]:
        print("- no changes: workspace tooling already current")
    if result["starter_version"]:
        print(f"- {'would set' if dry_run else 'set'} starter version: {result['starter_version']}")


def initializer_error_contract(
    message: str,
    *,
    operation: str,
    error_code: str | None = None,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return bounded remediation for init/upgrade failures without cleanup."""
    lower = message.lower()
    if "missing target path" in lower:
        selected_code = "VALUE_INVALID"
        selected_remediation = "Provide --target or workspace_init.target_path, preview with --dry-run, then retry."
        preserved = "Validation stopped before a target path was selected; no workspace files were written."
    elif "invalid yaml" in lower or "setup profile" in lower:
        selected_code = "CONFIG_INVALID"
        selected_remediation = (
            "Repair the YAML/profile field named in the message, then rerun the same command with --dry-run."
        )
        preserved = "Profile validation stopped before workspace initialization; no target files were written."
    elif "workspace-system.yml" in lower and ("supported" in lower or "compatible" in lower):
        selected_code = "CONFIG_INVALID"
        selected_remediation = (
            "Use a compatible package release or an explicit reviewed migration; do not force the upgrade."
        )
        preserved = "The compatibility check stopped before managed workspace files were replaced."
    else:
        selected_code = "WORKSPACE_UNREADABLE"
        selected_remediation = (
            f"Fix the rejected {operation} input or workspace state, inspect the target, and retry with --dry-run."
        )
        preserved = "No automatic cleanup was performed; inspect any existing target content before retrying."
    selected_details = {"operation": operation, "preserved": preserved}
    if details:
        selected_details.update(details)
    return error_envelope(
        error_code or selected_code,
        message,
        recoverable=True,
        remediation=remediation or selected_remediation,
        details=selected_details,
    )


def emit_initializer_error(
    message: str,
    *,
    operation: str,
    error_code: str | None = None,
    remediation: str | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Render the shared envelope fields as readable, structured CLI text."""
    envelope = initializer_error_contract(
        message,
        operation=operation,
        error_code=error_code,
        remediation=remediation,
        details=details,
    )
    print(f"{envelope['error_code']}: {envelope['message']}", file=sys.stderr)
    print(f"Remediation: {envelope['remediation']}", file=sys.stderr)
    print(f"Preserved: {envelope['details']['preserved']}", file=sys.stderr)
    return 2


def upgrade_main(argv: list[str] | None = None) -> int:
    args = parse_upgrade_args(argv)
    starter_root = Path(args.starter_root).expanduser().resolve() if args.starter_root else default_starter_root()
    target = Path(args.target).expanduser().resolve() if args.target else Path.cwd()
    paths = managed_upgrade_paths(args.include)
    dry_run = bool(args.dry_run)
    result = upgrade_workspace(starter_root, target, paths, dry_run, force_optional=bool(args.force_optional))
    print_upgrade_summary(target, starter_root, paths, result, dry_run)
    if not dry_run:
        print(f"Upgraded research workspace: {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        options = resolve_options(parse_args(argv))
        initialize_workspace(options)
        return 0
    except LockUnavailableError as exc:
        print(f"{exc.error_code}: {exc}", file=sys.stderr)
        return 2


def entrypoint(argv: list[str] | None = None) -> int:
    """Normalize direct-script validation failures without changing ``main`` tests."""
    try:
        return main(argv)
    except SystemExit as exc:
        if not isinstance(exc.code, str):
            raise
        return emit_initializer_error(exc.code, operation="initialization")


if __name__ == "__main__":
    raise SystemExit(entrypoint())
