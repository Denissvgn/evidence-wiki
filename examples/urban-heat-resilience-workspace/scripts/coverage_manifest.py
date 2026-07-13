#!/usr/bin/env python3
"""Manage per-question coverage manifests for deterministic answerability checks.

Coverage manifests live under ``sources.coverage_dir`` (default:
``sources/coverage``). They record the required and optional evidence facets
that must be grounded before later resolver gates can treat a high-stakes
question as fully answered. This script only manages and evaluates the manifest
state; it does not inspect source contents, fetch evidence, call an LLM, or
resolve question lifecycle status.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to manage coverage manifests") from exc

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_INVALID = 2
DEFAULT_COVERAGE_DIR = "sources/coverage"
DEFAULT_COVERAGE_PROFILE = "manual"

REQUIRED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "question_slug",
    "created_at",
    "updated_at",
    "coverage_profile",
    "required_facets",
    "optional_facets",
    "coverage_verdict",
}
REQUIRED_FACET_FIELDS = {
    "facet_id",
    "description",
    "required",
    "evidence_path",
    "source_policy",
    "freshness_policy",
    "identity_policy",
    "min_sources",
    "accepted_source_ids",
    "blocking_request_ids",
    "facet_verdict",
}
OPTIONAL_FACET_FIELDS = {
    "accepted_artifact_kinds",
    "claim_probe",
}
TEMPLATE_FIELDS = {"coverage_profile", "required_facets", "optional_facets"}
TEMPLATE_FACET_FIELDS = {
    "facet_id",
    "description",
    "required",
    "evidence_path",
    "source_policy",
    "freshness_policy",
    "identity_policy",
    "min_sources",
    "accepted_source_ids",
    "blocking_request_ids",
    "facet_verdict",
    "accepted_artifact_kinds",
    "claim_probe",
}
CLAIM_PROBE_LIMITATION = "not found in configured providers for this bounded run; not a global nonexistence claim"
CLAIM_PROBE_REQUIRED_FIELDS = {
    "claim_type",
    "claim_text",
    "claim_verdict",
    "limitation",
    "bounded_provider_results",
}
CLAIM_PROBE_RESULT_FIELDS = {
    "provider",
    "query",
    "max_results",
    "result_count",
    "exact_match_count",
    "network_io_executed",
}
ALLOWED_CLAIM_PROBE_TYPES = {"method_or_artifact_existence"}
ALLOWED_CLAIM_PROBE_VERDICTS = {"unconfirmed"}
ALLOWED_CLAIM_PROBE_PROVIDERS = {"arxiv", "openalex"}
ALLOWED_ARTIFACT_KINDS = {
    "source_archive",
    "repository_metadata",
    "release_metadata",
}
ALLOWED_COVERAGE_VERDICTS = {"pending", "pass", "blocked"}
ALLOWED_FACET_VERDICTS = {"pending", "pass", "blocked", "not_applicable"}
ALLOWED_EVIDENCE_PATHS = {
    "legal_current_figure",
    "academic_method_existence",
    "github_implementation",
    "official_guidance",
    "standards_registry_reference",
    "product_requirement_profile",
    "vendor_product_spec",
}
ALLOWED_SOURCE_POLICIES = {
    "official_primary",
    "primary_or_official",
    "academic_indexed",
    "openalex_or_arxiv",
    "canonical_repository",
    "official_vendor",
    "official_standards_registry",
    "standards_body_primary",
    "domain_pack_allowed",
    "manual_review_required",
}
ALLOWED_FRESHNESS_POLICIES = {
    "current_legal_figure",
    "current_product_spec",
    "current_standard_reference",
    "current_product_requirement",
    "publication_identity",
    "release_snapshot",
    "no_staleness_check",
    "manual_review",
}
ALLOWED_IDENTITY_POLICIES = {
    "citation_id_resolves",
    "origin_url_matches_candidate",
    "repo_ref_resolves",
    "official_domain_match",
    "standard_designation_matches_registry",
    "registry_entry_matches_product_requirement",
    "none",
}
POLICY_VOCABULARY_FIELDS = {
    "evidence_paths": "evidence_path",
    "source_policy": "source_policy",
    "freshness_policy": "freshness_policy",
    "identity_policy": "identity_policy",
}
PACK_POLICY_ID_RE = re.compile(r"^pack:[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$")
BASE_POLICY_DEFINITIONS = {
    "evidence_paths": {
        "legal_current_figure": "Current legal, tax, fee, threshold, deadline, benefit, or regulatory figure.",
        "academic_method_existence": "Named paper, method, dataset, benchmark, or artifact existence in scholarly evidence.",
        "github_implementation": "Code, implementation, release, or repository evidence tied to a canonical repository.",
        "official_guidance": (
            "Official operational, safety, response, standards-body, or best-practice guidance "
            "where the claim is the guidance itself rather than a current legal figure or academic citation."
        ),
        "standards_registry_reference": "Official standards registry metadata for designation, edition, status, and registry identity.",
        "product_requirement_profile": "Product-compliance requirement guidance, harmonised-standard linkage, or legal product profile metadata.",
        "vendor_product_spec": "Product, service, hardware, software, or API capability from a vendor-controlled source.",
    },
    "source_policy": {
        "official_primary": "Primary authority of record.",
        "primary_or_official": "Primary source or official aggregator that republishes authoritative source material.",
        "academic_indexed": "Scholarly index, publisher, DOI resolver, arXiv record, OpenAlex record, or equivalent bibliographic index.",
        "openalex_or_arxiv": "Scholarly evidence narrowed to OpenAlex or arXiv-backed metadata.",
        "canonical_repository": "Canonical project repository, owner namespace, release page, or commit/tag source.",
        "official_vendor": "Vendor-owned page, documentation source, support page, release note, or equivalent official product source.",
        "official_standards_registry": "Official standards-body, government register, OJEU, EUR-Lex, or recognized registry source for the standards claim.",
        "standards_body_primary": "The standards body's own catalogue, open-data, browsing, or publication record for the referenced standard.",
        "domain_pack_allowed": "Domain-pack-defined source family that has been reviewed as acceptable for that domain.",
        "manual_review_required": "Cannot pass on automation alone; a reviewer must inspect and record acceptance.",
    },
    "freshness_policy": {
        "current_legal_figure": "Current legal/regulatory/tax figure using retrieval and validity/effective/publication metadata.",
        "current_product_spec": "Currently published product, service, API, or vendor capability using retrieval metadata and a date signal.",
        "current_standard_reference": "Standards registry metadata shows a current or published reference without unresolved replacement status.",
        "current_product_requirement": "Product requirement metadata includes retrieval plus publication, validity, OJEU, or equivalent currentness signal.",
        "publication_identity": "Bibliographic publication identity rather than currentness.",
        "release_snapshot": "Stable release, tag, commit, package version, or repository ref.",
        "no_staleness_check": "No deterministic freshness check is required for this facet.",
        "manual_review": "Freshness cannot be determined locally and must be reviewed manually.",
    },
    "identity_policy": {
        "citation_id_resolves": "Local metadata records a valid DOI, arXiv ID, OpenAlex work ID, PMID, or PMCID plus title metadata.",
        "origin_url_matches_candidate": "Normalized source origin matches the reviewed discovery candidate or selected acquisition request.",
        "repo_ref_resolves": "Repository ref, tag, release, commit, or package version resolves in the canonical repository.",
        "official_domain_match": "Source origin matches an allowed official domain or jurisdiction profile.",
        "standard_designation_matches_registry": "The cited standard designation and edition/year exactly match recorded registry metadata.",
        "registry_entry_matches_product_requirement": "The registry entry links to the declared product category, legal act, OJEU/harmonised reference, or equivalent requirement metadata.",
        "none": "No additional identity check is required beyond the accepted source record.",
    },
}
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_module_loader import load_workspace_module

_script_errors = load_workspace_module(_SCRIPT_DIR, "_script_errors")
emit_error = _script_errors.emit_error
handle_system_exit = _script_errors.handle_system_exit
json_mode_requested = _script_errors.json_mode_requested


class CoverageManifestError(Exception):
    """A refused coverage-manifest operation with a stable machine code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        exit_code: int = EXIT_INVALID,
        recoverable: bool | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.exit_code = exit_code
        self.recoverable = recoverable
        self.remediation = remediation
        self.details = details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage per-question coverage manifests.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a coverage manifest for one question slug.")
    init_parser.add_argument("--slug", required=True, help="Question slug (file name without .md).")
    init_parser.add_argument("--coverage-profile", default=None, help="Coverage profile name for an untemplated manifest.")
    init_parser.add_argument("--template", default=None, help="Optional YAML coverage template path.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing coverage manifest.")
    add_format_arg(init_parser)

    show_parser = subparsers.add_parser("show", help="Print one coverage manifest.")
    show_parser.add_argument("--slug", required=True, help="Question slug (file name without .md).")
    add_format_arg(show_parser)

    validate_parser = subparsers.add_parser("validate", help="Validate one coverage manifest.")
    validate_parser.add_argument("--slug", required=True, help="Question slug (file name without .md).")
    add_format_arg(validate_parser)

    set_facet_parser = subparsers.add_parser("set-facet", help="Update accepted source or blocking request IDs.")
    set_facet_parser.add_argument("--slug", required=True, help="Question slug (file name without .md).")
    set_facet_parser.add_argument("--facet-id", required=True, help="Facet identifier to update.")
    set_facet_parser.add_argument(
        "--accepted-source-id",
        action="append",
        default=None,
        help="Manifest source id accepted for this facet. Repeatable.",
    )
    set_facet_parser.add_argument(
        "--blocking-request-id",
        action="append",
        default=None,
        help="Source request id blocking this facet. Repeatable.",
    )
    set_facet_parser.add_argument(
        "--clear-accepted-source-ids",
        action="store_true",
        help="Clear accepted source ids before adding any provided --accepted-source-id values.",
    )
    set_facet_parser.add_argument(
        "--clear-blocking-request-ids",
        action="store_true",
        help="Clear blocking request ids before adding any provided --blocking-request-id values.",
    )
    add_format_arg(set_facet_parser)

    evaluate_parser = subparsers.add_parser("evaluate", help="Evaluate facet and top-level coverage verdicts.")
    evaluate_parser.add_argument("--slug", required=True, help="Question slug (file name without .md).")
    add_format_arg(evaluate_parser)

    return parser.parse_args(argv)


def add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format. Defaults to text.")


def load_sibling_module(stem: str) -> ModuleType:
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.exists():
        raise SystemExit(f"Missing config: {path}")
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CoverageManifestError("CONFIG_INVALID", f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise CoverageManifestError("CONFIG_INVALID", f"Invalid config: {path}")
    return config


def base_policy_vocabularies() -> dict[str, dict[str, str]]:
    return {field: dict(definitions) for field, definitions in BASE_POLICY_DEFINITIONS.items()}


def normalize_policy_vocabulary_declarations(value: Any, *, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CoverageManifestError(
            "CONFIG_INVALID",
            f"{label} must be a mapping of namespaced policy id to definition text",
        )
    declarations: dict[str, str] = {}
    for raw_id, raw_definition in value.items():
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise CoverageManifestError("CONFIG_INVALID", f"{label} keys must be non-empty strings")
        policy_id = raw_id.strip()
        if PACK_POLICY_ID_RE.fullmatch(policy_id) is None:
            raise CoverageManifestError(
                "CONFIG_INVALID",
                f"{label}.{policy_id} must use a namespaced id like pack:name/policy-id",
                details={"policy_id": policy_id},
            )
        if not isinstance(raw_definition, str) or not raw_definition.strip():
            raise CoverageManifestError(
                "CONFIG_INVALID",
                f"{label}.{policy_id} must define non-empty policy text",
                details={"policy_id": policy_id},
            )
        declarations[policy_id] = raw_definition.strip()
    return declarations


def domain_pack_policy_vocabularies(config: dict[str, Any]) -> dict[str, dict[str, str]]:
    domain_pack = config.get("domain_pack") if isinstance(config.get("domain_pack"), dict) else {}
    raw_vocabularies = domain_pack.get("policy_vocabularies") if isinstance(domain_pack, dict) else None
    if raw_vocabularies is None:
        return {field: {} for field in POLICY_VOCABULARY_FIELDS}
    if not isinstance(raw_vocabularies, dict):
        raise CoverageManifestError("CONFIG_INVALID", "domain_pack.policy_vocabularies must be a mapping")
    unknown = sorted(set(raw_vocabularies) - set(POLICY_VOCABULARY_FIELDS))
    if unknown:
        raise CoverageManifestError(
            "CONFIG_INVALID",
            f"domain_pack.policy_vocabularies contains unknown section(s): {', '.join(unknown)}",
        )
    return {
        field: normalize_policy_vocabulary_declarations(
            raw_vocabularies.get(field),
            label=f"domain_pack.policy_vocabularies.{field}",
        )
        for field in POLICY_VOCABULARY_FIELDS
    }


def merged_policy_vocabularies(config: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    merged = base_policy_vocabularies()
    if isinstance(config, dict):
        pack_vocabularies = domain_pack_policy_vocabularies(config)
        for field, definitions in pack_vocabularies.items():
            collisions = sorted(set(merged.get(field, {})) & set(definitions))
            if collisions:
                raise CoverageManifestError(
                    "CONFIG_INVALID",
                    f"domain_pack.policy_vocabularies.{field} redefines base policy id(s): {', '.join(collisions)}",
                )
            merged.setdefault(field, {}).update(definitions)
    return merged


def allowed_policies_for_field(field: str, policy_vocabularies: dict[str, dict[str, str]] | None = None) -> set[str]:
    if policy_vocabularies is None:
        policy_vocabularies = base_policy_vocabularies()
    return set(policy_vocabularies.get(field, {}))


def validate_workspace_relative_path(value: Any, label: str, *, must_be_sources: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoverageManifestError("CONFIG_INVALID", f"research.yml {label} must be a non-empty workspace-relative path")
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise CoverageManifestError("CONFIG_INVALID", f"research.yml {label} must be a workspace-relative path, not a URL: {value}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise CoverageManifestError("CONFIG_INVALID", f"research.yml {label} must not be an absolute path: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise CoverageManifestError("CONFIG_INVALID", f"research.yml {label} must be a workspace-relative path without '..': {value}")
    relative = path.as_posix()
    if must_be_sources and relative != "sources" and not relative.startswith("sources/"):
        raise CoverageManifestError("CONFIG_INVALID", f"research.yml {label} must be under the generated sources/ directory: {value}")
    return relative


def coverage_dir(project_root: Path, config: dict[str, Any]) -> Path:
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    value = sources.get("coverage_dir", DEFAULT_COVERAGE_DIR)
    return project_root / validate_workspace_relative_path(value, "sources.coverage_dir", must_be_sources=True)


def wiki_questions_dir(project_root: Path, config: dict[str, Any]) -> Path:
    wiki = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    root = wiki.get("root", "wiki")
    return project_root / validate_workspace_relative_path(root, "wiki.root") / "questions"


def validate_slug(value: str) -> str:
    slug = value.strip() if isinstance(value, str) else ""
    normalized = slug.replace("\\", "/")
    parsed = urlparse(normalized)
    if (
        not slug
        or slug != value
        or slug in {".", ".."}
        or "/" in normalized
        or ".." in PurePosixPath(normalized).parts
        or "://" in normalized
        or parsed.scheme
        or (len(slug) >= 2 and slug[1] == ":" and slug[0].isalpha())
        or SLUG_RE.fullmatch(slug) is None
    ):
        raise CoverageManifestError(
            "SLUG_INVALID",
            f"invalid question slug: {value}",
            details={"slug": value},
        )
    return slug


def ensure_question_exists(project_root: Path, config: dict[str, Any], slug: str) -> Path:
    path = wiki_questions_dir(project_root, config) / f"{slug}.md"
    if not path.is_file():
        raise CoverageManifestError(
            "SLUG_UNKNOWN",
            f"Unknown question slug: {slug}",
            details={"slug": slug},
        )
    return path


def manifest_path(project_root: Path, config: dict[str, Any], slug: str) -> Path:
    return coverage_dir(project_root, config) / f"{slug}.yml"


def validate_manifest_path_value(value: str, label: str) -> str:
    raw = value.strip()
    if not raw:
        raise CoverageManifestError(
            "COVERAGE_MANIFEST_INVALID",
            f"{label} must be a non-empty workspace-relative path",
        )
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise CoverageManifestError(
            "COVERAGE_MANIFEST_INVALID",
            f"{label} must be workspace-relative, not a URL: {value}",
            details={"manifest_path": value},
        )
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise CoverageManifestError(
            "COVERAGE_MANIFEST_INVALID",
            f"{label} must not be an absolute path: {value}",
            details={"manifest_path": value},
        )
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise CoverageManifestError(
            "COVERAGE_MANIFEST_INVALID",
            f"{label} must be a workspace-relative path without '..': {value}",
            details={"manifest_path": value},
        )
    return path.as_posix()


def selected_manifest_path(project_root: Path, config: dict[str, Any], slug: str, value: str | None = None) -> Path:
    coverage_root = coverage_dir(project_root, config).resolve()
    if value is None:
        return coverage_root / f"{slug}.yml"
    relative = validate_manifest_path_value(value, "coverage_manifest")
    target = (project_root / relative).resolve()
    if target != coverage_root and coverage_root not in target.parents:
        raise CoverageManifestError(
            "COVERAGE_MANIFEST_INVALID",
            f"coverage_manifest must be under {relative_workspace_path(project_root, coverage_root)}: {value}",
            details={"manifest_path": relative},
        )
    return target


def relative_workspace_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def load_yaml_mapping(path: Path, *, error_code: str) -> dict[str, Any]:
    if not path.is_file():
        raise CoverageManifestError(error_code, f"Missing coverage manifest: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CoverageManifestError(error_code, f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise CoverageManifestError(error_code, f"Invalid YAML mapping in {path}")
    return document


def load_manifest(project_root: Path, config: dict[str, Any], slug: str) -> tuple[Path, dict[str, Any]]:
    path = manifest_path(project_root, config, slug)
    document = load_yaml_mapping(path, error_code="COVERAGE_MANIFEST_INVALID")
    validate_manifest(document, expected_slug=slug, policy_vocabularies=merged_policy_vocabularies(config))
    return path, document


def write_manifest(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    tmp_path.replace(path)


def string_field(document: dict[str, Any], key: str, label: str, *, error_code: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CoverageManifestError(error_code, f"{label}.{key} must be a non-empty string")
    return value.strip()


def string_list_field(document: dict[str, Any], key: str, label: str, *, error_code: str) -> list[str]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise CoverageManifestError(error_code, f"{label}.{key} must be a list of non-empty strings")
    return [item.strip() for item in value]


def probe_error(message: str, *, details: dict[str, Any] | None = None) -> CoverageManifestError:
    return CoverageManifestError(
        "COVERAGE_CLAIM_PROBE_INVALID",
        message,
        remediation=(
            "Record bounded arXiv/OpenAlex non-confirmation as an unconfirmed method_or_artifact_existence "
            "claim with the required limitation text."
        ),
        details=details,
    )


def nonnegative_int_field(document: dict[str, Any], key: str, label: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise probe_error(f"{label}.{key} must be a non-negative integer")
    return value


def validate_claim_probe_result(result: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise probe_error(f"{label}.bounded_provider_results entries must be mappings")
    missing = CLAIM_PROBE_RESULT_FIELDS - set(result)
    if missing:
        raise probe_error(
            f"{label}.bounded_provider_results entry missing fields: {', '.join(sorted(missing))}"
        )
    unknown = set(result) - CLAIM_PROBE_RESULT_FIELDS
    if unknown:
        raise probe_error(
            f"{label}.bounded_provider_results entry has unknown fields: {', '.join(sorted(unknown))}"
        )
    provider = string_field(result, "provider", label, error_code="COVERAGE_CLAIM_PROBE_INVALID")
    if provider not in ALLOWED_CLAIM_PROBE_PROVIDERS:
        raise probe_error(
            f"{label}.bounded_provider_results provider has unknown value: {provider}",
            details={"provider": provider, "allowed": sorted(ALLOWED_CLAIM_PROBE_PROVIDERS)},
        )
    string_field(result, "query", label, error_code="COVERAGE_CLAIM_PROBE_INVALID")
    max_results = nonnegative_int_field(result, "max_results", label)
    if max_results < 1:
        raise probe_error(f"{label}.max_results must be at least 1")
    result_count = nonnegative_int_field(result, "result_count", label)
    exact_match_count = nonnegative_int_field(result, "exact_match_count", label)
    if exact_match_count > result_count:
        raise probe_error(f"{label}.exact_match_count must not exceed result_count")
    if not isinstance(result.get("network_io_executed"), bool):
        raise probe_error(f"{label}.network_io_executed must be a boolean")
    return result


def validate_claim_probe(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise probe_error(f"{label}.claim_probe must be a mapping")
    missing = CLAIM_PROBE_REQUIRED_FIELDS - set(value)
    if missing:
        raise probe_error(f"{label}.claim_probe missing fields: {', '.join(sorted(missing))}")
    unknown = set(value) - CLAIM_PROBE_REQUIRED_FIELDS
    if unknown:
        raise probe_error(f"{label}.claim_probe has unknown fields: {', '.join(sorted(unknown))}")
    claim_type = string_field(value, "claim_type", label, error_code="COVERAGE_CLAIM_PROBE_INVALID")
    if claim_type not in ALLOWED_CLAIM_PROBE_TYPES:
        raise probe_error(
            f"{label}.claim_probe claim_type has unknown value: {claim_type}",
            details={"claim_type": claim_type, "allowed": sorted(ALLOWED_CLAIM_PROBE_TYPES)},
        )
    string_field(value, "claim_text", label, error_code="COVERAGE_CLAIM_PROBE_INVALID")
    claim_verdict = string_field(value, "claim_verdict", label, error_code="COVERAGE_CLAIM_PROBE_INVALID")
    if claim_verdict not in ALLOWED_CLAIM_PROBE_VERDICTS:
        raise probe_error(
            f"{label}.claim_probe claim_verdict has unknown value: {claim_verdict}",
            details={"claim_verdict": claim_verdict, "allowed": sorted(ALLOWED_CLAIM_PROBE_VERDICTS)},
        )
    limitation = string_field(value, "limitation", label, error_code="COVERAGE_CLAIM_PROBE_INVALID")
    if limitation != CLAIM_PROBE_LIMITATION:
        raise probe_error(
            f"{label}.claim_probe limitation must be exactly: {CLAIM_PROBE_LIMITATION!r}",
            details={"limitation": limitation},
        )
    results = value.get("bounded_provider_results")
    if not isinstance(results, list) or not results:
        raise probe_error(f"{label}.claim_probe bounded_provider_results must be a non-empty list")
    normalized_results = [validate_claim_probe_result(result, label=label) for result in results]
    providers = [result["provider"] for result in normalized_results]
    if len(set(providers)) != len(providers):
        raise probe_error(f"{label}.claim_probe bounded_provider_results must not repeat providers")
    if set(providers) != ALLOWED_CLAIM_PROBE_PROVIDERS:
        raise probe_error(
            f"{label}.claim_probe bounded_provider_results must include arxiv and openalex",
            details={"providers": providers, "required": sorted(ALLOWED_CLAIM_PROBE_PROVIDERS)},
        )
    if any(result["exact_match_count"] > 0 for result in normalized_results):
        raise probe_error(f"{label}.claim_probe unconfirmed results must have zero exact matches")
    return value


def validate_policy(value: str, allowed: set[str], label: str, field: str, *, error_code: str) -> None:
    if value not in allowed:
        raise CoverageManifestError(
            "COVERAGE_POLICY_UNKNOWN",
            f"{label}.{field} has unknown policy identifier: {value}",
            details={"field": field, "value": value, "allowed": sorted(allowed)},
        )
    if error_code == "COVERAGE_TEMPLATE_INVALID":
        return


def validate_manifest(
    document: dict[str, Any],
    *,
    expected_slug: str | None = None,
    policy_vocabularies: dict[str, dict[str, str]] | None = None,
) -> None:
    missing = REQUIRED_TOP_LEVEL_FIELDS - set(document)
    if missing:
        raise CoverageManifestError("COVERAGE_MANIFEST_INVALID", f"coverage manifest missing fields: {', '.join(sorted(missing))}")
    unknown = set(document) - REQUIRED_TOP_LEVEL_FIELDS
    if unknown:
        raise CoverageManifestError("COVERAGE_MANIFEST_INVALID", f"coverage manifest has unknown fields: {', '.join(sorted(unknown))}")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise CoverageManifestError("COVERAGE_MANIFEST_INVALID", f"coverage manifest schema_version must be {SCHEMA_VERSION}")
    slug = string_field(document, "question_slug", "coverage manifest", error_code="COVERAGE_MANIFEST_INVALID")
    if expected_slug is not None and slug != expected_slug:
        raise CoverageManifestError(
            "COVERAGE_MANIFEST_INVALID",
            f"coverage manifest question_slug {slug!r} does not match expected slug {expected_slug!r}",
        )
    validate_slug(slug)
    for key in ("created_at", "updated_at"):
        value = string_field(document, key, "coverage manifest", error_code="COVERAGE_MANIFEST_INVALID")
        if UTC_TIMESTAMP_RE.fullmatch(value) is None:
            raise CoverageManifestError("COVERAGE_MANIFEST_INVALID", f"coverage manifest {key} must be a UTC timestamp")
    string_field(document, "coverage_profile", "coverage manifest", error_code="COVERAGE_MANIFEST_INVALID")
    if document["coverage_verdict"] not in ALLOWED_COVERAGE_VERDICTS:
        raise CoverageManifestError("COVERAGE_MANIFEST_INVALID", f"coverage_verdict must be one of: {', '.join(sorted(ALLOWED_COVERAGE_VERDICTS))}")
    for key in ("required_facets", "optional_facets"):
        if not isinstance(document.get(key), list):
            raise CoverageManifestError("COVERAGE_MANIFEST_INVALID", f"{key} must be a list")
    for facet in document["required_facets"]:
        validate_facet(
            facet,
            expected_required=True,
            label="required_facets",
            error_code="COVERAGE_MANIFEST_INVALID",
            policy_vocabularies=policy_vocabularies,
        )
    for facet in document["optional_facets"]:
        validate_facet(
            facet,
            expected_required=False,
            label="optional_facets",
            error_code="COVERAGE_MANIFEST_INVALID",
            policy_vocabularies=policy_vocabularies,
        )


def validate_facet(
    facet: Any,
    *,
    expected_required: bool,
    label: str,
    error_code: str,
    policy_vocabularies: dict[str, dict[str, str]] | None = None,
) -> None:
    if not isinstance(facet, dict):
        raise CoverageManifestError(error_code, f"{label} entries must be mappings")
    missing = REQUIRED_FACET_FIELDS - set(facet)
    if missing:
        raise CoverageManifestError(error_code, f"{label} entry missing fields: {', '.join(sorted(missing))}")
    unknown = set(facet) - REQUIRED_FACET_FIELDS - OPTIONAL_FACET_FIELDS
    if unknown:
        raise CoverageManifestError(error_code, f"{label} entry has unknown fields: {', '.join(sorted(unknown))}")
    for key in ("facet_id", "description", "evidence_path", "source_policy", "freshness_policy", "identity_policy", "facet_verdict"):
        string_field(facet, key, label, error_code=error_code)
    if not isinstance(facet.get("required"), bool) or facet["required"] is not expected_required:
        raise CoverageManifestError(error_code, f"{label} entry required must be {expected_required}")
    min_sources = facet.get("min_sources")
    if isinstance(min_sources, bool) or not isinstance(min_sources, int) or min_sources < 0:
        raise CoverageManifestError(error_code, f"{label} entry min_sources must be a non-negative integer")
    validate_policy(
        facet["evidence_path"],
        allowed_policies_for_field("evidence_paths", policy_vocabularies),
        label,
        "evidence_path",
        error_code=error_code,
    )
    validate_policy(
        facet["source_policy"],
        allowed_policies_for_field("source_policy", policy_vocabularies),
        label,
        "source_policy",
        error_code=error_code,
    )
    validate_policy(
        facet["freshness_policy"],
        allowed_policies_for_field("freshness_policy", policy_vocabularies),
        label,
        "freshness_policy",
        error_code=error_code,
    )
    validate_policy(
        facet["identity_policy"],
        allowed_policies_for_field("identity_policy", policy_vocabularies),
        label,
        "identity_policy",
        error_code=error_code,
    )
    if facet["facet_verdict"] not in ALLOWED_FACET_VERDICTS:
        raise CoverageManifestError(error_code, f"{label} entry facet_verdict must be one of: {', '.join(sorted(ALLOWED_FACET_VERDICTS))}")
    string_list_field(facet, "accepted_source_ids", label, error_code=error_code)
    string_list_field(facet, "blocking_request_ids", label, error_code=error_code)
    if "accepted_artifact_kinds" in facet:
        kinds = string_list_field(facet, "accepted_artifact_kinds", label, error_code=error_code)
        unknown_kinds = sorted(set(kinds) - ALLOWED_ARTIFACT_KINDS)
        if unknown_kinds:
            raise CoverageManifestError(
                error_code,
                f"{label} entry accepted_artifact_kinds contains unknown value(s): {', '.join(unknown_kinds)}",
            )
    if "claim_probe" in facet:
        validate_claim_probe(facet["claim_probe"], label=label)
        if facet["accepted_source_ids"]:
            raise probe_error(f"{label}.claim_probe cannot be unconfirmed when accepted_source_ids is non-empty")


def normalize_template_facet(
    facet: Any,
    *,
    required: bool,
    policy_vocabularies: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    label = "required_facets" if required else "optional_facets"
    if not isinstance(facet, dict):
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", f"{label} entries must be mappings")
    unknown = set(facet) - TEMPLATE_FACET_FIELDS
    if unknown:
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", f"{label} entry has unknown fields: {', '.join(sorted(unknown))}")
    normalized = dict(facet)
    normalized["required"] = required if "required" not in normalized else normalized["required"]
    normalized.setdefault("accepted_source_ids", [])
    normalized.setdefault("blocking_request_ids", [])
    normalized.setdefault("facet_verdict", "pending")
    validate_facet(
        normalized,
        expected_required=required,
        label=label,
        error_code="COVERAGE_TEMPLATE_INVALID",
        policy_vocabularies=policy_vocabularies,
    )
    return normalized


def load_template(
    path_value: str,
    *,
    base_dir: Path | None = None,
    policy_vocabularies: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", f"Missing coverage template: {path_value}") from exc
    except OSError as exc:
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", f"Cannot read coverage template {path_value}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", f"Invalid YAML in coverage template {path_value}: {exc}") from exc
    if not isinstance(document, dict):
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", f"coverage template must be a YAML mapping: {path_value}")
    unknown = set(document) - TEMPLATE_FIELDS
    if unknown:
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", f"coverage template has unknown fields: {', '.join(sorted(unknown))}")
    required = document.get("required_facets", [])
    optional = document.get("optional_facets", [])
    if not isinstance(required, list) or not isinstance(optional, list):
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", "coverage template facets must be lists")
    profile = document.get("coverage_profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise CoverageManifestError("COVERAGE_TEMPLATE_INVALID", "coverage template coverage_profile must be a non-empty string")
    return {
        "coverage_profile": profile.strip() if isinstance(profile, str) else None,
        "required_facets": [
            normalize_template_facet(facet, required=True, policy_vocabularies=policy_vocabularies)
            for facet in required
        ],
        "optional_facets": [
            normalize_template_facet(facet, required=False, policy_vocabularies=policy_vocabularies)
            for facet in optional
        ],
    }


def build_manifest(slug: str, coverage_profile: str | None, template: dict[str, Any] | None) -> dict[str, Any]:
    now = timestamp_utc()
    profile = coverage_profile or (template or {}).get("coverage_profile") or DEFAULT_COVERAGE_PROFILE
    if not isinstance(profile, str) or not profile.strip():
        raise CoverageManifestError("VALUE_INVALID", "--coverage-profile must be non-empty")
    return {
        "schema_version": SCHEMA_VERSION,
        "question_slug": slug,
        "created_at": now,
        "updated_at": now,
        "coverage_profile": profile.strip(),
        "coverage_verdict": "pending",
        "required_facets": list((template or {}).get("required_facets", [])),
        "optional_facets": list((template or {}).get("optional_facets", [])),
    }


def unique_nonempty(values: list[str] | None, label: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values or []:
        value = raw.strip()
        if not value:
            raise CoverageManifestError("VALUE_INVALID", f"{label} must not be empty")
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def merge_unique(existing: list[str], additions: list[str], *, clear: bool) -> list[str]:
    result = [] if clear else list(existing)
    for value in additions:
        if value not in result:
            result.append(value)
    return result


def validate_source_ids(project_root: Path, config: dict[str, Any], source_ids: list[str]) -> None:
    if not source_ids:
        return
    source_requests = load_sibling_module("source_requests")
    valid_ids = source_requests.manifest_source_ids(project_root, config)
    for source_id in source_ids:
        if source_id not in valid_ids:
            raise CoverageManifestError(
                "SOURCE_UNKNOWN",
                f"Unknown source id: {source_id} (not in the manifest)",
                details={"source_id": source_id},
            )


def validate_request_ids(project_root: Path, config: dict[str, Any], slug: str, request_ids: list[str]) -> None:
    if not request_ids:
        return
    source_requests = load_sibling_module("source_requests")
    path = source_requests.requests_path(project_root, config)
    records = source_requests.load_requests(path)
    by_id = {
        record.get("request_id"): record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("request_id"), str) and record.get("request_id")
    }
    for request_id in request_ids:
        record = by_id.get(request_id)
        if record is None:
            raise CoverageManifestError(
                "REQUEST_UNKNOWN",
                f"Unknown request id: {request_id}",
                details={"request_id": request_id},
            )
        slugs = record.get("question_slugs")
        if isinstance(slugs, list) and slugs and slug not in [item for item in slugs if isinstance(item, str)]:
            raise CoverageManifestError(
                "REQUEST_NOT_LINKED",
                f"source request {request_id} does not reference question slug {slug}",
                details={"request_id": request_id, "slug": slug},
            )


def all_facets(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [*document["required_facets"], *document["optional_facets"]]


def find_facet(document: dict[str, Any], facet_id: str) -> dict[str, Any]:
    for facet in all_facets(document):
        if facet.get("facet_id") == facet_id:
            return facet
    raise CoverageManifestError(
        "COVERAGE_FACET_UNKNOWN",
        f"Unknown coverage facet: {facet_id}",
        details={"facet_id": facet_id},
    )


def evaluate_facet(facet: dict[str, Any], policy_results: list[dict[str, Any]] | None = None) -> str:
    accepted_count = len(facet.get("accepted_source_ids", []))
    blockers = facet.get("blocking_request_ids", [])
    min_sources = int(facet.get("min_sources", 0))
    if not facet.get("required") and min_sources == 0 and not blockers:
        if accepted_count == 0:
            return "not_applicable"
        return "pass"
    if accepted_count >= min_sources and not blockers:
        if (
            facet.get("required")
            and policy_results is not None
            and policy_results_block_required_facet(policy_results)
        ):
            return "blocked"
        return "pass"
    return "blocked"


def policy_results_block_required_facet(policy_results: list[dict[str, Any]]) -> bool:
    for result in policy_results:
        if result.get("verdict") == "fail":
            return True
    return False


def update_evaluation_fields(
    document: dict[str, Any],
    *,
    update_timestamp: bool,
    policy_results_by_facet: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    policy_results_by_facet = policy_results_by_facet or {}
    for facet in all_facets(document):
        facet_id = facet.get("facet_id")
        policy_results = policy_results_by_facet.get(facet_id) if isinstance(facet_id, str) else None
        facet["facet_verdict"] = evaluate_facet(facet, policy_results)
    required = document["required_facets"]
    if not required:
        document["coverage_verdict"] = "pending"
    elif any(facet["facet_verdict"] == "blocked" for facet in required):
        document["coverage_verdict"] = "blocked"
    elif all(facet["facet_verdict"] == "pass" for facet in required):
        document["coverage_verdict"] = "pass"
    else:
        document["coverage_verdict"] = "pending"
    if update_timestamp:
        document["updated_at"] = timestamp_utc()
    return document


def evaluate_manifest(
    document: dict[str, Any],
    policy_results_by_facet: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    return update_evaluation_fields(document, update_timestamp=True, policy_results_by_facet=policy_results_by_facet)


def evaluate_manifest_readonly(
    document: dict[str, Any],
    policy_results_by_facet: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    return update_evaluation_fields(
        copy.deepcopy(document),
        update_timestamp=False,
        policy_results_by_facet=policy_results_by_facet,
    )


def facet_summary(facet: dict[str, Any], policy_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    summary = {
        "facet_id": facet["facet_id"],
        "description": facet["description"],
        "required": facet["required"],
        "evidence_path": facet["evidence_path"],
        "source_policy": facet["source_policy"],
        "freshness_policy": facet["freshness_policy"],
        "identity_policy": facet["identity_policy"],
        "min_sources": facet["min_sources"],
        "accepted_source_ids": list(facet["accepted_source_ids"]),
        "blocking_request_ids": list(facet["blocking_request_ids"]),
        "facet_verdict": facet["facet_verdict"],
    }
    if "accepted_artifact_kinds" in facet:
        summary["accepted_artifact_kinds"] = list(facet["accepted_artifact_kinds"])
    if "claim_probe" in facet:
        summary["claim_probe"] = copy.deepcopy(facet["claim_probe"])
    if policy_results is not None:
        summary["policy_results"] = copy.deepcopy(policy_results)
    return summary


def evaluate_policy_results_for_manifest(
    project_root: Path,
    config: dict[str, Any],
    document: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    helper = load_sibling_module("_evidence_policies")
    inputs = helper.load_policy_inputs(project_root, config)
    facets: list[dict[str, Any]] = []
    by_facet: dict[str, list[dict[str, Any]]] = {}
    for facet in all_facets(document):
        facet_id = facet.get("facet_id")
        accepted_source_ids = facet.get("accepted_source_ids")
        policy_results: list[dict[str, Any]] = []
        if isinstance(facet_id, str) and isinstance(accepted_source_ids, list) and accepted_source_ids:
            policy_results = [result.to_dict() for result in helper.evaluate_facet_policies(facet, inputs)]
            by_facet[facet_id] = policy_results
        facets.append(
            {
                "facet_id": facet_id,
                "required": facet.get("required"),
                "evidence_path": facet.get("evidence_path"),
                "policy_results": policy_results,
            }
        )
    return (
        {
            "question_slug": document.get("question_slug"),
            "coverage_profile": document.get("coverage_profile"),
            "facets": facets,
        },
        by_facet,
    )


def unconfirmed_claims(document: dict[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for facet in all_facets(document):
        probe = facet.get("claim_probe")
        if not isinstance(probe, dict) or probe.get("claim_verdict") != "unconfirmed":
            continue
        claims.append(
            {
                "facet_id": facet["facet_id"],
                "description": facet["description"],
                "required": facet["required"],
                "evidence_path": facet["evidence_path"],
                "source_policy": facet["source_policy"],
                **copy.deepcopy(probe),
            }
        )
    return claims


def failed_required_facets(document: dict[str, Any]) -> list[str]:
    failed: list[str] = []
    for facet in document.get("required_facets", []):
        if not isinstance(facet, dict) or facet.get("facet_verdict") == "pass":
            continue
        facet_id = facet.get("facet_id")
        failed.append(facet_id if isinstance(facet_id, str) and facet_id else "<unknown>")
    return failed


def unique_blocking_request_ids(document: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    request_ids: list[str] = []
    for facet in all_facets(document):
        for request_id in facet.get("blocking_request_ids", []):
            if isinstance(request_id, str) and request_id and request_id not in seen:
                seen.add(request_id)
                request_ids.append(request_id)
    return request_ids


def source_request_records_by_id(project_root: Path, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source_requests = load_sibling_module("source_requests")
    try:
        records = source_requests.load_requests(source_requests.requests_path(project_root, config))
    except SystemExit:
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        request_id = record.get("request_id") if isinstance(record, dict) else None
        if isinstance(request_id, str) and request_id and request_id not in by_id:
            by_id[request_id] = record
    return by_id


def linked_request_state(
    project_root: Path,
    config: dict[str, Any],
    blocking_request_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = source_request_records_by_id(project_root, config)
    linked: list[dict[str, Any]] = []
    missing: list[str] = []
    for request_id in blocking_request_ids:
        record = by_id.get(request_id)
        if record is None:
            missing.append(request_id)
        else:
            linked.append(record)
    return linked, missing


def coverage_summary_for_question(
    project_root: Path,
    config: dict[str, Any],
    slug: str,
    frontmatter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frontmatter = frontmatter or {}
    coverage_required = frontmatter.get("coverage_required") is True
    raw_manifest = frontmatter.get("coverage_manifest")
    manifest_value = raw_manifest.strip() if isinstance(raw_manifest, str) and raw_manifest.strip() else None
    default_path = manifest_path(project_root, config, slug)
    should_probe_default = coverage_required or default_path.is_file()

    summary: dict[str, Any] = {
        "coverage_required": coverage_required,
        "coverage_manifest": None,
        "coverage_status": "not_required",
        "coverage_verdict": None,
        "coverage_facets": [],
        "failed_facets": [],
        "linked_source_requests": [],
        "missing_source_request_ids": [],
        "unconfirmed_claims": [],
    }
    if manifest_value is None and not should_probe_default:
        return summary

    try:
        path = selected_manifest_path(project_root, config, slug, manifest_value)
    except CoverageManifestError as exc:
        summary["coverage_status"] = "invalid"
        summary["coverage_manifest"] = manifest_value
        summary["error"] = str(exc)
        return summary
    manifest_label = relative_workspace_path(project_root, path)
    summary["coverage_manifest"] = manifest_label

    if not path.is_file():
        summary["coverage_status"] = "missing" if coverage_required else "not_required"
        return summary

    try:
        document = load_yaml_mapping(path, error_code="COVERAGE_MANIFEST_INVALID")
        policy_vocabularies = merged_policy_vocabularies(config)
        validate_manifest(document, expected_slug=slug, policy_vocabularies=policy_vocabularies)
        policy_results, policy_results_by_facet = evaluate_policy_results_for_manifest(project_root, config, document)
        evaluated = evaluate_manifest_readonly(document, policy_results_by_facet)
        validate_manifest(evaluated, expected_slug=slug, policy_vocabularies=policy_vocabularies)
    except CoverageManifestError as exc:
        summary["coverage_status"] = "invalid"
        summary["error"] = str(exc)
        return summary

    blocking_request_ids = unique_blocking_request_ids(evaluated)
    linked, missing = linked_request_state(project_root, config, blocking_request_ids)
    verdict = evaluated["coverage_verdict"]
    summary.update(
        {
            "coverage_status": verdict,
            "coverage_verdict": verdict,
            "coverage_facets": [
                facet_summary(facet, policy_results_by_facet.get(facet["facet_id"], []))
                for facet in all_facets(evaluated)
            ],
            "failed_facets": failed_required_facets(evaluated),
            "linked_source_requests": linked,
            "missing_source_request_ids": missing,
            "unconfirmed_claims": unconfirmed_claims(evaluated),
            "policy_results": policy_results,
        }
    )
    return summary


def coverage_manifest_count(project_root: Path, config: dict[str, Any]) -> int:
    try:
        directory = coverage_dir(project_root, config)
    except CoverageManifestError:
        return 0
    if not directory.is_dir():
        return 0
    return len([path for path in directory.glob("*.yml") if path.is_file()])


def report(action: str, project_root: Path, path: Path, document: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": True,
        "manifest_path": relative_workspace_path(project_root, path),
        "manifest": document,
        **extra,
    }


def run_init(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slug = validate_slug(args.slug)
    ensure_question_exists(project_root, config, slug)
    path = manifest_path(project_root, config, slug)
    if path.exists() and not args.force:
        raise CoverageManifestError(
            "COVERAGE_MANIFEST_EXISTS",
            f"coverage manifest already exists: {relative_workspace_path(project_root, path)}",
            details={"slug": slug, "manifest_path": relative_workspace_path(project_root, path)},
        )
    policy_vocabularies = merged_policy_vocabularies(config)
    template = (
        load_template(args.template, base_dir=project_root, policy_vocabularies=policy_vocabularies)
        if args.template
        else None
    )
    document = build_manifest(slug, args.coverage_profile, template)
    validate_manifest(document, expected_slug=slug, policy_vocabularies=policy_vocabularies)
    write_manifest(path, document)
    return report("init", project_root, path, document, created=True)


def run_show(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slug = validate_slug(args.slug)
    path, document = load_manifest(project_root, config, slug)
    return report("show", project_root, path, document)


def run_validate(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slug = validate_slug(args.slug)
    path, document = load_manifest(project_root, config, slug)
    return report("validate", project_root, path, document, valid=True)


def run_set_facet(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slug = validate_slug(args.slug)
    path, document = load_manifest(project_root, config, slug)
    facet_id = args.facet_id.strip()
    if not facet_id:
        raise CoverageManifestError("VALUE_INVALID", "--facet-id must be non-empty")
    accepted_source_ids = unique_nonempty(args.accepted_source_id, "--accepted-source-id")
    blocking_request_ids = unique_nonempty(args.blocking_request_id, "--blocking-request-id")
    validate_source_ids(project_root, config, accepted_source_ids)
    validate_request_ids(project_root, config, slug, blocking_request_ids)
    facet = find_facet(document, facet_id)
    facet["accepted_source_ids"] = merge_unique(
        facet["accepted_source_ids"],
        accepted_source_ids,
        clear=args.clear_accepted_source_ids,
    )
    facet["blocking_request_ids"] = merge_unique(
        facet["blocking_request_ids"],
        blocking_request_ids,
        clear=args.clear_blocking_request_ids,
    )
    document["updated_at"] = timestamp_utc()
    validate_manifest(document, expected_slug=slug, policy_vocabularies=merged_policy_vocabularies(config))
    write_manifest(path, document)
    return report("set-facet", project_root, path, document, facet_id=facet_id)


def run_evaluate(project_root: Path, config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    slug = validate_slug(args.slug)
    path, document = load_manifest(project_root, config, slug)
    policy_results, policy_results_by_facet = evaluate_policy_results_for_manifest(project_root, config, document)
    evaluate_manifest(document, policy_results_by_facet)
    validate_manifest(document, expected_slug=slug, policy_vocabularies=merged_policy_vocabularies(config))
    write_manifest(path, document)
    return report(
        "evaluate",
        project_root,
        path,
        document,
        coverage_verdict=document["coverage_verdict"],
        policy_results=policy_results,
    )


def render_text(result: dict[str, Any]) -> str:
    action = result["action"]
    slug = result["manifest"]["question_slug"]
    if action == "show":
        return yaml.safe_dump(result["manifest"], sort_keys=False)
    if action == "validate":
        return f"valid: {slug}\n"
    if action == "set-facet":
        return f"updated: {slug} facet {result['facet_id']}\n"
    if action == "evaluate":
        return f"{result['coverage_verdict']}: {slug}\n"
    return f"created: {result['manifest_path']}\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        config = load_config(project_root)
        if args.command == "init":
            result = run_init(project_root, config, args)
        elif args.command == "show":
            result = run_show(project_root, config, args)
        elif args.command == "validate":
            result = run_validate(project_root, config, args)
        elif args.command == "set-facet":
            result = run_set_facet(project_root, config, args)
        elif args.command == "evaluate":
            result = run_evaluate(project_root, config, args)
        else:  # pragma: no cover - argparse enforces subcommands
            raise CoverageManifestError("VALUE_INVALID", f"unknown command: {args.command}")
    except CoverageManifestError as error:
        emit_error(
            str(error),
            json_mode=json_mode,
            error_code=error.error_code,
            recoverable=error.recoverable,
            remediation=error.remediation,
            details=error.details,
        )
        return error.exit_code
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    if args.format == "json":
        print(json.dumps(result, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(result))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
