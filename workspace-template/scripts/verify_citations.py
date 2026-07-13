#!/usr/bin/env python3
"""Verify academic citation identifiers from recorded or live provider metadata.

The default mode is network-free: it reads the manifest, normalized records,
and provenance sidecars and reports whether academic citation identity is
already provider-backed locally. Live re-resolution is opt-in with
``--live --provider arxiv|openalex`` and still obeys the acquisition gate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_NO_SHIP = 1
EXIT_INVALID = 2
RESULT_VALUES = ("verified", "mismatch", "not_found", "skipped_no_live", "insufficient_metadata")
SUPPORTED_PROVIDERS = ("arxiv", "openalex")
OPENALEX_VERIFY_SELECT_FIELDS = (
    "id,doi,display_name,publication_year,type,authorships,"
    "primary_location,best_oa_location,open_access,locations"
)

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _workspace_module_loader import load_workspace_module

_academic_identity = load_workspace_module(_SCRIPT_DIR, "_academic_identity")
author_sets_match = _academic_identity.author_sets_match
_script_errors = load_workspace_module(_SCRIPT_DIR, "_script_errors")
emit_error = _script_errors.emit_error
handle_system_exit = _script_errors.handle_system_exit
json_mode_requested = _script_errors.json_mode_requested
policies = load_workspace_module(_SCRIPT_DIR, "_evidence_policies")
fetch_sources = load_workspace_module(_SCRIPT_DIR, "fetch_sources")


class VerifyCitationsError(Exception):
    """Fatal verifier setup or provider error with a stable machine code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        recoverable: bool = True,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.recoverable = recoverable
        self.remediation = remediation
        self.details = details or {}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify academic citation identifiers.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument("--format", choices=("json",), default="json", help="Report format. Defaults to json.")
    parser.add_argument("--output", help="Optional path to write the JSON report instead of stdout.")
    parser.add_argument("--source-id", action="append", default=None, help="Limit verification to one source id. Repeatable.")
    parser.add_argument("--live", action="store_true", help="Re-resolve identifiers against the selected provider.")
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS, help="Live provider to use with --live.")
    return parser.parse_args(argv)


def compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact_secrets(value: Any) -> Any:
    """Return a JSON-safe copy with provider URLs and known secret values removed."""
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for secret_name in ("OPENALEX_API_KEY", "GITHUB_TOKEN"):
        secret = os.environ.get(secret_name)
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(r"\bapi_key=[^\s&]+", "<redacted-query-param>", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://api\.openalex\.org/\S+", "<redacted-openalex-url>", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://export\.arxiv\.org/\S+", "<redacted-arxiv-url>", text, flags=re.IGNORECASE)
    return text


def source_exists(inputs: policies.PolicyInputs, source_id: str) -> bool:
    return source_id in inputs.manifest_records or source_id in inputs.normalized_records


def unique_source_ids(values: list[str] | None) -> list[str]:
    ids: list[str] = []
    for value in values or []:
        if isinstance(value, str) and value.strip() and value.strip() not in ids:
            ids.append(value.strip())
    return ids


def is_academic_source(inputs: policies.PolicyInputs, source_id: str) -> bool:
    record = inputs.manifest_records.get(source_id, {})
    normalized = inputs.normalized_records.get(source_id, {})
    kind = record.get("kind")
    source_kind = normalized.get("source_kind")
    return (
        source_id.startswith("paper:")
        or kind == "paper"
        or source_kind == "paper"
        or bool(policies.citation_ids(inputs, source_id))
    )


def select_source_ids(inputs: policies.PolicyInputs, requested: list[str] | None) -> tuple[list[str], dict[str, Any]]:
    requested_ids = unique_source_ids(requested)
    if requested_ids:
        return requested_ids, {"type": "source_id", "source_ids": requested_ids}
    ids: list[str] = []
    for source_id in sorted(set(inputs.manifest_records) | set(inputs.normalized_records)):
        if is_academic_source(inputs, source_id) and policies.citation_ids(inputs, source_id):
            ids.append(source_id)
    return ids, {"type": "manifest_papers", "source_ids": ids}


def citation_identifiers(inputs: policies.PolicyInputs, source_id: str) -> dict[str, str]:
    return {
        key: value
        for key, value in policies.citation_ids(inputs, source_id).items()
        if key in {"arxiv_id", "openalex_id", "doi"}
    }


def local_title(inputs: policies.PolicyInputs, source_id: str) -> str | None:
    return policies.citation_title(inputs, source_id)


def local_title_source(inputs: policies.PolicyInputs, source_id: str) -> str | None:
    value = policies.source_value(inputs, source_id, ("title_source",))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def local_year(inputs: policies.PolicyInputs, source_id: str) -> int | None:
    for key in ("publication_year", "year"):
        value = policies.source_value(inputs, source_id, (key,))
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 1000 <= value <= 9999:
            return value
        if isinstance(value, str) and re.fullmatch(r"\d{4}", value.strip()):
            return int(value.strip())
    for key in ("date", "published", "publication_date"):
        parsed = policies.date_from_value(policies.source_value(inputs, source_id, (key,)))
        if parsed is not None:
            return parsed.year
    return None


def normalized_author_names(value: Any) -> list[str]:
    names = policies.author_names_from_value(value)
    if names is None:
        return []
    return [normalize_text(name) for name in names if normalize_text(name)]


def local_authors(inputs: policies.PolicyInputs, source_id: str) -> list[str]:
    for key in ("authors", "authorships"):
        names = normalized_author_names(policies.source_value(inputs, source_id, (key,)))
        if names:
            return names
    return []


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def provider_backed_locally(inputs: policies.PolicyInputs, identifiers: dict[str, str], source_id: str) -> bool:
    provenance = inputs.provenance_by_source_id.get(source_id, {})
    retrieved_by = provenance.get("retrieved_by")
    if not isinstance(retrieved_by, str):
        return False
    if "arxiv_id" in identifiers and retrieved_by == "fetch_sources.py/arxiv":
        return True
    if any(key in identifiers for key in ("openalex_id", "doi")) and retrieved_by == "fetch_sources.py/openalex":
        return True
    return False


def recorded_provenance_identifiers(provenance: dict[str, Any]) -> dict[str, str]:
    """Normalize provider identifiers retained in acquisition provenance."""
    identifiers: dict[str, str] = {}
    for key, provenance_key in (
        ("arxiv_id", "arxiv_id"),
        ("openalex_id", "openalex_work_id"),
        ("doi", "doi"),
    ):
        normalized = policies.normalize_citation_identifier(key, provenance.get(provenance_key))
        if normalized:
            identifiers[key] = normalized
    origin_url = provenance.get("origin_url")
    for key in ("arxiv_id", "openalex_id", "doi"):
        if key in identifiers:
            continue
        normalized = policies.normalize_citation_identifier(key, origin_url)
        if normalized:
            identifiers[key] = normalized
    return identifiers


def offline_identity_evidence(
    inputs: policies.PolicyInputs,
    source_id: str,
    identifiers: dict[str, str],
) -> dict[str, Any]:
    provenance = inputs.provenance_by_source_id.get(source_id, {})
    retrieved_by = provenance.get("retrieved_by")
    if retrieved_by == "fetch_sources.py/arxiv":
        provider = "arxiv"
        relevant_keys = ("arxiv_id",)
    elif retrieved_by == "fetch_sources.py/openalex":
        provider = "openalex"
        relevant_keys = ("openalex_id", "doi")
    else:
        provider = None
        relevant_keys = ()
    retained = recorded_provenance_identifiers(provenance)
    matched_keys: list[str] = []
    conflicts: list[dict[str, str]] = []
    for key in relevant_keys:
        local_value = identifiers.get(key)
        retained_value = retained.get(key)
        if local_value is None or retained_value is None:
            continue
        matched = (
            arxiv_ids_match(local_value, retained_value)
            if key == "arxiv_id"
            else local_value == retained_value
        )
        if matched:
            matched_keys.append(key)
        else:
            conflicts.append({"identifier": key, "local": local_value, "provenance": retained_value})
    return {
        "provider": provider,
        "retrieved_by": retrieved_by,
        "local_identifiers": {key: identifiers[key] for key in relevant_keys if key in identifiers},
        "provenance_identifiers": {key: retained[key] for key in relevant_keys if key in retained},
        "matched_keys": matched_keys,
        "conflicts": conflicts,
        "quorum_met": bool(provider and matched_keys and not conflicts),
    }


def has_recorded_academic_identity(inputs: policies.PolicyInputs, source_id: str) -> bool:
    provenance = inputs.provenance_by_source_id.get(source_id, {})
    retrieved_by = provenance.get("retrieved_by")
    academic_provider = provenance.get("academic_provider")
    return retrieved_by in {"fetch_sources.py/arxiv", "fetch_sources.py/openalex"} or academic_provider in {
        "arxiv",
        "openalex",
    }


def recorded_openalex_identity_conflict(inputs: policies.PolicyInputs, source_id: str) -> bool:
    return policies.source_value(inputs, source_id, ("openalex_identity_conflict",)) is True


def recorded_doi_resolution_matches_arxiv(inputs: policies.PolicyInputs, source_id: str) -> bool:
    value = policies.source_value(inputs, source_id, ("doi_resolution",))
    if not isinstance(value, dict):
        return False
    return value.get("matches_arxiv_id") is True


def base_result(
    source_id: str,
    *,
    result: str,
    identifiers: dict[str, str],
    title: str | None,
    reasons: list[str],
    provider: str | None,
    mode: str,
    title_source: str | None = None,
    comparisons: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source_id": source_id,
        "result": result,
        "mode": mode,
        "provider": provider,
        "identifiers": identifiers,
        "title": title,
        "reasons": redact_secrets(reasons),
        "artifacts": ["sources/manifest.jsonl", source_id],
        "policy": "citation_identity_quorum",
        "remediation": (
            "No remediation required."
            if result == "verified"
            else "Correct the retained identifiers or provenance, reacquire the exact work when needed, and rerun citation verification."
        ),
    }
    if title_source:
        record["title_source"] = title_source
    if comparisons is not None:
        record["comparisons"] = redact_secrets(comparisons)
    return record


def insufficient_result(
    inputs: policies.PolicyInputs,
    source_id: str,
    *,
    provider: str | None,
    mode: str,
) -> dict[str, Any] | None:
    identifiers = citation_identifiers(inputs, source_id)
    title = local_title(inputs, source_id)
    reasons: list[str] = []
    if not source_exists(inputs, source_id):
        reasons.append(f"{source_id} is not present in manifest or normalized records.")
    if not identifiers:
        reasons.append(f"{source_id} has no local arXiv, OpenAlex, or DOI metadata.")
    if not title:
        reasons.append(f"{source_id} has no title metadata for citation verification.")
    if not reasons:
        return None
    return base_result(
        source_id,
        result="insufficient_metadata",
        identifiers=identifiers,
        title=title,
        reasons=reasons,
        provider=provider,
        mode=mode,
        title_source=local_title_source(inputs, source_id),
    )


def verify_local(inputs: policies.PolicyInputs, source_id: str) -> dict[str, Any]:
    insufficient = insufficient_result(inputs, source_id, provider=None, mode="local")
    if insufficient is not None:
        return insufficient
    identifiers = citation_identifiers(inputs, source_id)
    title = local_title(inputs, source_id)
    identity = offline_identity_evidence(inputs, source_id, identifiers)
    if identity["conflicts"]:
        return base_result(
            source_id,
            result="mismatch",
            identifiers=identifiers,
            title=title,
            reasons=[
                f"{source_id} local citation identifiers conflict with retained provider acquisition provenance."
            ],
            provider=identity["provider"],
            mode="local",
            title_source=local_title_source(inputs, source_id),
            comparisons={"offline_identity": identity},
        )
    if identity["quorum_met"]:
        return base_result(
            source_id,
            result="verified",
            identifiers=identifiers,
            title=title,
            reasons=[
                f"{source_id} normalized citation identity matches retained provider acquisition provenance."
            ],
            provider=identity["provider"],
            mode="local",
            title_source=local_title_source(inputs, source_id),
            comparisons={"offline_identity": identity},
        )
    if identity["provider"]:
        return base_result(
            source_id,
            result="insufficient_metadata",
            identifiers=identifiers,
            title=title,
            reasons=[
                f"{source_id} has provider acquisition provenance but no matching retained provider identifier."
            ],
            provider=identity["provider"],
            mode="local",
            title_source=local_title_source(inputs, source_id),
            comparisons={"offline_identity": identity},
        )
    return base_result(
        source_id,
        result="skipped_no_live",
        identifiers=identifiers,
        title=title,
        reasons=[
            (
                f"{source_id} has valid local citation metadata but no fetch_sources.py/arxiv or "
                "fetch_sources.py/openalex provenance; rerun with --live to re-resolve it."
            )
        ],
        provider=None,
        mode="local",
        title_source=local_title_source(inputs, source_id),
    )


def live_gate(project_root: Path, config: dict[str, Any], provider: str | None) -> str:
    if provider is None:
        raise VerifyCitationsError(
            "VALUE_INVALID",
            "--provider is required when --live is set.",
            remediation="Pass --provider arxiv or --provider openalex.",
        )
    acquisition = fetch_sources.acquisition_config(config)
    providers = fetch_sources.validate_provider_list(
        acquisition.get("providers", []),
        "integrations.acquisition.providers",
        require_non_empty=True,
    )
    fetch_sources.require_provider_allowed(provider, providers)
    return provider


def arxiv_ids_match(local_id: str, provider_id: str) -> bool:
    local = local_id.casefold()
    provider = provider_id.casefold()
    if local == provider:
        return True
    return "v" not in local and provider.startswith(f"{local}v")


def arxiv_base_id(value: str) -> str:
    return re.sub(r"v\d+$", "", value.casefold())


def fetch_arxiv_record(arxiv_id: str) -> dict[str, Any] | None:
    url = fetch_sources.arxiv_query_url(None, [arxiv_id], 1)
    try:
        records = fetch_sources.parse_arxiv_atom(fetch_sources.arxiv_fetch_url(url))
    except fetch_sources.FetchSourcesError as exc:
        message = redact_secrets(exc.message)
        if "HTTP 404" in str(message):
            return None
        raise VerifyCitationsError(
            exc.error_code,
            "arXiv citation verification request failed.",
            remediation=exc.remediation,
            details={"provider_error": message},
        ) from exc
    version_conflict: dict[str, Any] | None = None
    for record in records:
        found = record.get("id")
        if isinstance(found, str) and arxiv_ids_match(arxiv_id, found):
            return record
        if isinstance(found, str) and arxiv_base_id(arxiv_id) == arxiv_base_id(found):
            version_conflict = record
    return version_conflict


def openalex_identifier(identifiers: dict[str, str]) -> str | None:
    if identifiers.get("openalex_id"):
        return identifiers["openalex_id"]
    if identifiers.get("doi"):
        return f"doi:{identifiers['doi']}"
    return None


def fetch_openalex_record(identifier: str) -> dict[str, Any] | None:
    url = fetch_sources.openalex_url(
        fetch_sources.openalex_work_api_path(identifier),
        {"select": OPENALEX_VERIFY_SELECT_FIELDS},
    )
    try:
        return fetch_sources.openalex_json_response(fetch_sources.openalex_fetch_url(url))
    except fetch_sources.FetchSourcesError as exc:
        message = redact_secrets(exc.message)
        if "HTTP 404" in str(message):
            return None
        raise VerifyCitationsError(
            exc.error_code,
            "OpenAlex citation verification request failed.",
            remediation=exc.remediation,
            details={"provider_error": message},
        ) from exc


def provider_year(record: dict[str, Any]) -> int | None:
    value = record.get("publication_year")
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1000 <= value <= 9999:
        return value
    for key in ("published", "updated", "publication_date"):
        parsed = policies.date_from_value(record.get(key))
        if parsed is not None:
            return parsed.year
    return None


def provider_authors(record: dict[str, Any]) -> list[str]:
    for key in ("authors", "authorships"):
        names = normalized_author_names(record.get(key))
        if names:
            return names
    return []


def provider_openalex_identifiers(record: dict[str, Any]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    work_id = policies.normalize_citation_identifier("openalex_id", record.get("id"))
    doi = policies.normalize_citation_identifier("doi", record.get("doi"))
    if work_id:
        identifiers["openalex_id"] = work_id
    if doi:
        identifiers["doi"] = doi
    return identifiers


def compare_provider_record(
    inputs: policies.PolicyInputs,
    source_id: str,
    provider: str,
    local_identifiers: dict[str, str],
    record: dict[str, Any],
    *,
    require_authors: bool = False,
) -> tuple[str, list[str], dict[str, Any]]:
    local_title_value = local_title(inputs, source_id) or ""
    provider_title_value = (
        record.get("display_name") if provider == "openalex" else record.get("title")
    )
    provider_title = provider_title_value if isinstance(provider_title_value, str) else ""
    local_year_value = local_year(inputs, source_id)
    provider_year_value = provider_year(record)
    local_author_values = local_authors(inputs, source_id)
    provider_author_values = provider_authors(record)
    author_comparison = (
        {"matched": True, "matches": [], "unmatched_local": [], "unmatched_provider": provider_author_values}
        if not local_author_values and not require_authors
        else author_sets_match(local_author_values, provider_author_values)
    )

    comparisons: dict[str, Any] = {
        "title": {
            "local": local_title_value,
            "provider": provider_title,
            "matched": normalize_text(local_title_value) == normalize_text(provider_title),
        },
        "year": {
            "local": local_year_value,
            "provider": provider_year_value,
            "matched": local_year_value is None or local_year_value == provider_year_value,
        },
        "authors": {
            "local": local_author_values,
            "provider": provider_author_values,
            "matched": author_comparison["matched"],
            "matches": author_comparison["matches"],
            "unmatched_local": author_comparison["unmatched_local"],
            "unmatched_provider": author_comparison["unmatched_provider"],
        },
    }

    if provider == "arxiv":
        found_id = record.get("id")
        matched = isinstance(found_id, str) and arxiv_ids_match(local_identifiers.get("arxiv_id", ""), found_id)
        version_conflict = (
            isinstance(found_id, str)
            and not matched
            and arxiv_base_id(local_identifiers.get("arxiv_id", "")) == arxiv_base_id(found_id)
        )
        comparisons["arxiv_id"] = {
            "local": local_identifiers.get("arxiv_id"),
            "provider": found_id,
            "matched": matched,
            "version_conflict": version_conflict,
        }
    elif provider == "openalex":
        provider_ids = provider_openalex_identifiers(record)
        if local_identifiers.get("openalex_id"):
            matched = local_identifiers["openalex_id"] == provider_ids.get("openalex_id")
            comparisons["openalex_id"] = {
                "local": local_identifiers.get("openalex_id"),
                "provider": provider_ids.get("openalex_id"),
                "matched": matched,
            }
        if local_identifiers.get("doi"):
            matched = local_identifiers["doi"] == provider_ids.get("doi")
            comparisons["doi"] = {
                "local": local_identifiers.get("doi"),
                "provider": provider_ids.get("doi"),
                "matched": matched,
            }

    if provider == "openalex":
        id_keys = [key for key in ("openalex_id", "doi") if key in comparisons]
        identifiers_match = bool(id_keys) and all(comparisons[key]["matched"] for key in id_keys)
        identity_recorded = has_recorded_academic_identity(inputs, source_id)
        title_or_year_lag = not comparisons["title"]["matched"] or not comparisons["year"]["matched"]
        if (
            title_or_year_lag
            and comparisons["authors"]["matched"]
            and identifiers_match
            and identity_recorded
        ):
            if not comparisons["title"]["matched"]:
                comparisons["title"]["lag_suspected"] = True
            if not comparisons["year"]["matched"]:
                comparisons["year"]["lag_suspected"] = True
            return (
                "verified",
                [
                    (
                        "openalex_title_version_lag: OpenAlex title/year diverged, but recorded identifiers "
                        f"{', '.join(id_keys)} and canonical authors matched for {source_id}."
                    )
                ],
                comparisons,
            )
        if not comparisons["title"]["matched"] and not comparisons["authors"]["matched"]:
            comparisons["openalex_identity_conflict"] = {
                "recorded": recorded_openalex_identity_conflict(inputs, source_id),
                "doi_resolution_matches_arxiv": recorded_doi_resolution_matches_arxiv(inputs, source_id),
            }
            if (
                comparisons["openalex_identity_conflict"]["recorded"]
                and comparisons["openalex_identity_conflict"]["doi_resolution_matches_arxiv"]
            ):
                return (
                    "verified",
                    [
                        (
                            "openalex_identity_conflict_recorded: OpenAlex returned a divergent work record, "
                            f"and enrichment recorded the conflict for {source_id}."
                        ),
                        (
                            "openalex_identity_quorum_verified: arXiv identity and DOI resolution corroborate "
                            f"the local citation identity for {source_id}."
                        ),
                    ],
                    comparisons,
                )
            reason_code = (
                "openalex_identity_conflict_uncorroborated"
                if comparisons["openalex_identity_conflict"]["recorded"]
                else "openalex_identity_conflict_unrecorded"
            )
            return (
                "mismatch",
                [
                    (
                        f"{reason_code}: OpenAlex title and authors diverged for {source_id}; "
                        "recorded quorum evidence was absent or incomplete."
                    )
                ],
                comparisons,
            )

    mismatches: list[str] = []
    if not comparisons["title"]["matched"]:
        mismatches.append(f"{source_id} title mismatch between local metadata and {provider}.")
    if not comparisons["year"]["matched"]:
        mismatches.append(f"{source_id} publication year mismatch between local metadata and {provider}.")
    if not comparisons["authors"]["matched"]:
        mismatches.append(f"{source_id} author mismatch between local metadata and {provider}.")
    if provider == "arxiv" and not comparisons["arxiv_id"]["matched"]:
        mismatch_kind = "version mismatch" if comparisons["arxiv_id"]["version_conflict"] else "id mismatch"
        mismatches.append(f"{source_id} arXiv {mismatch_kind} between local metadata and provider record.")
    if provider == "openalex":
        if "openalex_id" in comparisons and not comparisons["openalex_id"]["matched"]:
            mismatches.append(f"{source_id} OpenAlex work id mismatch between local metadata and provider record.")
        if "doi" in comparisons and not comparisons["doi"]["matched"]:
            mismatches.append(f"{source_id} DOI mismatch between local metadata and provider record.")

    if mismatches:
        return "mismatch", mismatches, comparisons
    return "verified", [f"{source_id} re-resolved against {provider} and matched local metadata."], comparisons


def verify_live(inputs: policies.PolicyInputs, source_id: str, provider: str) -> dict[str, Any]:
    insufficient = insufficient_result(inputs, source_id, provider=provider, mode="live")
    if insufficient is not None:
        return insufficient
    identifiers = citation_identifiers(inputs, source_id)
    title_source = local_title_source(inputs, source_id)
    provider_backed = provider_backed_locally(inputs, identifiers, source_id)
    if provider_backed and not local_authors(inputs, source_id):
        return base_result(
            source_id,
            result="insufficient_metadata",
            identifiers=identifiers,
            title=local_title(inputs, source_id),
            reasons=[
                (
                    f"{source_id} has provider-backed acquisition provenance but no local author metadata; "
                    "rerun fetch_sources.py openalex enrich, source_inventory.py, and "
                    "normalize_sources.py --all before live verification."
                )
            ],
            provider=provider,
            mode="live",
            title_source=title_source,
        )
    if provider == "arxiv":
        arxiv_id = identifiers.get("arxiv_id")
        if arxiv_id is None:
            return base_result(
                source_id,
                result="insufficient_metadata",
                identifiers=identifiers,
                title=local_title(inputs, source_id),
                reasons=[f"{source_id} has no arXiv id for live arxiv verification."],
                provider=provider,
                mode="live",
                title_source=title_source,
            )
        record = fetch_arxiv_record(arxiv_id)
    else:
        identifier = openalex_identifier(identifiers)
        if identifier is None:
            return base_result(
                source_id,
                result="insufficient_metadata",
                identifiers=identifiers,
                title=local_title(inputs, source_id),
                reasons=[f"{source_id} has no OpenAlex id or DOI for live OpenAlex verification."],
                provider=provider,
                mode="live",
                title_source=title_source,
            )
        record = fetch_openalex_record(identifier)
    if record is None:
        return base_result(
            source_id,
            result="not_found",
            identifiers=identifiers,
            title=local_title(inputs, source_id),
            reasons=[f"{source_id} was not found by live {provider} re-resolution."],
            provider=provider,
            mode="live",
            title_source=title_source,
        )
    result, reasons, comparisons = compare_provider_record(
        inputs,
        source_id,
        provider,
        identifiers,
        record,
        require_authors=provider_backed,
    )
    return base_result(
        source_id,
        result=result,
        identifiers=identifiers,
        title=local_title(inputs, source_id),
        reasons=reasons,
        provider=provider,
        mode="live",
        title_source=title_source,
        comparisons=comparisons,
    )


def result_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {value: 0 for value in RESULT_VALUES}
    for result in results:
        value = result.get("result")
        if isinstance(value, str) and value in counts:
            counts[value] += 1
    counts["total"] = len(results)
    return counts


def build_report(project_root: Path, args: argparse.Namespace) -> dict[str, Any]:
    config = fetch_sources.load_config(project_root)
    inputs = policies.load_policy_inputs(project_root, config)
    source_ids, source_scope = select_source_ids(inputs, args.source_id)
    warnings: list[str] = []
    provider = live_gate(project_root, config, args.provider) if args.live else None
    mode = "live" if args.live else "local"

    results: list[dict[str, Any]] = []
    for source_id in source_ids:
        results.append(verify_live(inputs, source_id, provider) if args.live else verify_local(inputs, source_id))
    if not source_ids:
        warnings.append("No citation-bearing academic sources were selected for verification.")

    counts = result_counts(results)
    overall_result = "verified" if results and counts["verified"] == len(results) else "no_ship"
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_utc(),
        "mode": mode,
        "provider": provider,
        "network_io_executed": bool(args.live and source_ids),
        "source_scope": source_scope,
        "counts": counts,
        "overall_result": overall_result,
        "warnings": redact_secrets(warnings),
        "results": redact_secrets(results),
    }


def write_report(report: dict[str, Any], output: str | None) -> None:
    rendered = compact_json(redact_secrets(report))
    if output:
        Path(output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=True)
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        report = build_report(project_root, args)
    except VerifyCitationsError as exc:
        emit_error(
            redact_secrets(exc.message),
            json_mode=json_mode,
            error_code=exc.error_code,
            recoverable=exc.recoverable,
            remediation=redact_secrets(exc.remediation),
            details=redact_secrets(exc.details),
        )
        return EXIT_INVALID
    except fetch_sources.FetchSourcesError as exc:
        emit_error(
            "Citation verification provider request failed.",
            json_mode=json_mode,
            error_code=exc.error_code,
            recoverable=exc.recoverable,
            remediation=redact_secrets(exc.remediation),
            details={"provider_error": redact_secrets(exc.message)},
        )
        return EXIT_INVALID
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=EXIT_INVALID)

    write_report(report, args.output)
    return EXIT_OK if report["overall_result"] == "verified" else EXIT_NO_SHIP


if __name__ == "__main__":
    raise SystemExit(main())
