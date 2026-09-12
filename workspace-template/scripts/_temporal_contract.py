#!/usr/bin/env python3
"""Opt-in source clocks and independently authenticated public availability."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from _evidence_authority import EvidenceInvalid, bounded_list, digest, exact_object, name, timestamp, verify_attestation
from _record_artifacts import artifact_path

SOURCE_SCHEMA = "evidence-temporal-source/v1"
AVAILABILITY_SCHEMA = "evidence-availability-receipt/v1"
REQUEST_SCHEMA = "evidence-temporal-request/v1"
RESULT_SCHEMA = "evidence-temporal-result/v1"
MODES = frozenset({"current", "historical-audit", "historical-available"})
BOUNDS = {"sources": 32, "revisions": 64, "lineage": 128, "artifact_bytes": 8 * 1024 * 1024,
          "request_bytes": 256 * 1024, "facets": 32, "grounding": 128, "query_characters": 1024}


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise EvidenceInvalid(reason)


def claim(value: Any) -> datetime | None:
    item = exact_object(value, {"value", "basis"})
    if item["value"] is None:
        require(item["basis"] == "unknown", "temporal_unknown_basis_required")
        return None
    require(item["basis"] in {"source-declared", "provider-asserted"}, "temporal_claim_basis_unsupported")
    return timestamp(item["value"])


def interval(value: Any, *, open_end: bool = False) -> tuple[datetime | None, datetime | None] | None:
    if value is None:
        return None
    item = exact_object(value, {"start", "end"})
    start = claim(item["start"])
    end = None if open_end and item["end"] is None else claim(item["end"])
    require(start is None or end is None or start <= end, "temporal_interval_invalid")
    return start, end


def source_times(record: dict[str, Any]) -> dict[str, Any]:
    value = record["descriptor"]["temporal"]
    require(bool(value), "temporal_metadata_unsupported")
    temporal = exact_object(value, {"schema_version", "asserting_principal", "measurement", "published_at", "available_at",
                                    "claimed_retrieved_at", "effective", "expires_at", "supersedes", "record_path", "provenance_path"})
    require(temporal["schema_version"] == SOURCE_SCHEMA, "temporal_contract_unsupported")
    name(temporal["asserting_principal"])
    published, available, retrieved = (claim(temporal[key]) for key in ("published_at", "available_at", "claimed_retrieved_at"))
    measurement, effective = interval(temporal["measurement"]), interval(temporal["effective"], open_end=True)
    expires = None if temporal["expires_at"] is None else claim(temporal["expires_at"])
    require(published is None or available is None or published <= available, "temporal_publication_order_invalid")
    require(retrieved is None or available is None or available <= retrieved, "temporal_retrieval_order_invalid")
    require(retrieved is None or retrieved <= timestamp(record["observed_at"]), "temporal_claim_exceeds_host_observation")
    require(expires is None or available is None or available < expires, "temporal_expiry_order_invalid")
    if temporal["supersedes"] is not None:
        digest(temporal["supersedes"])
    for key in ("record_path", "provenance_path"):
        path = temporal[key]
        if path is not None:
            require(artifact_path(path) in record["files"] and path.endswith(".json"), "temporal_structured_artifact_missing")
    return {"published": published, "available": available, "retrieved": retrieved, "measurement": measurement,
            "effective": effective, "expires": expires, "claims": temporal}


def availability_receipt(body: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Bind an inert receipt to an exact stored revision and its proof artifacts."""
    body = exact_object(body, {"source_id", "source_revision", "receipt"})
    require(body["source_id"] == record["source_id"], "availability_source_binding_mismatch")
    digest(body["source_revision"])
    envelope = exact_object(body["receipt"], {"payload", "authentication"})
    payload = exact_object(envelope["payload"], {"schema_version", "source_id", "source_revision", "asserting_principal",
                                                "published_at", "available_at", "method", "proof_artifacts"})
    require(payload["schema_version"] == AVAILABILITY_SCHEMA and all(payload[key] == body[key] for key in
            ("source_id", "source_revision")), "availability_revision_binding_mismatch")
    times = source_times(record)
    require(name(payload["asserting_principal"]) == times["claims"]["asserting_principal"], "availability_asserting_principal_mismatch")
    published, available = timestamp(payload["published_at"]), timestamp(payload["available_at"])
    require(published == times["published"] and available == times["available"], "availability_timestamp_binding_mismatch")
    require(payload["method"] in {"public-archive", "publisher-publication-record"}, "availability_method_unsupported")
    seen = set()
    for proof in bounded_list(payload["proof_artifacts"], maximum=16, minimum=1):
        exact_object(proof, {"path", "content_hash"})
        path = artifact_path(proof["path"])
        require(path not in seen and path in record["files"], "availability_proof_artifact_missing_or_duplicate")
        seen.add(path)
        require(digest(proof["content_hash"]) == "sha256:" + hashlib.sha256(record["files"][path]).hexdigest(),
                "availability_proof_artifact_mismatch")
    return payload


def authenticate_availability(body: dict[str, Any], record: dict[str, Any], trust: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Only an independently controlled host-selected availability principal can qualify public time."""
    payload = availability_receipt(body, record)
    authentication = verify_attestation(body["receipt"], "availability", trust, now)
    require(authentication["authenticated"], authentication["reason"])
    supplier = trust["policy"]["principals"].get(payload["asserting_principal"])
    require(supplier is not None and supplier["controller"] != authentication["controller"], "availability_verifier_not_independent")
    require(timestamp(payload["available_at"]) <= timestamp(authentication["issued_at"]), "availability_receipt_precedes_publication")
    return authentication


def request_document(value: Any) -> dict[str, Any]:
    request = exact_object(value, {"schema_version", "mode", "cutoff", "checkpoint", "source_ids", "purpose", "consumer", "analysis"})
    require(request["schema_version"] == REQUEST_SCHEMA and request["mode"] in MODES, "temporal_request_unsupported")
    if request["mode"] == "current":
        require(request["cutoff"] is None and request["checkpoint"] is None, "temporal_current_clock_is_host_owned")
    else:
        timestamp(request["cutoff"])
        digest(request["checkpoint"])
    sources = [name(value) for value in bounded_list(request["source_ids"], maximum=BOUNDS["sources"], minimum=1)]
    require(len(set(sources)) == len(sources), "temporal_duplicate_source")
    name(request["purpose"])
    name(request["consumer"])
    analysis = exact_object(request["analysis"], {"query", "facets", "grounding", "domain_pack", "question_frontmatter"})
    require(isinstance(analysis["query"], str) and len(analysis["query"]) <= BOUNDS["query_characters"], "temporal_query_bound_exceeded")
    require(isinstance(analysis["domain_pack"], dict) and isinstance(analysis["question_frontmatter"], dict), "temporal_analysis_invalid")
    for key, fields in (("facets", {"id", "source_ids", "policy_ids"}), ("grounding", {"id", "source_id", "pointer", "expected"})):
        ids = set()
        for item in bounded_list(analysis[key], maximum=BOUNDS[key]):
            exact_object(item, fields)
            require(name(item["id"]) not in ids, "temporal_duplicate_analysis_id")
            ids.add(item["id"])
            if key == "facets":
                selected = [name(source) for source in bounded_list(item["source_ids"], maximum=BOUNDS["sources"], minimum=1)]
                require(len(selected) == len(set(selected)) and set(selected) <= set(sources), "temporal_facet_source_scope_invalid")
                policies = [name(policy) for policy in bounded_list(item["policy_ids"], maximum=32, minimum=1)]
                require(len(policies) == len(set(policies)), "temporal_duplicate_policy")
            else:
                require(item["source_id"] in sources and isinstance(item["pointer"], str) and item["pointer"].startswith("/")
                        and len(item["pointer"]) <= 1024, "temporal_grounding_scope_invalid")
                require(item["expected"] is None or type(item["expected"]) in {str, int, float, bool}, "temporal_grounding_scalar_required")
    return request


def qualify_time(record: dict[str, Any], times: dict[str, Any], cutoff: datetime, mode: str) -> None:
    require(times["published"] is not None and times["available"] is not None, "temporal_availability_unknown")
    require(times["published"] <= cutoff and times["available"] <= cutoff, "temporal_future_publication")
    if mode != "historical-available":
        require(timestamp(record["observed_at"]) <= cutoff, "temporal_not_host_observed_at_cutoff")
    measured = times["measurement"]
    if measured is not None:
        require(all(value is not None for value in measured), "temporal_measurement_unknown")
        require(measured[1] <= cutoff, "temporal_measurement_incomplete_at_cutoff")
    effective = times["effective"]
    if effective is not None:
        require(effective[0] is not None and (times["claims"]["effective"]["end"] is None or effective[1] is not None),
                "temporal_effective_interval_unknown")
        require(effective[0] <= cutoff and (effective[1] is None or cutoff < effective[1]), "temporal_outside_effective_interval")
    require(times["claims"]["expires_at"] is None or times["expires"] is not None, "temporal_expiry_unknown")
    require(times["expires"] is None or cutoff < times["expires"], "temporal_expired_at_cutoff")
