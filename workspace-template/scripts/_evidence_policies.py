#!/usr/bin/env python3
"""Local-only evidence policy evaluation helpers.

The helpers in this module read workspace artifacts and evaluate whether the
currently recorded metadata can satisfy coverage-manifest policy fields. They do
not fetch URLs, resolve identifiers over the network, clone repositories, or
mutate the workspace. Live re-resolution belongs in future explicit commands.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to evaluate evidence policies") from exc

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _workspace_module_loader import load_workspace_module

_source_failure_taxonomy = load_workspace_module(_SCRIPT_DIR, "source_failure_taxonomy")
delivery_unusable_evidence_reasons = _source_failure_taxonomy.unusable_evidence_reasons

VERDICT_OK = "pass"
VERDICT_FAIL = "fail"
VERDICT_MANUAL_REVIEW = "manual_review"
DEFAULT_MANIFEST_PATH = "sources/manifest.jsonl"
DEFAULT_NORMALIZED_DIR = "sources/normalized"
DEFAULT_COVERAGE_DIR = "sources/coverage"
DEFAULT_CANDIDATES_PATH = "sources/discovery/candidates.jsonl"
DEFAULT_JURISDICTIONS_PATH = "sources/jurisdictions.yml"

REPOSITORY_REF_KEYS = {
    "repository_ref",
    "repo_ref",
    "release_tag",
    "tag",
    "commit_sha",
    "commit",
    "version",
    "codebase_revision",
}
REPOSITORY_SELECTED_REF_KEYS = ("repository_ref", "repo_ref", "release_tag", "tag", "version", "codebase_revision")
REPOSITORY_ARTIFACT_KINDS = {"source_archive", "repository_metadata", "release_metadata"}
DEFAULT_REPOSITORY_ARTIFACT_KINDS = ("source_archive",)
CITATION_ID_KEYS = ("doi", "arxiv_id", "openalex_id", "pmid", "pmcid")
OPENALEX_OR_ARXIV_KEYS = ("arxiv_id", "openalex_id")
HOST_INDEX_PROVIDERS = {"arxiv.org", "openalex.org", "doi.org", "www.doi.org"}
GITHUB_HOSTS = {"github.com", "www.github.com"}
DOI_RE = re.compile(r"^10\.\S+/.+$", re.IGNORECASE)
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$", re.IGNORECASE)
OPENALEX_WORK_ID_RE = re.compile(r"^W\d+$", re.IGNORECASE)
PMID_RE = re.compile(r"^\d+$")
PMCID_RE = re.compile(r"^PMC\d+$", re.IGNORECASE)
BLOCKING_CURRENTNESS_SOURCE_STATUSES = {"error_page", "not_found", "unavailable"}
BLOCKING_CURRENTNESS_RECORD_STATUSES = {"rejected", "superseded"}
BLOCKING_CURRENTNESS_RISK_FLAGS = {"superseded_or_historical", "stale_source"}
STANDARD_CURRENT_STATUSES = {"active", "confirmed", "current", "in force", "in-force", "in_force", "published", "valid"}
STANDARD_WITHDRAWN_STATUS_TERMS = ("withdrawn",)
STANDARD_SUPERSEDED_STATUS_TERMS = ("replaced", "superseded", "obsolete")
STANDARD_DRAFT_STATUS_TERMS = ("committee draft", "draft international standard", "draft", "preliminary")
OFFICIAL_STANDARDS_REGISTRY_PROVIDERS = {
    "csrc",
    "eu-harmonised-standards",
    "european-commission",
    "eur-lex",
    "govuk-geospatial-register",
    "iso",
    "iso-open-data",
    "nist-csrc",
    "nist-standards-info",
    "ojeu",
    "standards.gov",
    "uk-geospatial-register",
}
PRODUCT_REQUIREMENT_LEGAL_PROVIDERS = {
    "eu-harmonised-standards",
    "european-commission",
    "eur-lex",
    "ojeu",
}
STANDARDS_BODY_DOMAINS = {
    "ansi": ("ansi.org",),
    "bsi": ("bsigroup.com",),
    "cen": ("cencenelec.eu",),
    "cenelec": ("cencenelec.eu",),
    "csrc": ("csrc.nist.gov", "nist.gov"),
    "etsi": ("etsi.org",),
    "iec": ("iec.ch",),
    "ieee": ("ieee.org",),
    "ietf": ("ietf.org", "rfc-editor.org"),
    "iso": ("iso.org",),
    "nist": ("nist.gov", "csrc.nist.gov"),
    "ogc": ("ogc.org",),
}
STANDARDS_SOURCE_TYPES = {
    "geospatial_standard_register_entry",
    "harmonised_standard_reference",
    "product_requirement_guidance",
    "standards_registry_entry",
}
STANDARDS_TERMS_KEYS = ("dataset_license", "terms_url", "terms_note", "license", "license_note")
BLOCKING_REPOSITORY_SOURCE_STATUSES = {
    "error_page",
    "not_found",
    "unavailable",
    "refused",
    "refused_oversize",
    "archive_too_large",
    "too_large",
}
POLICY_VOCABULARY_FIELDS = {
    "evidence_paths": "evidence_path",
    "source_policy": "source_policy",
    "freshness_policy": "freshness_policy",
    "identity_policy": "identity_policy",
}
MANUAL_ONLY_SOURCE_POLICIES = ("manual_review_required", "domain_pack_allowed")
MANUAL_ONLY_FRESHNESS_POLICIES = ("manual_review",)
MANUAL_ONLY_IDENTITY_POLICIES: tuple[str, ...] = ()
PACK_POLICY_ID_RE = re.compile(r"^pack:[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$")


@dataclass
class PolicyResult:
    policy: str
    verdict: str
    source_ids: list[str]
    reasons: list[str]
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "verdict": self.verdict,
            "source_ids": list(self.source_ids),
            "reasons": list(self.reasons),
            "remediation": self.remediation,
        }


@dataclass
class PolicyInputs:
    project_root: Path
    config: dict[str, Any]
    manifest_records: dict[str, dict[str, Any]]
    normalized_records: dict[str, dict[str, Any]]
    provenance_by_source_id: dict[str, dict[str, Any]]
    candidates: list[dict[str, Any]]
    candidates_by_request_id: dict[str, list[dict[str, Any]]]
    jurisdiction_profiles: dict[str, dict[str, Any]]
    coverage_manifests: dict[str, dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


@dataclass
class CitationIdentityEvidence:
    identifiers: dict[str, str]
    pass_reasons: list[str]
    fail_reasons: list[str]


@dataclass
class RepositoryIdentityEvidence:
    artifact_kind: str | None
    pass_reasons: list[str]
    fail_reasons: list[str]


def load_policy_inputs(project_root: Path | str, config: dict[str, Any] | None = None) -> PolicyInputs:
    root = Path(project_root).expanduser().resolve()
    loaded_config = config if isinstance(config, dict) else load_config(root)
    manifest_records = load_manifest_records(root / manifest_path_text(loaded_config))
    normalized_records = load_normalized_records(root / normalized_dir_text(loaded_config))
    provenance = load_provenance_by_source_id(root, manifest_records, normalized_records)
    candidates = load_jsonl_mapping_records(root / DEFAULT_CANDIDATES_PATH, optional=True)
    jurisdiction_profiles = load_jurisdiction_profiles(root / jurisdictions_path_text(loaded_config))
    coverage_manifests = load_coverage_manifests(root / coverage_dir_text(loaded_config))
    return PolicyInputs(
        project_root=root,
        config=loaded_config,
        manifest_records=manifest_records,
        normalized_records=normalized_records,
        provenance_by_source_id=provenance,
        candidates=candidates,
        candidates_by_request_id=index_candidates_by_request_id(candidates),
        jurisdiction_profiles=jurisdiction_profiles,
        coverage_manifests=coverage_manifests,
    )


def load_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "research.yml"
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ValueError(f"Invalid config: {path}")
    return document


def sources_config(config: dict[str, Any]) -> dict[str, Any]:
    sources = config.get("sources")
    return sources if isinstance(sources, dict) else {}


def discovery_config(config: dict[str, Any]) -> dict[str, Any]:
    integrations = config.get("integrations")
    if not isinstance(integrations, dict):
        return {}
    discovery = integrations.get("discovery")
    return discovery if isinstance(discovery, dict) else {}


def validate_workspace_relative_path(value: Any, label: str, *, must_be_sources: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty workspace-relative path")
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise ValueError(f"{label} must be workspace-relative, not a URL: {value}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise ValueError(f"{label} must not be an absolute path: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be workspace-relative without '..': {value}")
    relative = path.as_posix()
    if must_be_sources and relative != "sources" and not relative.startswith("sources/"):
        raise ValueError(f"{label} must be under sources/: {value}")
    return relative


def manifest_path_text(config: dict[str, Any]) -> str:
    return validate_workspace_relative_path(
        sources_config(config).get("manifest_path", DEFAULT_MANIFEST_PATH),
        "sources.manifest_path",
    )


def normalized_dir_text(config: dict[str, Any]) -> str:
    return validate_workspace_relative_path(
        sources_config(config).get("normalized_dir", DEFAULT_NORMALIZED_DIR),
        "sources.normalized_dir",
        must_be_sources=True,
    )


def coverage_dir_text(config: dict[str, Any]) -> str:
    return validate_workspace_relative_path(
        sources_config(config).get("coverage_dir", DEFAULT_COVERAGE_DIR),
        "sources.coverage_dir",
        must_be_sources=True,
    )


def jurisdictions_path_text(config: dict[str, Any]) -> str:
    value = discovery_config(config).get("jurisdictions_path", DEFAULT_JURISDICTIONS_PATH)
    return validate_workspace_relative_path(value, "integrations.discovery.jurisdictions_path", must_be_sources=True)


def load_jsonl_mapping_records(path: Path, *, optional: bool = False) -> list[dict[str, Any]]:
    if not path.is_file():
        if optional:
            return []
        raise ValueError(f"Missing JSONL artifact: {path}")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if optional:
                continue
            raise ValueError(f"Invalid JSONL in {path}:{line_number}: {exc}") from exc
        if isinstance(record, dict):
            records.append(record)
        elif not optional:
            raise ValueError(f"Invalid JSONL in {path}:{line_number}: expected object")
    return records


def load_manifest_records(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return records
    for record in load_jsonl_mapping_records(path):
        source_id = record.get("id")
        if isinstance(source_id, str) and source_id:
            records[source_id] = record
    return records


def read_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
    except OSError:
        return {}
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        return {}
    try:
        frontmatter = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError:
        return {}
    return frontmatter if isinstance(frontmatter, dict) else {}


def load_normalized_records(normalized_root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not normalized_root.is_dir():
        return records
    for path in sorted(normalized_root.rglob("*.md")):
        frontmatter = read_frontmatter(path)
        source_id = frontmatter.get("source_id")
        if isinstance(source_id, str) and source_id:
            records[source_id] = frontmatter
    return records


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return document if isinstance(document, dict) else {}


def raw_sidecar_path(project_root: Path, raw_path: str) -> Path | None:
    try:
        relative = validate_workspace_relative_path(raw_path, "raw_path")
    except ValueError:
        return None
    return project_root / f"{relative}.provenance.yml"


def sidecar_provenance_for_record(project_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    raw_paths = record.get("raw_paths")
    if not isinstance(raw_paths, list):
        return {}
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            continue
        sidecar = raw_sidecar_path(project_root, raw_path)
        if sidecar is None or not sidecar.is_file():
            continue
        document = load_yaml_mapping(sidecar)
        if document:
            document.setdefault("sidecar_path", sidecar.relative_to(project_root).as_posix())
            return document
    return {}


def load_provenance_by_source_id(
    project_root: Path,
    manifest_records: dict[str, dict[str, Any]],
    normalized_records: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    source_ids = sorted(set(manifest_records) | set(normalized_records))
    for source_id in source_ids:
        combined: dict[str, Any] = {}
        record = manifest_records.get(source_id, {})
        normalized = normalized_records.get(source_id, {})
        sidecar = sidecar_provenance_for_record(project_root, record) if record else {}
        normalized_provenance = normalized.get("provenance") if isinstance(normalized.get("provenance"), dict) else {}
        record_provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        for item in (sidecar, normalized_provenance, record_provenance):
            combined.update(item)
        if combined:
            provenance[source_id] = combined
    return provenance


def index_candidates_by_request_id(candidates: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_request: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        seen: set[str] = set()
        for key in ("selected_for_request_id", "selected_request_id", "request_id"):
            request_id = candidate.get(key)
            if isinstance(request_id, str) and request_id and request_id not in seen:
                by_request.setdefault(request_id, []).append(candidate)
                seen.add(request_id)
    return by_request


def normalize_domain(value: str) -> str:
    raw = value.strip().lower()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc or parsed.path
    return host.split("@")[-1].split(":")[0].removeprefix("www.")


def load_jurisdiction_profiles(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    document = load_yaml_mapping(path)
    profiles = document.get("jurisdiction_profiles")
    if not isinstance(profiles, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        raw_id = profile.get("jurisdiction_id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        normalized = dict(profile)
        for field_name in ("official_domains", "blocked_domains"):
            value = profile.get(field_name)
            if isinstance(value, list):
                normalized[field_name] = [
                    normalize_domain(item) for item in value if isinstance(item, str) and item.strip()
                ]
            else:
                normalized[field_name] = []
        result[raw_id.strip().lower()] = normalized
    return result


def load_coverage_manifests(coverage_root: Path) -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    if not coverage_root.is_dir():
        return manifests
    for path in sorted(coverage_root.glob("*.yml")):
        document = load_yaml_mapping(path)
        slug = document.get("question_slug")
        if isinstance(slug, str) and slug:
            manifests[slug] = document
        elif document:
            manifests[path.stem] = document
    return manifests


def normalize_source_ids(source_ids: list[str] | tuple[str, ...] | Any) -> list[str]:
    if not isinstance(source_ids, (list, tuple)):
        return []
    result: list[str] = []
    for value in source_ids:
        if isinstance(value, str) and value.strip() and value not in result:
            result.append(value.strip())
    return result


def unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def source_exists(inputs: PolicyInputs, source_id: str) -> bool:
    return source_id in inputs.manifest_records or source_id in inputs.normalized_records


def present_and_missing(source_ids: list[str], inputs: PolicyInputs) -> tuple[list[str], list[str]]:
    present = [source_id for source_id in source_ids if source_exists(inputs, source_id)]
    missing = [source_id for source_id in source_ids if not source_exists(inputs, source_id)]
    return present, missing


def source_metadata(inputs: PolicyInputs, source_id: str) -> dict[str, Any]:
    record = inputs.manifest_records.get(source_id, {})
    metadata = record.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def candidate_values(*documents: dict[str, Any]) -> list[dict[str, Any]]:
    return [document for document in documents if isinstance(document, dict)]


def source_value_present(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    return True


def source_value(inputs: PolicyInputs, source_id: str, keys: tuple[str, ...]) -> Any:
    record = inputs.manifest_records.get(source_id, {})
    normalized = inputs.normalized_records.get(source_id, {})
    metadata = source_metadata(inputs, source_id)
    provenance = inputs.provenance_by_source_id.get(source_id, {})
    for document in candidate_values(normalized, metadata, provenance, record):
        for key in keys:
            value = document.get(key)
            if source_value_present(value):
                return value
    return None


def source_date_metadata(inputs: PolicyInputs, source_id: str) -> dict[str, Any]:
    value = source_value(inputs, source_id, ("date_metadata",))
    return value if isinstance(value, dict) else {}


def source_date_metadata_value(inputs: PolicyInputs, source_id: str, key: str) -> Any:
    value = source_date_metadata(inputs, source_id).get(key)
    return value if value is not None and value != "" else None


def string_date_metadata_value(inputs: PolicyInputs, source_id: str, key: str) -> str | None:
    value = source_date_metadata_value(inputs, source_id, key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def explicit_unusable_reasons(document: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    value = document.get("unusable_evidence_reasons")
    if isinstance(value, list):
        reasons.extend(reason for reason in value if isinstance(reason, str) and reason.strip())
    reasons.extend(delivery_unusable_evidence_reasons(document))
    if document.get("evidence_usable") is False and not reasons:
        reasons.append("evidence_usable:false")
    return reasons


def source_unusable_evidence_reasons(inputs: PolicyInputs, source_id: str) -> list[str]:
    record = inputs.manifest_records.get(source_id, {})
    normalized = inputs.normalized_records.get(source_id, {})
    metadata = source_metadata(inputs, source_id)
    provenance = inputs.provenance_by_source_id.get(source_id, {})
    reasons: list[str] = []
    for document in candidate_values(normalized, metadata, provenance, record):
        reasons.extend(explicit_unusable_reasons(document))
    return unique_strings(reasons)


def unusable_evidence_result(policy: str, ids: list[str], present: list[str], inputs: PolicyInputs) -> PolicyResult | None:
    reasons: list[str] = []
    for source_id in present:
        for reason in source_unusable_evidence_reasons(inputs, source_id):
            reasons.append(f"{source_id} is marked unusable evidence ({reason}).")
    if not reasons:
        return None
    return result(
        policy,
        VERDICT_FAIL,
        ids,
        reasons,
        "Redeliver or replace unusable source captures before accepting them for required coverage facets.",
    )


def string_source_value(inputs: PolicyInputs, source_id: str, keys: tuple[str, ...]) -> str | None:
    value = source_value(inputs, source_id, keys)
    return value.strip() if isinstance(value, str) and value.strip() else None


def mapping_source_value(inputs: PolicyInputs, source_id: str, keys: tuple[str, ...]) -> dict[str, Any] | None:
    value = source_value(inputs, source_id, keys)
    return dict(value) if isinstance(value, dict) else None


def mapping_string(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def mapping_scalar_text(mapping: dict[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def standards_metadata(inputs: PolicyInputs, source_id: str) -> dict[str, Any] | None:
    value = mapping_source_value(inputs, source_id, ("standards",))
    if value is not None:
        return value
    for candidate in linked_candidates(inputs, source_id):
        candidate_standards = candidate.get("standards")
        if isinstance(candidate_standards, dict):
            return dict(candidate_standards)
    return None


def standards_provider(standards: dict[str, Any]) -> str | None:
    value = mapping_string(standards, "registry_provider")
    return value.lower() if value else None


def standards_body(standards: dict[str, Any]) -> str | None:
    return mapping_string(standards, "standards_body")


def standards_registry_url(inputs: PolicyInputs, source_id: str, standards: dict[str, Any]) -> str | None:
    return mapping_string(standards, "registry_url") or origin_url(inputs, source_id)


def standards_source_type(inputs: PolicyInputs, source_id: str) -> str | None:
    value = string_source_value(inputs, source_id, ("source_type",))
    return value.lower() if value else None


def standards_terms_known(inputs: PolicyInputs, source_id: str, standards: dict[str, Any]) -> bool:
    for key in STANDARDS_TERMS_KEYS:
        value = standards.get(key)
        if value is not None and value != "":
            return True
    for key in ("terms_url", "terms_note"):
        if string_source_value(inputs, source_id, (key,)):
            return True
    license_value = source_value(inputs, source_id, ("license",))
    return isinstance(license_value, str) and bool(license_value.strip()) and license_value != "unresolved"


def standards_has_edition_or_year(standards: dict[str, Any]) -> bool:
    if mapping_scalar_text(standards, "edition") or mapping_scalar_text(standards, "standard_edition"):
        return True
    for key in ("year", "publication_year", "standard_year"):
        value = standards.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return True
        if isinstance(value, str) and re.fullmatch(r"\d{4}", value.strip()):
            return True
    designation = mapping_string(standards, "designation")
    return bool(designation and re.search(r":\d{4}\b", designation))


def standards_status_text(standards: dict[str, Any]) -> str | None:
    for key in ("status", "stage", "current_stage"):
        value = mapping_string(standards, key)
        if value:
            return value.lower()
    return None


def standards_replacement_recorded(standards: dict[str, Any]) -> bool:
    for key in ("replaced_by", "replacement", "replacement_chain", "successor", "superseded_by"):
        value = standards.get(key)
        if value is not None and value != "" and value != []:
            return True
    return False


def standards_product_legal_authority(standards: dict[str, Any]) -> bool:
    provider = standards_provider(standards)
    if provider in PRODUCT_REQUIREMENT_LEGAL_PROVIDERS:
        return True
    return bool(mapping_string(standards, "legal_act") and (
        mapping_string(standards, "ojeu_reference")
        or mapping_string(standards, "harmonised_standard_reference")
    ))


def facet_text(facet: dict[str, Any] | None, *keys: str) -> str | None:
    if not isinstance(facet, dict):
        return None
    for key in keys:
        value = facet.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    standards = facet.get("standards")
    if isinstance(standards, dict):
        for key in keys:
            value = standards.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
    return None


def origin_url(inputs: PolicyInputs, source_id: str) -> str | None:
    return string_source_value(
        inputs,
        source_id,
        ("origin_url", "downloaded_pdf_url", "downloaded_archive_url", "url", "repository_url"),
    )


def host_from_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return normalize_domain(parsed.netloc)


def domain_matches(host: str, domain: str) -> bool:
    normalized_host = normalize_domain(host)
    normalized_domain = normalize_domain(domain)
    return normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}")


def canonical_url(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value.strip().rstrip("/")
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{normalize_domain(parsed.netloc)}{path}{query}"


def source_request_ids(inputs: PolicyInputs, source_id: str) -> list[str]:
    values: list[str] = []
    for keys in (("request_id", "source_request_id", "selected_for_request_id", "selected_request_id"),):
        value = string_source_value(inputs, source_id, keys)
        if value and value not in values:
            values.append(value)
    return values


def selected_candidate_request_id(candidate: dict[str, Any]) -> str | None:
    for key in ("selected_for_request_id", "selected_request_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_references_source(candidate: dict[str, Any], source_id: str) -> bool:
    for key in ("source_id", "selected_source_id", "manifest_source_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value == source_id:
            return True
    source_ids = candidate.get("source_ids")
    return isinstance(source_ids, list) and source_id in source_ids


def linked_candidates(inputs: PolicyInputs, source_id: str) -> list[dict[str, Any]]:
    origin = canonical_url(origin_url(inputs, source_id))
    request_ids = set(source_request_ids(inputs, source_id))
    matched: list[dict[str, Any]] = []
    for candidate in inputs.candidates:
        candidate_url = canonical_url(candidate.get("url") if isinstance(candidate.get("url"), str) else None)
        request_id = selected_candidate_request_id(candidate)
        if (
            candidate_references_source(candidate, source_id)
            or (isinstance(request_id, str) and request_id in request_ids)
            or (origin is not None and candidate_url == origin)
        ):
            matched.append(candidate)
    return matched


def blocked_domains(inputs: PolicyInputs) -> list[str]:
    domains: list[str] = []
    for profile in inputs.jurisdiction_profiles.values():
        for domain in profile.get("blocked_domains", []):
            if isinstance(domain, str) and domain not in domains:
                domains.append(domain)
    return domains


def official_domains(inputs: PolicyInputs) -> list[str]:
    domains: list[str] = []
    for profile in inputs.jurisdiction_profiles.values():
        for domain in profile.get("official_domains", []):
            if isinstance(domain, str) and domain not in domains:
                domains.append(domain)
    return domains


def source_matches_official_domain(inputs: PolicyInputs, source_id: str) -> tuple[bool, str | None]:
    host = host_from_url(origin_url(inputs, source_id))
    if host is None:
        return False, None
    if any(domain_matches(host, domain) for domain in blocked_domains(inputs)):
        return False, f"{source_id} origin host {host} is blocked by a jurisdiction profile"
    for domain in official_domains(inputs):
        if domain_matches(host, domain):
            return True, f"{source_id} origin host {host} matches official domain {domain}"
    return False, None


def source_has_official_candidate(inputs: PolicyInputs, source_id: str) -> tuple[bool, str | None]:
    for candidate in linked_candidates(inputs, source_id):
        candidate_id = candidate.get("candidate_id", "<unknown>")
        if candidate.get("official_source") is True or candidate.get("trust_tier") == "official_primary":
            return True, f"{source_id} is linked to official selected candidate {candidate_id}"
    return False, None


def strip_identifier_suffix(value: str) -> str:
    return value.strip().rstrip(".,;:)]}")


def normalize_doi_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and normalize_domain(parsed.netloc) in {"doi.org", "dx.doi.org"}:
        text = unquote(parsed.path.lstrip("/"))
    elif text.lower().startswith("doi:"):
        text = text.split(":", 1)[1].strip()
    text = strip_identifier_suffix(re.sub(r"\s+", "", text)).lower()
    return text if DOI_RE.fullmatch(text) else None


def normalize_arxiv_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and normalize_domain(parsed.netloc) == "arxiv.org":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"abs", "pdf", "e-print"}:
            text = unquote(parts[1])
        elif parts:
            text = unquote(parts[-1])
    elif text.lower().startswith("arxiv:"):
        text = text.split(":", 1)[1].strip()
    text = strip_identifier_suffix(text.removesuffix(".pdf")).lower()
    return text if ARXIV_ID_RE.fullmatch(text) else None


def normalize_openalex_work_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and normalize_domain(parsed.netloc) == "openalex.org":
        text = unquote(parsed.path.rsplit("/", 1)[-1])
    elif text.lower().startswith("openalex:"):
        text = text.split(":", 1)[1].strip()
    text = strip_identifier_suffix(text).upper()
    return text if OPENALEX_WORK_ID_RE.fullmatch(text) else None


def normalize_pmid_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = strip_identifier_suffix(value)
    return text if PMID_RE.fullmatch(text) else None


def normalize_pmcid_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = strip_identifier_suffix(value).upper()
    return text if PMCID_RE.fullmatch(text) else None


def normalize_citation_identifier(key: str, value: Any) -> str | None:
    if key == "doi":
        return normalize_doi_identifier(value)
    if key == "arxiv_id":
        return normalize_arxiv_identifier(value)
    if key == "openalex_id":
        return normalize_openalex_work_id(value)
    if key == "pmid":
        return normalize_pmid_identifier(value)
    if key == "pmcid":
        return normalize_pmcid_identifier(value)
    return None


def raw_citation_identifier_values(inputs: PolicyInputs, source_id: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in CITATION_ID_KEYS:
        value = string_source_value(inputs, source_id, (key,))
        if value:
            values[key] = value
    url = origin_url(inputs, source_id)
    host = host_from_url(url)
    if host in HOST_INDEX_PROVIDERS and url:
        if host in {"doi.org", "www.doi.org"}:
            values.setdefault("doi", url)
        elif host == "arxiv.org":
            values.setdefault("arxiv_id", url)
        elif host == "openalex.org":
            values.setdefault("openalex_id", url)
    return values


def citation_ids(inputs: PolicyInputs, source_id: str) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    for key, value in raw_citation_identifier_values(inputs, source_id).items():
        normalized = normalize_citation_identifier(key, value)
        if normalized:
            identifiers[key] = normalized
    return identifiers


def citation_title(inputs: PolicyInputs, source_id: str) -> str | None:
    return string_source_value(inputs, source_id, ("title", "display_name"))


def citation_year_reason(inputs: PolicyInputs, source_id: str) -> tuple[str | None, str | None]:
    for key in ("publication_year", "year"):
        value = source_value(inputs, source_id, (key,))
        if value is None:
            continue
        if isinstance(value, bool):
            return None, f"{source_id} {key} must be a four-digit year."
        if isinstance(value, int):
            if 1000 <= value <= 9999:
                return f"{key} {value}", None
            return None, f"{source_id} {key} must be a four-digit year."
        if isinstance(value, str) and re.fullmatch(r"\d{4}", value.strip()):
            return f"{key} {value.strip()}", None
        return None, f"{source_id} {key} must be a four-digit year."
    for key in ("date", "published", "publication_date"):
        value = source_value(inputs, source_id, (key,))
        if value is None:
            continue
        parsed = date_from_value(value)
        if parsed is not None:
            return f"{key} {parsed.isoformat()}", None
        return None, f"{source_id} {key} must be an ISO date or datetime."
    return None, None


def author_names_from_value(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        name: str | None = None
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            author = item.get("author")
            candidates = [
                item.get("name"),
                item.get("display_name"),
                author.get("display_name") if isinstance(author, dict) else None,
            ]
            name = next((candidate.strip() for candidate in candidates if isinstance(candidate, str) and candidate.strip()), None)
        if name:
            names.append(name)
    return names


def citation_authors_reason(inputs: PolicyInputs, source_id: str) -> tuple[str | None, str | None]:
    for key in ("authors", "authorships"):
        value = source_value(inputs, source_id, (key,))
        names = author_names_from_value(value)
        if names is None:
            continue
        if names:
            return f"{key} recorded", None
        if value:
            return None, f"{source_id} {key} metadata does not contain a usable author name."
    return None, None


def citation_identity_evidence(inputs: PolicyInputs, source_id: str) -> CitationIdentityEvidence:
    identifiers: dict[str, str] = {}
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    for key, value in raw_citation_identifier_values(inputs, source_id).items():
        normalized = normalize_citation_identifier(key, value)
        if normalized:
            identifiers[key] = normalized
        else:
            fail_reasons.append(f"{source_id} has malformed {key} metadata: {value!r}.")

    title = citation_title(inputs, source_id)
    if not title:
        fail_reasons.append(f"{source_id} has no title metadata for citation identity.")
    if not identifiers:
        fail_reasons.append(f"{source_id} has no valid local DOI, arXiv, OpenAlex, PMID, or PMCID metadata.")

    year_reason, year_error = citation_year_reason(inputs, source_id)
    if year_error:
        fail_reasons.append(year_error)
    author_reason, author_error = citation_authors_reason(inputs, source_id)
    if author_error:
        fail_reasons.append(author_error)

    if identifiers and title and not fail_reasons:
        details = [f"{key}={value}" for key, value in sorted(identifiers.items())]
        details.append("title recorded")
        if year_reason:
            details.append(year_reason)
        if author_reason:
            details.append(author_reason)
        pass_reasons.append(f"{source_id} has valid local citation identity metadata: {', '.join(details)}.")
    return CitationIdentityEvidence(identifiers=identifiers, pass_reasons=pass_reasons, fail_reasons=fail_reasons)


def has_openalex_or_arxiv_identity(inputs: PolicyInputs, source_id: str) -> bool:
    identifiers = citation_ids(inputs, source_id)
    return any(key in identifiers for key in OPENALEX_OR_ARXIV_KEYS)


def repo_full_name(inputs: PolicyInputs, source_id: str) -> str | None:
    value = string_source_value(inputs, source_id, ("repo_full_name", "codebase_repo", "repository"))
    if value:
        return value
    url = origin_url(inputs, source_id)
    parsed = urlparse(url or "")
    host = normalize_domain(parsed.netloc) if parsed.netloc else ""
    parts = [part for part in parsed.path.split("/") if part]
    if host in GITHUB_HOSTS and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def repository_ref(inputs: PolicyInputs, source_id: str) -> str | None:
    for key in sorted(REPOSITORY_REF_KEYS):
        value = string_source_value(inputs, source_id, (key,))
        if value:
            return value
    url = origin_url(inputs, source_id)
    parsed = urlparse(url or "")
    path = parsed.path
    match = re.search(r"/(?:releases/tag|tree|commit)/([^/?#]+)", path)
    return match.group(1) if match else None


def accepted_repository_artifact_kinds(value: Any = None) -> tuple[str, ...]:
    if not isinstance(value, list):
        return DEFAULT_REPOSITORY_ARTIFACT_KINDS
    kinds: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() in REPOSITORY_ARTIFACT_KINDS and item.strip() not in kinds:
            kinds.append(item.strip())
    return tuple(kinds) if kinds else DEFAULT_REPOSITORY_ARTIFACT_KINDS


def repository_provenance(inputs: PolicyInputs, source_id: str) -> dict[str, Any]:
    provenance = inputs.provenance_by_source_id.get(source_id)
    return provenance if isinstance(provenance, dict) else {}


def provenance_string(provenance: dict[str, Any], key: str) -> str | None:
    value = provenance.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def provenance_field_present(provenance: dict[str, Any], key: str) -> bool:
    return key in provenance


def repository_full_name_from_provenance(provenance: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    owner = provenance_string(provenance, "repository_owner")
    repo_name = provenance_string(provenance, "repository_name")
    full_name = provenance_string(provenance, "repository_full_name")
    if full_name is None and owner and repo_name:
        full_name = f"{owner}/{repo_name}"
    return owner, repo_name, full_name


def selected_repository_ref(provenance: dict[str, Any]) -> str | None:
    for key in REPOSITORY_SELECTED_REF_KEYS:
        value = provenance_string(provenance, key)
        if value:
            return value
    return None


def repository_refusal_reason(inputs: PolicyInputs, source_id: str, provenance: dict[str, Any]) -> str | None:
    for status in status_values(inputs, source_id):
        if status in BLOCKING_REPOSITORY_SOURCE_STATUSES or "refus" in status or "oversize" in status:
            return f"{source_id} source_status {status!r} cannot satisfy repository implementation evidence."
    notes = provenance_string(provenance, "notes")
    if notes and ("refus" in notes.lower() or "oversize" in notes.lower()):
        return f"{source_id} provenance notes describe a refused repository artifact: {notes}"
    return None


def repository_identity_evidence(
    inputs: PolicyInputs,
    source_id: str,
    allowed_artifact_kinds: tuple[str, ...] = DEFAULT_REPOSITORY_ARTIFACT_KINDS,
) -> RepositoryIdentityEvidence:
    provenance = repository_provenance(inputs, source_id)
    fail_reasons: list[str] = []
    pass_reasons: list[str] = []
    artifact_kind = provenance_string(provenance, "repository_artifact_kind")
    if artifact_kind not in REPOSITORY_ARTIFACT_KINDS:
        fail_reasons.append(f"{source_id} has no supported GitHub repository_artifact_kind provenance.")
        artifact_kind = None
    elif artifact_kind not in allowed_artifact_kinds:
        fail_reasons.append(
            f"{source_id} repository artifact kind {artifact_kind} is not allowed for this facet."
        )

    refusal = repository_refusal_reason(inputs, source_id, provenance)
    if refusal:
        fail_reasons.append(refusal)

    origin = provenance_string(provenance, "origin_url")
    if host_from_url(origin) not in {"github.com"}:
        fail_reasons.append(f"{source_id} has no GitHub origin_url in acquisition provenance.")

    owner, repo_name, full_name = repository_full_name_from_provenance(provenance)
    if not owner or not repo_name or not full_name:
        fail_reasons.append(f"{source_id} lacks repository_owner, repository_name, or repository_full_name provenance.")

    ref = selected_repository_ref(provenance)
    if not ref:
        fail_reasons.append(f"{source_id} lacks selected repository_ref provenance.")

    retrieved = date_from_value(provenance.get("retrieved_at"))
    if retrieved is None:
        fail_reasons.append(f"{source_id} has no parseable retrieved_at provenance.")

    if not provenance_field_present(provenance, "license"):
        fail_reasons.append(f"{source_id} has no explicit license provenance; use null when the license is unknown.")

    if artifact_kind == "source_archive":
        archive_url = provenance_string(provenance, "downloaded_archive_url")
        if not archive_url:
            fail_reasons.append(f"{source_id} source_archive lacks downloaded_archive_url provenance.")
        checksum = provenance_string(provenance, "checksum")
        if not checksum:
            fail_reasons.append(f"{source_id} source_archive lacks archive checksum provenance.")
        checksum_verified = provenance.get("checksum_verified")
        if checksum_verified is not None and checksum_verified is not True:
            fail_reasons.append(f"{source_id} source_archive checksum is not verified.")

    if not fail_reasons:
        details = [
            f"artifact_kind={artifact_kind}",
            f"repository={full_name}",
            f"ref={ref}",
            "license status recorded",
            "retrieved_at recorded",
        ]
        if artifact_kind == "source_archive":
            details.append("archive checksum recorded")
        commit_sha = provenance_string(provenance, "commit_sha")
        if commit_sha:
            details.append(f"commit_sha={commit_sha}")
        pass_reasons.append(f"{source_id} has GitHub acquisition provenance: {', '.join(details)}.")

    return RepositoryIdentityEvidence(
        artifact_kind=artifact_kind,
        pass_reasons=pass_reasons,
        fail_reasons=fail_reasons,
    )


def date_from_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date()
    except ValueError:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None


def parse_validity_period(value: Any) -> tuple[date | None, date | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None, None
    text = value.strip()
    if "/" not in text:
        return None, None, f"validity_period {text!r} must use ISO interval syntax start/end"
    start_text, end_text = (part.strip() for part in text.split("/", 1))
    start = date_from_value(start_text) if start_text else None
    end = date_from_value(end_text) if end_text else None
    if start_text and start is None:
        return None, None, f"validity_period start {start_text!r} is not an ISO date"
    if end_text and end is None:
        return None, None, f"validity_period end {end_text!r} is not an ISO date"
    if start and end and end < start:
        return None, None, f"validity_period {text!r} ends before it starts"
    return start, end, None


def status_values(inputs: PolicyInputs, source_id: str) -> list[str]:
    values: list[str] = []
    for document in candidate_values(inputs.normalized_records.get(source_id, {}), inputs.manifest_records.get(source_id, {})):
        value = document.get("status")
        if isinstance(value, str) and value.strip():
            values.append(value.strip().lower())
    source_status = string_source_value(inputs, source_id, ("source_status",))
    if source_status:
        values.append(source_status.lower())
    return values


def candidate_risk_flags(candidate: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for container in (candidate, candidate.get("reasoning")):
        if not isinstance(container, dict):
            continue
        flags = container.get("risk_flags")
        if not isinstance(flags, list):
            continue
        for flag in flags:
            if isinstance(flag, str) and flag.strip() and flag not in values:
                values.append(flag.strip())
    return values


def currentness_blocking_reasons(inputs: PolicyInputs, source_id: str) -> list[str]:
    reasons: list[str] = []
    for status in status_values(inputs, source_id):
        if status in BLOCKING_CURRENTNESS_RECORD_STATUSES:
            reasons.append(f"{source_id} status {status!r} cannot satisfy a currentness policy.")
        if status in BLOCKING_CURRENTNESS_SOURCE_STATUSES:
            reasons.append(f"{source_id} source_status {status!r} cannot satisfy a currentness policy.")
    for candidate in linked_candidates(inputs, source_id):
        candidate_id = candidate.get("candidate_id", "<unknown>")
        for flag in candidate_risk_flags(candidate):
            if flag in BLOCKING_CURRENTNESS_RISK_FLAGS:
                reasons.append(f"{source_id} linked candidate {candidate_id} has blocking risk flag {flag}.")
    return reasons


def currentness_date_reason(policy: str, source_id: str, retrieved: date, inputs: PolicyInputs) -> tuple[bool, str]:
    validity = (
        string_source_value(inputs, source_id, ("validity_period",))
        or string_date_metadata_value(inputs, source_id, "validity_period")
    )
    if validity:
        start, end, error = parse_validity_period(validity)
        if error:
            return False, f"{source_id} {error}."
        if end and end < retrieved:
            return False, f"{source_id} validity period {validity} is stale for retrieval date {retrieved.isoformat()}."
        if start and start > retrieved:
            return False, f"{source_id} validity period {validity} starts after retrieval date {retrieved.isoformat()}."
        return True, f"{source_id} validity period {validity} covers retrieval date {retrieved.isoformat()}."

    valid_for_year = source_date_metadata_value(inputs, source_id, "valid_for_year")
    if valid_for_year is not None:
        try:
            year = int(str(valid_for_year).strip())
        except ValueError:
            return False, f"{source_id} date_metadata.valid_for_year is not a four-digit year."
        if year == retrieved.year:
            return True, f"{source_id} date_metadata.valid_for_year {year} matches retrieval year {retrieved.year}."
        return False, f"{source_id} date_metadata.valid_for_year {year} is stale for retrieval year {retrieved.year}."

    dated_fields = (
        ("effective_date", "effective_date"),
        ("publication_date", "publication_date"),
        ("valid_from", "date_metadata.valid_from"),
        ("effective_date", "date_metadata.effective_date"),
        ("publication_date", "date_metadata.publication_date"),
    )
    for key, label in dated_fields:
        if label.startswith("date_metadata."):
            raw_value = source_date_metadata_value(inputs, source_id, key)
        else:
            raw_value = source_value(inputs, source_id, (key,))
        parsed = date_from_value(raw_value)
        if raw_value is None:
            continue
        if parsed is None:
            return False, f"{source_id} {label} is not an ISO date."
        if parsed > retrieved:
            return False, f"{source_id} {label} {parsed.isoformat()} is after retrieval date {retrieved.isoformat()}."
        if key == "valid_from":
            return True, f"{source_id} {label} {parsed.isoformat()} is active by retrieval date {retrieved.isoformat()}."
        if parsed.year == retrieved.year:
            return True, f"{source_id} {label} {parsed.isoformat()} is in retrieval year {retrieved.year}."
        return False, f"{source_id} {label} {parsed.isoformat()} is stale for retrieval year {retrieved.year}."

    currentness_indicator = string_date_metadata_value(inputs, source_id, "currentness_indicator")
    if currentness_indicator:
        return True, f"{source_id} date_metadata.currentness_indicator records: {currentness_indicator}"

    date_note = string_source_value(inputs, source_id, ("date_not_available",))
    if date_note:
        if policy == "current_product_spec":
            return True, f"{source_id} records date_not_available: {date_note}"
        return False, f"{source_id} date_not_available cannot satisfy current legal, regulatory, or tax figures."

    return False, f"{source_id} has no effective_date, validity_period, publication_date, date_metadata, or date_not_available note."


def evaluate_currentness_policy(policy: str, ids: list[str], present: list[str], inputs: PolicyInputs) -> PolicyResult:
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    for source_id in present:
        blocking = currentness_blocking_reasons(inputs, source_id)
        if blocking:
            fail_reasons.extend(blocking)
            continue
        origin = origin_url(inputs, source_id)
        if host_from_url(origin) is None:
            fail_reasons.append(f"{source_id} has no valid origin_url metadata for currentness evaluation.")
            continue
        retrieved = date_from_value(source_value(inputs, source_id, ("retrieved_at",)))
        if retrieved is None:
            fail_reasons.append(f"{source_id} has no parseable retrieved_at metadata for currentness evaluation.")
            continue
        passed, reason = currentness_date_reason(policy, source_id, retrieved, inputs)
        if passed:
            pass_reasons.append(reason)
        else:
            fail_reasons.append(reason)

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Record origin_url, retrieved_at, and current effective/publication/validity metadata from a usable source.",
        )
    return result(policy, VERDICT_OK, ids, pass_reasons)


def result(
    policy: str,
    verdict: str,
    source_ids: list[str],
    reasons: list[str],
    remediation: str | None = None,
) -> PolicyResult:
    return PolicyResult(
        policy=policy,
        verdict=verdict,
        source_ids=list(source_ids),
        reasons=reasons or [f"Policy {policy} produced no detailed local reason."],
        remediation=remediation,
    )


def missing_result(policy: str, source_ids: list[str], missing: list[str]) -> PolicyResult:
    return result(
        policy,
        VERDICT_FAIL,
        source_ids,
        [f"Missing accepted source id(s): {', '.join(missing)}."],
        "Add the source to sources/manifest.jsonl and normalize it before accepting it for this facet.",
    )


def manual_result(policy: str, source_ids: list[str], reasons: list[str]) -> PolicyResult:
    return result(
        policy,
        VERDICT_MANUAL_REVIEW,
        source_ids,
        reasons,
        "Review the accepted local source metadata and record a stronger source, provenance, or manual decision.",
    )


def pack_policy_definition(inputs: PolicyInputs, field: str, policy: str) -> str | None:
    if PACK_POLICY_ID_RE.fullmatch(policy) is None:
        return None
    domain_pack = inputs.config.get("domain_pack") if isinstance(inputs.config.get("domain_pack"), dict) else {}
    vocabularies = domain_pack.get("policy_vocabularies") if isinstance(domain_pack.get("policy_vocabularies"), dict) else {}
    definitions = vocabularies.get(field) if isinstance(vocabularies.get(field), dict) else {}
    value = definitions.get(policy) if isinstance(definitions, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def pack_policy_manual_result(policy: str, source_ids: list[str], definition: str) -> PolicyResult:
    return manual_result(
        policy,
        source_ids,
        [f"Domain-pack policy {policy} requires recorded domain review: {definition}"],
    )


def undeclared_pack_policy_result(policy: str, source_ids: list[str], field: str) -> PolicyResult | None:
    if PACK_POLICY_ID_RE.fullmatch(policy) is None:
        return None
    return result(
        policy,
        VERDICT_FAIL,
        source_ids,
        [f"Namespaced {field} policy {policy} is not declared by the active domain pack."],
        "Declare the namespaced policy under domain_pack.policy_vocabularies or use a base policy id.",
    )


def evaluate_standards_source_policy(policy: str, ids: list[str], present: list[str], inputs: PolicyInputs) -> PolicyResult:
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    for source_id in present:
        standards = standards_metadata(inputs, source_id)
        if standards is None:
            fail_reasons.append(f"standard_reference_missing: {source_id} has no standards metadata.")
            continue
        if not standards_terms_known(inputs, source_id, standards):
            fail_reasons.append(f"registry_terms_unknown: {source_id} has no recorded registry terms, terms note, or dataset license.")
        provider = standards_provider(standards)
        body = standards_body(standards)
        source_type = standards_source_type(inputs, source_id)
        registry_url = standards_registry_url(inputs, source_id, standards)
        if policy == "official_standards_registry":
            official_candidate, candidate_reason = source_has_official_candidate(inputs, source_id)
            if body or provider in OFFICIAL_STANDARDS_REGISTRY_PROVIDERS or source_type in STANDARDS_SOURCE_TYPES:
                pass_reasons.append(
                    f"{source_id} records official standards registry metadata"
                    f"{f' provider={provider}' if provider else ''}{f' body={body}' if body else ''}."
                )
            elif official_candidate and candidate_reason:
                pass_reasons.append(candidate_reason)
            else:
                fail_reasons.append(f"standard_reference_missing: {source_id} has no official standards registry signal.")
        elif policy == "standards_body_primary":
            if not body or not registry_url:
                fail_reasons.append(f"standard_reference_missing: {source_id} lacks standards_body or registry_url metadata.")
                continue
            host = host_from_url(registry_url)
            allowed_domains = STANDARDS_BODY_DOMAINS.get(body.lower(), ())
            if host and any(domain_matches(host, domain) for domain in allowed_domains):
                pass_reasons.append(f"{source_id} registry_url host {host} matches standards body {body}.")
            else:
                fail_reasons.append(
                    f"standard_reference_missing: {source_id} registry_url does not identify standards body {body}."
                )

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            [*pass_reasons, *fail_reasons],
            "Record official registry metadata, registry terms, and standards-body catalogue URLs before accepting this source.",
        )
    return result(policy, VERDICT_OK, ids, pass_reasons)


def evaluate_current_standard_reference(policy: str, ids: list[str], present: list[str], inputs: PolicyInputs) -> PolicyResult:
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    for source_id in present:
        standards = standards_metadata(inputs, source_id)
        if standards is None:
            fail_reasons.append(f"standard_reference_missing: {source_id} has no standards metadata.")
            continue
        if not mapping_string(standards, "designation"):
            fail_reasons.append(f"standard_reference_missing: {source_id} has no standard designation.")
        if not standards_has_edition_or_year(standards):
            fail_reasons.append(f"standard_edition_missing: {source_id} lacks edition or year metadata.")
        if date_from_value(source_value(inputs, source_id, ("retrieved_at",))) is None:
            fail_reasons.append(f"registry_metadata_stale: {source_id} has no parseable retrieved_at provenance.")
        status = standards_status_text(standards)
        if status is None:
            fail_reasons.append(f"standard_reference_missing: {source_id} has no standard status metadata.")
        elif any(term in status for term in STANDARD_SUPERSEDED_STATUS_TERMS):
            fail_reasons.append(f"standard_status_superseded: {source_id} status is {status}.")
        elif any(term in status for term in STANDARD_WITHDRAWN_STATUS_TERMS):
            fail_reasons.append(f"standard_status_withdrawn: {source_id} status is {status}.")
        elif any(term in status for term in STANDARD_DRAFT_STATUS_TERMS):
            fail_reasons.append(f"standard_status_draft: {source_id} status is {status}.")
        elif status not in STANDARD_CURRENT_STATUSES and not standards.get("historical_scope"):
            fail_reasons.append(f"registry_metadata_stale: {source_id} status {status!r} is not a current status.")
        if standards_replacement_recorded(standards) and standards.get("replacement_resolved") is not True:
            fail_reasons.append(f"standard_replacement_unresolved: {source_id} records an unresolved replacement chain.")
        if not fail_reasons:
            pass_reasons.append(f"{source_id} records a current published standard reference.")

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Record exact designation, edition/year, current status, retrieved_at, and resolved replacement metadata.",
        )
    return result(policy, VERDICT_OK, ids, pass_reasons)


def evaluate_current_product_requirement(policy: str, ids: list[str], present: list[str], inputs: PolicyInputs) -> PolicyResult:
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    for source_id in present:
        standards = standards_metadata(inputs, source_id)
        if standards is None:
            fail_reasons.append(f"standard_reference_missing: {source_id} has no standards product-requirement metadata.")
            continue
        source_type = standards_source_type(inputs, source_id)
        if source_type == "product_requirement_guidance" and not standards_product_legal_authority(standards):
            fail_reasons.append(
                f"product_requirement_guidance_not_legal_authority: {source_id} is guidance without legal/OJEU authority."
            )
            continue
        if date_from_value(source_value(inputs, source_id, ("retrieved_at",))) is None:
            fail_reasons.append(f"registry_metadata_stale: {source_id} has no parseable retrieved_at provenance.")
        if source_type == "harmonised_standard_reference" and not mapping_string(standards, "ojeu_reference"):
            fail_reasons.append(f"harmonised_standard_ojeu_reference_missing: {source_id} lacks an OJEU reference.")
        signal_keys = (
            "ojeu_reference_date",
            "publication_date",
            "effective_date",
            "validity_period",
            "valid_from",
            "updated_at",
        )
        has_signal = any(mapping_string(standards, key) for key in signal_keys) or bool(source_date_metadata(inputs, source_id))
        if not has_signal:
            fail_reasons.append(f"registry_metadata_stale: {source_id} has no product-requirement date/currentness signal.")
        if not fail_reasons:
            pass_reasons.append(f"{source_id} records product requirement currentness metadata.")

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Record retrieved_at plus OJEU/legal/product requirement date metadata from a controlling source.",
        )
    return result(policy, VERDICT_OK, ids, pass_reasons)


def evaluate_standard_designation_identity(
    policy: str,
    ids: list[str],
    present: list[str],
    inputs: PolicyInputs,
    facet: dict[str, Any] | None,
) -> PolicyResult:
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    expected_designation = facet_text(facet, "standard_designation", "designation")
    expected_edition = facet_text(facet, "standard_edition", "edition")
    expected_title = facet_text(facet, "standard_title", "title")
    for source_id in present:
        standards = standards_metadata(inputs, source_id)
        if standards is None:
            fail_reasons.append(f"standard_reference_missing: {source_id} has no standards metadata.")
            continue
        designation = mapping_string(standards, "designation")
        if not designation:
            fail_reasons.append(f"standard_reference_missing: {source_id} has no standard designation.")
        elif expected_designation and designation != expected_designation:
            fail_reasons.append(
                f"standard_title_mismatch: {source_id} designation {designation!r} does not match {expected_designation!r}."
            )
        edition = mapping_scalar_text(standards, "edition") or mapping_scalar_text(standards, "standard_edition")
        if expected_edition and edition != expected_edition:
            fail_reasons.append(f"standard_edition_missing: {source_id} edition does not match {expected_edition}.")
        title = mapping_string(standards, "title")
        if expected_title and title and title != expected_title:
            fail_reasons.append(f"standard_title_mismatch: {source_id} title does not match the facet standard title.")
        for candidate in linked_candidates(inputs, source_id):
            candidate_standards = candidate.get("standards")
            if not isinstance(candidate_standards, dict):
                continue
            candidate_designation = mapping_string(candidate_standards, "designation")
            if designation and candidate_designation and candidate_designation != designation:
                fail_reasons.append(
                    f"standard_title_mismatch: {source_id} candidate designation does not match source designation."
                )
        if not fail_reasons:
            pass_reasons.append(f"{source_id} standard designation and edition align with registry metadata.")

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Align the facet designation, edition/year, title, selected candidate, and source standards metadata.",
        )
    return result(policy, VERDICT_OK, ids, pass_reasons)


def evaluate_product_requirement_identity(
    policy: str,
    ids: list[str],
    present: list[str],
    inputs: PolicyInputs,
    facet: dict[str, Any] | None,
) -> PolicyResult:
    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    expected_category = facet_text(facet, "product_category", "register_category")
    expected_legal_act = facet_text(facet, "legal_act")
    expected_designation = facet_text(facet, "standard_designation", "designation")
    for source_id in present:
        standards = standards_metadata(inputs, source_id)
        if standards is None:
            fail_reasons.append(f"standard_reference_missing: {source_id} has no product-requirement standards metadata.")
            continue
        source_type = standards_source_type(inputs, source_id)
        if source_type == "product_requirement_guidance" and not standards_product_legal_authority(standards):
            fail_reasons.append(
                f"product_requirement_guidance_not_legal_authority: {source_id} is guidance without legal/OJEU authority."
            )
        product_category = mapping_string(standards, "product_category") or mapping_string(standards, "register_category")
        legal_act = mapping_string(standards, "legal_act")
        designation = mapping_string(standards, "designation")
        ojeu_reference = mapping_string(standards, "ojeu_reference")
        harmonised = mapping_string(standards, "harmonised_standard_reference")
        if not product_category or (expected_category and product_category != expected_category):
            fail_reasons.append(f"standard_reference_missing: {source_id} product category does not match the facet.")
        if not legal_act or (expected_legal_act and legal_act != expected_legal_act):
            fail_reasons.append(f"standard_reference_missing: {source_id} legal act does not match the facet.")
        if not designation or (expected_designation and designation != expected_designation):
            fail_reasons.append(f"standard_title_mismatch: {source_id} standard designation does not match the facet.")
        if not (ojeu_reference or harmonised):
            fail_reasons.append(f"harmonised_standard_ojeu_reference_missing: {source_id} lacks OJEU/harmonised linkage.")
        if not fail_reasons:
            pass_reasons.append(f"{source_id} registry entry links the product category, legal act, and harmonised standard.")

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Record product category, legal act, OJEU/harmonised reference, and exact registry designation.",
        )
    return result(policy, VERDICT_OK, ids, pass_reasons)


def evaluate_source_policy(
    policy: str,
    source_ids: list[str] | tuple[str, ...],
    inputs: PolicyInputs,
    accepted_artifact_kinds: tuple[str, ...] = DEFAULT_REPOSITORY_ARTIFACT_KINDS,
) -> PolicyResult:
    ids = normalize_source_ids(source_ids)
    present, missing = present_and_missing(ids, inputs)
    if not ids:
        return missing_result(policy, ids, ["<none>"])
    if not present:
        return missing_result(policy, ids, missing)
    unusable = unusable_evidence_result(policy, ids, present, inputs)
    if unusable:
        return unusable
    if policy == "manual_review_required":
        return manual_result(policy, ids, ["Source policy requires manual review and cannot pass automatically."])
    if policy == "domain_pack_allowed":
        return manual_result(policy, ids, ["Domain-pack-specific source policy requires a recorded domain review."])
    pack_definition = pack_policy_definition(inputs, "source_policy", policy)
    if pack_definition is not None:
        return pack_policy_manual_result(policy, ids, pack_definition)
    undeclared = undeclared_pack_policy_result(policy, ids, "source_policy")
    if undeclared is not None:
        return undeclared

    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    review_reasons: list[str] = []
    if policy in {"official_standards_registry", "standards_body_primary"}:
        return evaluate_standards_source_policy(policy, ids, present, inputs)
    for source_id in present:
        if policy == "official_primary":
            matched, reason = source_matches_official_domain(inputs, source_id)
            if not matched:
                matched, reason = source_has_official_candidate(inputs, source_id)
            if matched and reason:
                pass_reasons.append(reason)
            else:
                review_reasons.append(f"{source_id} has no local official-domain or official-candidate signal.")
        elif policy == "primary_or_official":
            if citation_ids(inputs, source_id):
                pass_reasons.append(f"{source_id} has local primary citation identity metadata.")
            elif repo_full_name(inputs, source_id):
                pass_reasons.append(f"{source_id} has local repository-owner metadata.")
            else:
                matched, reason = source_matches_official_domain(inputs, source_id)
                if not matched:
                    matched, reason = source_has_official_candidate(inputs, source_id)
                if matched and reason:
                    pass_reasons.append(reason)
                else:
                    review_reasons.append(f"{source_id} has no local primary or official-source signal.")
        elif policy == "academic_indexed":
            evidence = citation_identity_evidence(inputs, source_id)
            if evidence.identifiers:
                pass_reasons.append(
                    f"{source_id} has valid local academic identifier metadata: "
                    f"{', '.join(sorted(evidence.identifiers))}."
                )
            else:
                review_reasons.extend(
                    evidence.fail_reasons
                    or [f"{source_id} has no valid local DOI, arXiv, OpenAlex, PMID, or PMCID metadata."]
                )
        elif policy == "openalex_or_arxiv":
            identifiers = citation_ids(inputs, source_id)
            indexed_keys = [key for key in OPENALEX_OR_ARXIV_KEYS if key in identifiers]
            if indexed_keys:
                pass_reasons.append(f"{source_id} has valid local OpenAlex or arXiv metadata: {', '.join(indexed_keys)}.")
            else:
                evidence = citation_identity_evidence(inputs, source_id)
                review_reasons.extend(
                    evidence.fail_reasons or [f"{source_id} has no valid local OpenAlex or arXiv identity metadata."]
                )
        elif policy == "canonical_repository":
            evidence = repository_identity_evidence(inputs, source_id, accepted_artifact_kinds)
            if evidence.fail_reasons:
                fail_reasons.extend(evidence.fail_reasons)
            elif evidence.pass_reasons:
                pass_reasons.extend(evidence.pass_reasons)
            else:
                review_reasons.append(f"{source_id} has no local canonical repository metadata.")
        elif policy == "official_vendor":
            matched, reason = source_has_official_candidate(inputs, source_id)
            if matched and reason:
                pass_reasons.append(reason)
            else:
                review_reasons.append(f"{source_id} is not linked to an official selected vendor candidate.")
        else:
            review_reasons.append(f"Source policy {policy!r} has no offline evaluator in this helper.")

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Use a GitHub acquisition record with repository owner/name, artifact kind, ref, retrieved_at, license status, and archive checksum when source code evidence is required.",
        )
    if pass_reasons:
        if missing:
            pass_reasons.append(f"Ignored missing non-qualifying source id(s): {', '.join(missing)}.")
        return result(policy, VERDICT_OK, ids, pass_reasons)
    if missing:
        return missing_result(policy, ids, missing)
    return manual_result(policy, ids, review_reasons)


def evaluate_freshness_policy(
    policy: str,
    source_ids: list[str] | tuple[str, ...],
    inputs: PolicyInputs,
    accepted_artifact_kinds: tuple[str, ...] = DEFAULT_REPOSITORY_ARTIFACT_KINDS,
) -> PolicyResult:
    ids = normalize_source_ids(source_ids)
    present, missing = present_and_missing(ids, inputs)
    if not ids:
        return missing_result(policy, ids, ["<none>"])
    if not present:
        return missing_result(policy, ids, missing)
    unusable = unusable_evidence_result(policy, ids, present, inputs)
    if unusable:
        return unusable
    if policy == "manual_review":
        return manual_result(
            policy,
            ids,
            [f"Freshness policy {policy} requires currentness review and cannot pass from local metadata alone."],
        )
    pack_definition = pack_policy_definition(inputs, "freshness_policy", policy)
    if pack_definition is not None:
        return pack_policy_manual_result(policy, ids, pack_definition)
    undeclared = undeclared_pack_policy_result(policy, ids, "freshness_policy")
    if undeclared is not None:
        return undeclared
    if policy in {"current_legal_figure", "current_product_spec"}:
        return evaluate_currentness_policy(policy, ids, present, inputs)
    if policy == "current_standard_reference":
        return evaluate_current_standard_reference(policy, ids, present, inputs)
    if policy == "current_product_requirement":
        return evaluate_current_product_requirement(policy, ids, present, inputs)
    if policy == "no_staleness_check":
        return result(policy, VERDICT_OK, ids, ["No deterministic staleness check is required for this facet."])

    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    review_reasons: list[str] = []
    for source_id in present:
        if policy == "publication_identity":
            evidence = citation_identity_evidence(inputs, source_id)
            if evidence.identifiers:
                pass_reasons.append(
                    f"{source_id} has valid local publication identity metadata: "
                    f"{', '.join(sorted(evidence.identifiers))}."
                )
            else:
                review_reasons.extend(
                    evidence.fail_reasons or [f"{source_id} has no valid local citation identifier for publication identity."]
                )
        elif policy == "release_snapshot":
            evidence = repository_identity_evidence(inputs, source_id, accepted_artifact_kinds)
            if evidence.fail_reasons:
                fail_reasons.extend(evidence.fail_reasons)
            elif evidence.pass_reasons:
                pass_reasons.extend(evidence.pass_reasons)
            else:
                review_reasons.append(f"{source_id} lacks local repository and stable ref metadata.")
        else:
            review_reasons.append(f"Freshness policy {policy!r} has no offline evaluator in this helper.")

    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Record a GitHub acquisition artifact with a selected ref and retrieval provenance before accepting this release snapshot.",
        )
    if pass_reasons:
        return result(policy, VERDICT_OK, ids, pass_reasons)
    if missing:
        return missing_result(policy, ids, missing)
    return manual_result(policy, ids, review_reasons)


def evaluate_identity_policy(
    policy: str,
    source_ids: list[str] | tuple[str, ...],
    inputs: PolicyInputs,
    accepted_artifact_kinds: tuple[str, ...] = DEFAULT_REPOSITORY_ARTIFACT_KINDS,
    facet: dict[str, Any] | None = None,
) -> PolicyResult:
    ids = normalize_source_ids(source_ids)
    present, missing = present_and_missing(ids, inputs)
    if not ids:
        return missing_result(policy, ids, ["<none>"])
    if not present:
        return missing_result(policy, ids, missing)
    unusable = unusable_evidence_result(policy, ids, present, inputs)
    if unusable:
        return unusable
    if policy == "none":
        return result(policy, VERDICT_OK, ids, ["No additional identity check is required for this facet."])
    pack_definition = pack_policy_definition(inputs, "identity_policy", policy)
    if pack_definition is not None:
        return pack_policy_manual_result(policy, ids, pack_definition)
    undeclared = undeclared_pack_policy_result(policy, ids, "identity_policy")
    if undeclared is not None:
        return undeclared

    pass_reasons: list[str] = []
    fail_reasons: list[str] = []
    review_reasons: list[str] = []
    if policy == "standard_designation_matches_registry":
        return evaluate_standard_designation_identity(policy, ids, present, inputs, facet)
    if policy == "registry_entry_matches_product_requirement":
        return evaluate_product_requirement_identity(policy, ids, present, inputs, facet)
    for source_id in present:
        if policy == "citation_id_resolves":
            evidence = citation_identity_evidence(inputs, source_id)
            if evidence.fail_reasons:
                fail_reasons.extend(evidence.fail_reasons)
            elif evidence.pass_reasons:
                pass_reasons.extend(evidence.pass_reasons)
            else:
                fail_reasons.append(f"{source_id} lacks validated local citation identity metadata.")
        elif policy == "origin_url_matches_candidate":
            origin = canonical_url(origin_url(inputs, source_id))
            matches = [
                candidate
                for candidate in linked_candidates(inputs, source_id)
                if canonical_url(candidate.get("url") if isinstance(candidate.get("url"), str) else None) == origin
            ]
            if origin and matches:
                candidate_ids = ", ".join(str(candidate.get("candidate_id", "<unknown>")) for candidate in matches)
                pass_reasons.append(f"{source_id} origin URL matches selected candidate(s): {candidate_ids}.")
            else:
                review_reasons.append(f"{source_id} has no selected candidate with a matching origin URL.")
        elif policy == "repo_ref_resolves":
            evidence = repository_identity_evidence(inputs, source_id, accepted_artifact_kinds)
            if evidence.fail_reasons:
                fail_reasons.extend(evidence.fail_reasons)
            elif evidence.pass_reasons:
                pass_reasons.extend(evidence.pass_reasons)
            else:
                review_reasons.append(f"{source_id} lacks local repository/ref identity metadata.")
        elif policy == "official_domain_match":
            matched, reason = source_matches_official_domain(inputs, source_id)
            if matched and reason:
                pass_reasons.append(reason)
            elif reason:
                fail_reasons.append(reason)
            else:
                review_reasons.append(f"{source_id} origin URL does not match a configured official domain.")
        else:
            review_reasons.append(f"Identity policy {policy!r} has no offline evaluator in this helper.")

    if policy == "citation_id_resolves":
        if missing:
            return missing_result(policy, ids, missing)
        if fail_reasons:
            return result(
                policy,
                VERDICT_FAIL,
                ids,
                [*pass_reasons, *fail_reasons],
                "Record a valid DOI, arXiv ID, or OpenAlex work ID plus title metadata before accepting this citation.",
            )
        return result(policy, VERDICT_OK, ids, pass_reasons)
    if pass_reasons:
        return result(policy, VERDICT_OK, ids, pass_reasons)
    if fail_reasons:
        return result(
            policy,
            VERDICT_FAIL,
            ids,
            fail_reasons,
            "Use a source whose origin matches an allowed official domain, or update the jurisdiction profile.",
        )
    if missing:
        return missing_result(policy, ids, missing)
    return manual_result(policy, ids, review_reasons)


def evaluate_facet_policies(facet: dict[str, Any], inputs: PolicyInputs) -> list[PolicyResult]:
    source_ids = normalize_source_ids(facet.get("accepted_source_ids"))
    artifact_kinds = accepted_repository_artifact_kinds(facet.get("accepted_artifact_kinds"))
    return [
        evaluate_source_policy(str(facet.get("source_policy", "")), source_ids, inputs, artifact_kinds),
        evaluate_freshness_policy(str(facet.get("freshness_policy", "")), source_ids, inputs, artifact_kinds),
        evaluate_identity_policy(str(facet.get("identity_policy", "")), source_ids, inputs, artifact_kinds, facet),
    ]


def evaluate_coverage_manifest_policies(manifest: dict[str, Any], inputs: PolicyInputs) -> dict[str, Any]:
    facets: list[dict[str, Any]] = []
    for section in ("required_facets", "optional_facets"):
        raw_facets = manifest.get(section)
        if not isinstance(raw_facets, list):
            continue
        for facet in raw_facets:
            if not isinstance(facet, dict):
                continue
            facets.append(
                {
                    "facet_id": facet.get("facet_id"),
                    "required": facet.get("required"),
                    "evidence_path": facet.get("evidence_path"),
                    "policy_results": [policy_result.to_dict() for policy_result in evaluate_facet_policies(facet, inputs)],
                }
            )
    return {
        "question_slug": manifest.get("question_slug"),
        "coverage_profile": manifest.get("coverage_profile"),
        "facets": facets,
    }
