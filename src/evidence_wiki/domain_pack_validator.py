"""Validate reusable EvidenceWiki domain packs."""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

import yaml

from .resources import STARTER_DIR, assets_root

SCHEMA_VERSION = "1.0"
ALLOWED_RECOMMENDED_ACQUISITION = ("arxiv", "openalex")
ALLOWED_RECOMMENDED_DISCOVERY = ("arxiv", "openalex")
COVERAGE_TEMPLATE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ALLOWED_PACK_FILE_SUFFIXES = {".csv", ".json", ".md", ".txt", ".yaml", ".yml"}
WINDOWS_RESERVED_PACK_NAMES = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class LoadedScripts:
    initializer: ModuleType
    smoke: ModuleType
    coverage: ModuleType
    errors: ModuleType
    evidence: ModuleType


def _load_script(script_path: Path, module_name: str) -> ModuleType:
    from .cli import _load_script as load_packaged_script

    return load_packaged_script(script_path, module_name)


def load_scripts(starter_root: Path) -> LoadedScripts:
    scripts_dir = starter_root / "scripts"
    return LoadedScripts(
        initializer=_load_script(scripts_dir / "init_research_workspace.py", "domain_pack_validator_init"),
        smoke=_load_script(scripts_dir / "smoke_validate_workspace.py", "domain_pack_validator_smoke"),
        coverage=_load_script(scripts_dir / "coverage_manifest.py", "domain_pack_validator_coverage"),
        errors=_load_script(scripts_dir / "_script_errors.py", "domain_pack_validator_errors"),
        evidence=_load_script(scripts_dir / "_evidence_policies.py", "domain_pack_validator_evidence"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a reusable EvidenceWiki domain pack.")
    parser.add_argument("--path", required=True, help="Domain pack name or filesystem path.")
    parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Output format. Defaults to json.",
    )
    return parser.parse_args(argv)


def check(check_id: str, status: str, message: str, files: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "files": files or [],
    }


def load_overlay(overlay_path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not overlay_path.is_file():
        return None, check(
            "overlay_parse",
            "fail",
            "Domain pack is missing research.overlay.yml",
            ["research.overlay.yml"],
        )
    try:
        document = yaml.safe_load(overlay_path.read_text()) or {}
    except yaml.YAMLError as exc:
        return None, check(
            "overlay_parse",
            "fail",
            f"Invalid YAML in research.overlay.yml: {exc}",
            ["research.overlay.yml"],
        )
    if not isinstance(document, dict):
        return None, check(
            "overlay_parse",
            "fail",
            "research.overlay.yml must contain a mapping",
            ["research.overlay.yml"],
        )
    return document, check("overlay_parse", "pass", "research.overlay.yml parses as a mapping.", ["research.overlay.yml"])


def string_field(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def starter_contract(starter_root: Path) -> str | None:
    metadata_path = starter_root / "workspace-system.yml"
    try:
        metadata = yaml.safe_load(metadata_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid workspace-system.yml: {exc}") from exc
    workspace_system = metadata.get("workspace_system") if isinstance(metadata, dict) else {}
    if not isinstance(workspace_system, dict):
        raise SystemExit("workspace-system.yml workspace_system must be a mapping")
    contract = workspace_system.get("compatible_research_yml_contract")
    if not isinstance(contract, str) or not contract.strip():
        raise SystemExit("workspace-system.yml missing compatible_research_yml_contract")
    return contract.strip()


def metadata_check(domain_pack: Any, expected_contract: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(domain_pack, dict):
        return (
            {
                "name": None,
                "version": None,
                "compatible_research_yml_contract": None,
                "recommended_acquisition": [],
                "recommended_discovery": [],
                "coverage_templates": {},
            },
            [
                check(
                    "required_metadata",
                    "fail",
                    "research.overlay.yml must declare a domain_pack mapping.",
                    ["research.overlay.yml"],
                ),
                check(
                    "contract_compatibility",
                    "fail",
                    "Cannot compare contract compatibility without domain_pack metadata.",
                    ["research.overlay.yml"],
                ),
            ],
        )

    info = {
        "name": string_field(domain_pack, "name"),
        "version": string_field(domain_pack, "version"),
        "compatible_research_yml_contract": string_field(domain_pack, "compatible_research_yml_contract"),
        "recommended_acquisition": [],
        "recommended_discovery": [],
        "coverage_templates": {},
    }
    missing = [key for key, value in info.items() if value is None]
    checks = []
    if missing:
        checks.append(
            check(
                "required_metadata",
                "fail",
                f"domain_pack is missing required string field(s): {', '.join(missing)}.",
                ["research.overlay.yml"],
            )
        )
    else:
        checks.append(
            check(
                "required_metadata",
                "pass",
                "domain_pack declares name, version, and compatible_research_yml_contract.",
                ["research.overlay.yml"],
            )
        )

    pack_contract = info["compatible_research_yml_contract"]
    if pack_contract == expected_contract:
        checks.append(
            check(
                "contract_compatibility",
                "pass",
                f"Domain pack contract {pack_contract} matches starter contract {expected_contract}.",
                ["research.overlay.yml", "workspace-system.yml"],
            )
        )
    else:
        checks.append(
            check(
                "contract_compatibility",
                "fail",
                f"Domain pack contract {pack_contract!r} does not match starter contract {expected_contract!r}.",
                ["research.overlay.yml", "workspace-system.yml"],
            )
        )
    return info, checks


def recommended_acquisition_check(domain_pack: Any) -> tuple[list[str], dict[str, Any]]:
    if not isinstance(domain_pack, dict):
        return [], check(
            "recommended_acquisition",
            "pass",
            "No domain-pack acquisition recommendations declared.",
            ["research.overlay.yml"],
        )
    value = domain_pack.get("recommended_acquisition")
    if value is None:
        return [], check(
            "recommended_acquisition",
            "pass",
            "No domain-pack acquisition recommendations declared.",
            ["research.overlay.yml"],
        )
    if not isinstance(value, list):
        return [], check(
            "recommended_acquisition",
            "fail",
            "domain_pack.recommended_acquisition must be a list of provider identifiers.",
            ["research.overlay.yml"],
        )
    providers: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return [], check(
                "recommended_acquisition",
                "fail",
                "domain_pack.recommended_acquisition must contain only non-empty provider identifiers.",
                ["research.overlay.yml"],
            )
        providers.append(item.strip())
    duplicates = sorted({provider for provider in providers if providers.count(provider) > 1})
    if duplicates:
        return providers, check(
            "recommended_acquisition",
            "fail",
            f"domain_pack.recommended_acquisition contains duplicate provider(s): {', '.join(duplicates)}.",
            ["research.overlay.yml"],
        )
    unknown = sorted(set(providers) - set(ALLOWED_RECOMMENDED_ACQUISITION))
    if unknown:
        allowed = ", ".join(ALLOWED_RECOMMENDED_ACQUISITION)
        return providers, check(
            "recommended_acquisition",
            "fail",
            (
                "domain_pack.recommended_acquisition contains unknown provider(s): "
                f"{', '.join(unknown)}. Allowed providers: {allowed}."
            ),
            ["research.overlay.yml"],
        )
    return providers, check(
        "recommended_acquisition",
        "pass",
        f"Recommended acquisition providers are valid: {', '.join(providers) or 'none'}.",
        ["research.overlay.yml"],
    )


def recommended_discovery_check(domain_pack: Any) -> tuple[list[str], dict[str, Any]]:
    if not isinstance(domain_pack, dict):
        return [], check(
            "recommended_discovery",
            "pass",
            "No domain-pack discovery recommendations declared.",
            ["research.overlay.yml"],
        )
    value = domain_pack.get("recommended_discovery")
    if value is None:
        return [], check(
            "recommended_discovery",
            "pass",
            "No domain-pack discovery recommendations declared.",
            ["research.overlay.yml"],
        )
    if not isinstance(value, list):
        return [], check(
            "recommended_discovery",
            "fail",
            "domain_pack.recommended_discovery must be a list of provider identifiers.",
            ["research.overlay.yml"],
        )
    providers: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return [], check(
                "recommended_discovery",
                "fail",
                "domain_pack.recommended_discovery must contain only non-empty provider identifiers.",
                ["research.overlay.yml"],
            )
        providers.append(item.strip())
    duplicates = sorted({provider for provider in providers if providers.count(provider) > 1})
    if duplicates:
        return providers, check(
            "recommended_discovery",
            "fail",
            f"domain_pack.recommended_discovery contains duplicate provider(s): {', '.join(duplicates)}.",
            ["research.overlay.yml"],
        )
    unknown = sorted(set(providers) - set(ALLOWED_RECOMMENDED_DISCOVERY))
    if unknown:
        return providers, check(
            "recommended_discovery",
            "fail",
            (
                "domain_pack.recommended_discovery contains unknown provider(s): "
                f"{', '.join(unknown)}. Allowed providers: {', '.join(ALLOWED_RECOMMENDED_DISCOVERY)}."
            ),
            ["research.overlay.yml"],
        )
    return providers, check(
        "recommended_discovery",
        "pass",
        f"Recommended discovery providers are valid: {', '.join(providers) or 'none'}.",
        ["research.overlay.yml"],
    )


def human_gated_check(domain_pack: Any) -> tuple[bool, dict[str, Any]]:
    if not isinstance(domain_pack, dict):
        return False, check(
            "human_gated",
            "pass",
            "No domain-pack human gating declaration present.",
            ["research.overlay.yml"],
        )
    value = domain_pack.get("human_gated", False)
    if isinstance(value, bool):
        if value:
            return True, check(
                "human_gated",
                "pass",
                "domain_pack.human_gated explicitly allows required facets that depend on human review.",
                ["research.overlay.yml"],
            )
        return False, check(
            "human_gated",
            "pass",
            "domain_pack.human_gated is false; required coverage facets must be autonomously satisfiable.",
            ["research.overlay.yml"],
        )
    return False, check(
        "human_gated",
        "fail",
        "domain_pack.human_gated must be a boolean when present.",
        ["research.overlay.yml"],
    )


def policy_vocabularies_check(scripts: LoadedScripts, domain_pack: Any) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    empty = {field: {} for field in scripts.coverage.POLICY_VOCABULARY_FIELDS}
    if not isinstance(domain_pack, dict):
        return empty, check(
            "policy_vocabularies",
            "pass",
            "No domain-pack policy vocabulary extensions declared.",
            ["research.overlay.yml"],
        )
    try:
        vocabularies = scripts.coverage.domain_pack_policy_vocabularies({"domain_pack": domain_pack})
        merged = scripts.coverage.merged_policy_vocabularies({"domain_pack": domain_pack})
    except scripts.coverage.CoverageManifestError as exc:
        return empty, check("policy_vocabularies", "fail", str(exc), ["research.overlay.yml"])
    declared_count = sum(len(values) for values in vocabularies.values())
    if declared_count == 0:
        return vocabularies, check(
            "policy_vocabularies",
            "pass",
            "No domain-pack policy vocabulary extensions declared.",
            ["research.overlay.yml"],
        )
    # Calling merged_policy_vocabularies above also proves declarations do not
    # collide with base ids. Keep a cheap reference so linters see the guard.
    _ = merged
    return vocabularies, check(
        "policy_vocabularies",
        "pass",
        f"Domain pack declares {declared_count} namespaced policy vocabulary extension(s).",
        ["research.overlay.yml"],
    )


def coverage_templates_check(
    scripts: LoadedScripts,
    pack_path: Path,
    domain_pack: Any,
    policy_vocabularies: dict[str, dict[str, str]] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    if not isinstance(domain_pack, dict):
        return {}, check(
            "coverage_templates",
            "pass",
            "No domain-pack coverage templates declared.",
            ["research.overlay.yml"],
        )
    value = domain_pack.get("coverage_templates")
    if value is None:
        return {}, check(
            "coverage_templates",
            "pass",
            "No domain-pack coverage templates declared.",
            ["research.overlay.yml"],
        )
    if not isinstance(value, dict):
        return {}, check(
            "coverage_templates",
            "fail",
            "domain_pack.coverage_templates must be a mapping of template slug to pack-local YAML path.",
            ["research.overlay.yml"],
        )

    normalized: dict[str, str] = {}
    errors: list[str] = []
    files = ["research.overlay.yml"]
    for raw_slug, raw_path in value.items():
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            errors.append("domain_pack.coverage_templates keys must be non-empty strings")
            continue
        slug = raw_slug.strip()
        if not COVERAGE_TEMPLATE_SLUG_RE.match(slug):
            errors.append(f"coverage template key {slug!r} must be a lowercase hyphenated slug")

        relative, reason = normalize_pack_relative_path(raw_path)
        if relative is None:
            errors.append(f"coverage template {slug!r} path {raw_path!r} {reason}")
            continue
        if reason is not None:
            errors.append(f"coverage template {slug!r} path {relative} {reason}")
            continue

        normalized[slug] = relative
        files.append(relative)
        template_path = pack_path / relative
        if not template_path.is_file():
            errors.append(f"Missing coverage template file: {relative}")
            continue
        try:
            scripts.coverage.load_template(relative, base_dir=pack_path, policy_vocabularies=policy_vocabularies)
        except scripts.coverage.CoverageManifestError as exc:
            errors.append(f"{relative}: {exc}")

    if errors:
        return normalized, check("coverage_templates", "fail", "; ".join(errors), sorted(set(files)))
    return normalized, check(
        "coverage_templates",
        "pass",
        f"Coverage templates are valid: {', '.join(sorted(normalized)) or 'none'}.",
        sorted(set(files)),
    )


def manual_only_policies_by_field(
    scripts: LoadedScripts,
    pack_policy_vocabularies: dict[str, dict[str, str]] | None,
) -> dict[str, set[str]]:
    pack_policy_vocabularies = pack_policy_vocabularies or {}
    return {
        "source_policy": set(getattr(scripts.evidence, "MANUAL_ONLY_SOURCE_POLICIES", ()))
        | set(pack_policy_vocabularies.get("source_policy", {})),
        "freshness_policy": set(getattr(scripts.evidence, "MANUAL_ONLY_FRESHNESS_POLICIES", ()))
        | set(pack_policy_vocabularies.get("freshness_policy", {})),
        "identity_policy": set(getattr(scripts.evidence, "MANUAL_ONLY_IDENTITY_POLICIES", ()))
        | set(pack_policy_vocabularies.get("identity_policy", {})),
    }


def autonomous_required_facets_check(
    scripts: LoadedScripts,
    pack_path: Path,
    coverage_templates: dict[str, str],
    *,
    human_gated: bool,
    pack_policy_vocabularies: dict[str, dict[str, str]] | None,
    merged_policy_vocabularies: dict[str, dict[str, str]] | None,
    templates_valid: bool,
) -> dict[str, Any]:
    if not coverage_templates:
        return check(
            "autonomous_required_facets",
            "pass",
            "No coverage templates declare required facets.",
            ["research.overlay.yml"],
        )
    if not templates_valid:
        return check(
            "autonomous_required_facets",
            "pass",
            "Skipped autonomy scan because coverage templates did not validate.",
            ["research.overlay.yml"],
        )
    if human_gated:
        return check(
            "autonomous_required_facets",
            "pass",
            "domain_pack.human_gated is true; manual-only required facets are explicit.",
            ["research.overlay.yml", *coverage_templates.values()],
        )

    manual_by_field = manual_only_policies_by_field(scripts, pack_policy_vocabularies)
    errors: list[str] = []
    files = ["research.overlay.yml"]
    for _slug, relative in sorted(coverage_templates.items()):
        files.append(relative)
        try:
            template = scripts.coverage.load_template(
                relative,
                base_dir=pack_path,
                policy_vocabularies=merged_policy_vocabularies,
            )
        except scripts.coverage.CoverageManifestError:
            continue
        for facet in template.get("required_facets", []):
            facet_id = facet.get("facet_id") if isinstance(facet, dict) else None
            label = facet_id if isinstance(facet_id, str) and facet_id else "<unknown>"
            for field, manual_values in manual_by_field.items():
                value = facet.get(field) if isinstance(facet, dict) else None
                if isinstance(value, str) and value in manual_values:
                    errors.append(
                        f"{relative} required facet {label!r} uses manual-only {field} {value!r}; "
                        "declare domain_pack.human_gated: true, move the facet to optional_facets, "
                        "or use a deterministic policy."
                    )

    if errors:
        return check("autonomous_required_facets", "fail", "; ".join(errors), sorted(set(files)))
    return check(
        "autonomous_required_facets",
        "pass",
        "Required coverage facets use deterministic policies for autonomous pack validation.",
        sorted(set(files)),
    )


def normalize_pack_relative_path(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, "must be a non-empty string path"
    raw = value.strip()
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
        elif path.as_posix() == ".":
            reason = "must not point at the pack root"
    if reason is not None:
        return normalized, reason
    return PurePosixPath(normalized).as_posix(), None


def collect_referenced_paths(domain_pack: Any) -> tuple[list[str], list[str]]:
    if not isinstance(domain_pack, dict):
        return [], []

    references: list[Any] = []
    errors: list[str] = []
    for field in ("taxonomy_doc", "claims_doc"):
        if field in domain_pack:
            references.append(domain_pack[field])

    scaffolds = domain_pack.get("scaffolds")
    if isinstance(scaffolds, dict):
        references.extend(scaffolds.values())
    elif scaffolds is not None:
        errors.append("domain_pack.scaffolds must be a mapping when present")

    implemented_files = domain_pack.get("implemented_files")
    if implemented_files is None:
        pass
    elif isinstance(implemented_files, list):
        references.extend(implemented_files)
    else:
        errors.append("domain_pack.implemented_files must be a list when present")

    normalized: list[str] = []
    for item in references:
        relative, reason = normalize_pack_relative_path(item)
        if relative is None:
            errors.append(f"{item!r} {reason}")
            continue
        if reason is not None:
            errors.append(f"{relative} {reason}")
            continue
        normalized.append(relative)
    return sorted(set(normalized)), errors


def referenced_files_check(pack_path: Path, domain_pack: Any) -> dict[str, Any]:
    referenced_paths, shape_errors = collect_referenced_paths(domain_pack)
    missing = [relative for relative in referenced_paths if not (pack_path / relative).is_file()]
    if shape_errors or missing:
        messages = []
        if shape_errors:
            messages.append("; ".join(shape_errors))
        if missing:
            messages.append(f"Missing referenced pack file(s): {', '.join(missing)}.")
        return check("referenced_files", "fail", " ".join(messages), missing)
    return check("referenced_files", "pass", "All declared domain-pack files exist.", referenced_paths)


def pack_tree_safety_check(pack_path: Path) -> dict[str, Any]:
    findings: list[str] = []
    files: list[str] = []
    if pack_path.is_symlink():
        return check(
            "pack_tree_safety",
            "fail",
            ".: symbolic-link domain-pack roots are not allowed",
            ["."],
        )
    root_name = pack_path.name
    if (
        any(ord(character) < 32 or character in '<>:"|?*' for character in root_name)
        or root_name.endswith((" ", "."))
        or root_name.split(".", 1)[0].casefold() in WINDOWS_RESERVED_PACK_NAMES
    ):
        findings.append(f".: non-portable domain-pack root name {root_name!r}")
        files.append(".")
    portable_paths: dict[str, str] = {}
    for path in sorted(pack_path.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(pack_path).as_posix()
        relative_parts = PurePosixPath(relative).parts
        portable_identity = "/".join(unicodedata.normalize("NFC", part).casefold() for part in relative_parts)
        previous = portable_paths.get(portable_identity)
        if previous is not None:
            findings.append(f"{relative}: portably collides with {previous}")
            files.append(relative)
        else:
            portable_paths[portable_identity] = relative
        for part in relative_parts:
            if (
                any(ord(character) < 32 or character in '<>:"|?*' for character in part)
                or part.endswith((" ", "."))
                or part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_PACK_NAMES
            ):
                findings.append(f"{relative}: non-portable path component {part!r}")
                files.append(relative)
                break
        if path.is_symlink():
            findings.append(f"{relative}: symbolic links are not allowed")
            files.append(relative)
            continue
        if path.is_dir():
            continue
        if not path.is_file():
            findings.append(f"{relative}: special filesystem entries are not allowed")
            files.append(relative)
            continue
        suffix = path.suffix.casefold()
        if suffix not in ALLOWED_PACK_FILE_SUFFIXES:
            findings.append(f"{relative}: executable or unsupported file type {suffix or '<none>'}")
            files.append(relative)
            continue
        if path.stat().st_mode & 0o111:
            findings.append(f"{relative}: executable permission bits are not allowed")
            files.append(relative)
        try:
            content = path.read_bytes()
            if b"\x00" in content:
                findings.append(f"{relative}: binary content is not allowed")
                files.append(relative)
            else:
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError:
                    findings.append(f"{relative}: content must be UTF-8 text")
                    files.append(relative)
        except OSError as exc:
            findings.append(f"{relative}: could not be read safely ({exc})")
            files.append(relative)
    if findings:
        return check(
            "pack_tree_safety",
            "fail",
            "; ".join(findings),
            sorted(set(files)),
        )
    return check(
        "pack_tree_safety",
        "pass",
        "Pack contains only inert, non-executable, non-symlinked data files.",
        [],
    )


def resolve_pack_path(selection: str, starter_root: Path) -> Path:
    """Resolve a pack location without opening pack-controlled content."""
    candidate = Path(selection).expanduser()
    if not candidate.exists():
        candidate = starter_root.parent / "domain-packs" / selection
    if not candidate.exists() or not candidate.is_dir():
        raise SystemExit(f"Domain pack not found: {selection}")
    # Preserve a linked root until the inert-tree check can report it. Resolving
    # first would erase the evidence that the caller selected a symlink.
    if candidate.is_symlink():
        return candidate.absolute()
    return candidate.resolve()


def unsafe_pack_payload(pack_path: Path, tree_safety: dict[str, Any]) -> dict[str, Any]:
    """Return a machine-readable refusal without parsing untrusted pack bytes."""
    reason = "pack tree safety failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "domain_pack": {
            "path": pack_path.absolute().as_posix(),
            "name": pack_path.name,
            "version": None,
            "compatible_research_yml_contract": None,
            "recommended_acquisition": [],
            "recommended_discovery": [],
            "coverage_templates": {},
            "human_gated": False,
            "policy_vocabularies": {},
        },
        "checks": [
            tree_safety,
            check("overlay_parse", "fail", f"Overlay parsing skipped because {reason}.", []),
            check("merged_config", "fail", f"Overlay cannot be merged because {reason}.", []),
            check("smoke_validation", "fail", f"Smoke validation skipped because {reason}.", []),
        ],
        "smoke_validation": {"ok": False, "summary": {}, "issues": []},
    }


def merged_config_check(
    scripts: LoadedScripts,
    starter_root: Path,
    domain_pack_selection: Any,
) -> dict[str, Any]:
    try:
        options = scripts.initializer.InitOptions(
            starter_root=starter_root,
            target=Path(tempfile.gettempdir()) / "domain-pack-validator-merge-check",
            project_name="domain-pack-validator",
            project_description="Temporary workspace for domain pack validation.",
            owner_goal="Validate reusable domain pack configuration.",
            language="en",
            domain_pack=domain_pack_selection.name,
            profile_path=None,
            profile={},
            dry_run=False,
            force=False,
        )
        scripts.initializer.build_config(options, domain_pack_selection)
    except SystemExit as exc:
        return check("merged_config", "fail", str(exc), ["research.overlay.yml", "research.yml"])
    return check(
        "merged_config",
        "pass",
        "Overlay deep-merges onto starter research.yml and passes initializer config validation.",
        ["research.overlay.yml", "research.yml"],
    )


def run_temp_init_and_smoke(
    scripts: LoadedScripts,
    starter_root: Path,
    pack_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="domain-pack-validate-") as tmpdir:
        target = Path(tmpdir) / "workspace"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = scripts.initializer.main(
                    [
                        "--starter-root",
                        str(starter_root),
                        "--target",
                        str(target),
                        "--project-name",
                        "domain-pack-validator",
                        "--project-description",
                        "Temporary workspace for domain pack validation.",
                        "--owner-goal",
                        "Validate reusable domain pack configuration.",
                        "--domain-pack",
                        str(pack_path),
                    ]
                )
            if int(code or 0) != 0:
                return {}, check("dry_run_init", "fail", f"Initializer exited with code {code}.", [])
        except SystemExit as exc:
            return {}, check("dry_run_init", "fail", str(exc), ["research.overlay.yml"])

        results = scripts.smoke.run_checks(target)
        if results.get("ok"):
            smoke_check = check("smoke_validation", "pass", "Smoke validation passed.", [])
        else:
            files: list[str] = []
            for issue in results.get("issues", []):
                files.extend(issue.get("files", []))
            smoke_check = check("smoke_validation", "fail", "Smoke validation failed.", sorted(set(files)))
        return results, smoke_check


def validate_domain_pack(selection: str, *, root: Path | None = None) -> dict[str, Any]:
    if root is None:
        with assets_root() as resolved_root:
            return validate_domain_pack(selection, root=resolved_root)

    starter_root = root / STARTER_DIR
    scripts = load_scripts(starter_root)
    pack_path = resolve_pack_path(selection, starter_root)
    tree_safety = pack_tree_safety_check(pack_path)
    if tree_safety["status"] != "pass":
        return unsafe_pack_payload(pack_path, tree_safety)
    # The canonical validator must own structured semantic failures. Calling
    # the initializer's fail-fast resolver here would turn a missing declared
    # file into an empty-stdout SystemExit instead of the validator contract.
    pack_selection = scripts.initializer.DomainPackSelection(
        name=pack_path.name,
        source_path=pack_path,
        target_relative=f"domain-packs/{pack_path.name}",
    )
    overlay, overlay_check = load_overlay(pack_path / "research.overlay.yml")
    checks = [overlay_check, tree_safety]

    domain_pack = copy.deepcopy(overlay.get("domain_pack")) if isinstance(overlay, dict) else None
    expected_contract = starter_contract(starter_root)
    domain_pack_info, metadata_checks = metadata_check(domain_pack, expected_contract)
    checks.extend(metadata_checks)
    recommended_acquisition, acquisition_check = recommended_acquisition_check(domain_pack)
    domain_pack_info["recommended_acquisition"] = recommended_acquisition
    checks.append(acquisition_check)
    recommended_discovery, discovery_check = recommended_discovery_check(domain_pack)
    domain_pack_info["recommended_discovery"] = recommended_discovery
    checks.append(discovery_check)
    human_gated, human_gated_result = human_gated_check(domain_pack)
    domain_pack_info["human_gated"] = human_gated
    checks.append(human_gated_result)
    policy_vocabularies, vocabulary_check = policy_vocabularies_check(scripts, domain_pack)
    domain_pack_info["policy_vocabularies"] = policy_vocabularies
    checks.append(vocabulary_check)
    merged_policy_vocabularies = None
    if isinstance(domain_pack, dict) and vocabulary_check["status"] == "pass":
        merged_policy_vocabularies = scripts.coverage.merged_policy_vocabularies({"domain_pack": domain_pack})
    coverage_templates, templates_check = coverage_templates_check(
        scripts,
        pack_path,
        domain_pack,
        policy_vocabularies=merged_policy_vocabularies,
    )
    domain_pack_info["coverage_templates"] = coverage_templates
    checks.append(templates_check)
    checks.append(
        autonomous_required_facets_check(
            scripts,
            pack_path,
            coverage_templates,
            human_gated=human_gated,
            pack_policy_vocabularies=policy_vocabularies,
            merged_policy_vocabularies=merged_policy_vocabularies,
            templates_valid=templates_check["status"] == "pass",
        )
    )
    checks.append(referenced_files_check(pack_path, domain_pack))

    if overlay is not None and tree_safety["status"] == "pass":
        checks.append(merged_config_check(scripts, starter_root, pack_selection))
        smoke_results, smoke_check = run_temp_init_and_smoke(scripts, starter_root, pack_path)
        checks.append(smoke_check)
    else:
        smoke_results = {}
        reason = "overlay did not parse" if overlay is None else "pack tree safety failed"
        checks.append(check("merged_config", "fail", f"Overlay cannot be merged because {reason}.", []))
        checks.append(check("smoke_validation", "fail", f"Smoke validation skipped because {reason}.", []))

    ok = all(item["status"] == "pass" for item in checks) and bool(smoke_results.get("ok", False))
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "domain_pack": {
            "path": pack_path.as_posix(),
            **domain_pack_info,
        },
        "checks": checks,
        "smoke_validation": {
            "ok": bool(smoke_results.get("ok", False)),
            "summary": smoke_results.get("summary", {}),
            "issues": smoke_results.get("issues", []),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with assets_root() as root:
        starter_root = root / STARTER_DIR
        errors = _load_script(
            starter_root / "scripts" / "_script_errors.py",
            "domain_pack_validator_main_errors",
        )
        try:
            payload = validate_domain_pack(args.path, root=root)
        except SystemExit as exc:
            return errors.handle_system_exit(
                exc,
                json_mode=True,
                default_exit_code=2,
                remediation="Use a domain pack name under domain-packs/ or pass a filesystem path to a pack directory.",
            )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
