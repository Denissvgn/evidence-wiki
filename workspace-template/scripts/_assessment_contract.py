#!/usr/bin/env python3
"""Versioned, bounded assessment and refresh wire contracts."""

from __future__ import annotations

import re

from _evidence_authority import bounded_list, digest, exact_object, name, timestamp
from _evidence_revision import canonical_bytes, content_id
from _record_artifacts import json_document
from _temporal_contract import MODES, require

SCHEMA = "evidence-assessment/v1"
REQUEST_SCHEMA = "evidence-assessment-request/v1"
REFRESH_SCHEMA = "evidence-assessment-refresh/v1"
REFRESH_REQUEST = "evidence-assessment-refresh-request/v1"
CAPABILITIES = ["assessment-refresh/v1", "current-usage-check/v1", "selected-publication/v1", "whole-envelope-attestation/v1"]
MAX_BYTES = 1024 * 1024
MAX_ASSESSMENTS = 32


def questions(value):
    result = [name(item) for item in bounded_list(value, maximum=32, minimum=1)]
    require(all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item) for item in result), "assessment_question_invalid")
    require(len(set(result)) == len(result), "assessment_duplicate_question")
    return sorted(result)


def detached(value):
    data = canonical_bytes(value)
    require(len(data) <= MAX_BYTES, "assessment_transport_bound_exceeded")
    return json_document(data)


def request_document(value):
    value = exact_object(detached(value), {"schema_version", "question_slugs", "temporal", "purpose", "consumer", "expires_at", "review"})
    require(value["schema_version"] == REQUEST_SCHEMA, "assessment_request_unsupported")
    value["question_slugs"] = questions(value["question_slugs"])
    temporal = exact_object(value["temporal"], {"mode", "cutoff"})
    require(temporal["mode"] in MODES, "assessment_temporal_mode_unsupported")
    if temporal["mode"] == "current":
        require(temporal["cutoff"] is None, "assessment_current_clock_is_host_owned")
    else:
        timestamp(temporal["cutoff"])
    name(value["purpose"])
    name(value["consumer"])
    if value["expires_at"] is not None:
        timestamp(value["expires_at"])
    require(value["review"] in {"approved", "pending", "rejected"}, "assessment_review_unsupported")
    return value


def assessment_document(value):
    value = exact_object(value, {"schema_version", "assessment_id", "required_capabilities", "request", "evaluated_at", "expires_at", "inputs", "basis", "publication", "gaps", "recorded_eligible"})
    require(value["schema_version"] == SCHEMA, "assessment_contract_unsupported")
    require(value["required_capabilities"] == CAPABILITIES, "assessment_capabilities_unsupported")
    require(request_document(value["request"]) == value["request"], "assessment_request_not_canonical")
    timestamp(value["evaluated_at"])
    if value["expires_at"] is not None:
        timestamp(value["expires_at"])
    inputs = exact_object(value["inputs"], {"selected", "ancestors", "coverage", "complete"})
    require(isinstance(inputs["selected"], dict) and 1 <= len(inputs["selected"]) <= 64, "assessment_input_bound_exceeded")
    for source, revision in inputs["selected"].items():
        name(source)
        digest(revision)
    ancestors = [digest(item) for item in bounded_list(inputs["ancestors"], maximum=128, minimum=1)]
    require(ancestors == sorted(set(ancestors)) and set(inputs["selected"].values()) <= set(ancestors), "assessment_ancestry_incomplete")
    require(inputs["complete"] is True and inputs["coverage"] == "all_workspace_sources", "assessment_dependency_coverage_unsupported")
    basis = exact_object(value["basis"], {"workspace_revision", "configuration", "producer", "authority"})
    for field in ("workspace_revision", "configuration", "producer", "authority"):
        digest(basis[field])
    publication = exact_object(value["publication"], {"schema_version", "question_slugs", "duplicate_handling", "revision",
        "producer_id", "gate_scope", "verdict", "readiness", "export"})
    require(publication["schema_version"] == "evidence-selected-publication/v1"
            and publication["question_slugs"] == value["request"]["question_slugs"]
            and publication["producer_id"] == basis["producer"]
            and isinstance(publication["revision"], dict)
            and publication["revision"].get("revision_id") == basis["workspace_revision"], "assessment_publication_binding_invalid")
    require(isinstance(publication["readiness"], dict) and isinstance(publication["export"], dict)
            and publication["verdict"] in {"ship", "no_ship", "blocked_on_sources", "attention_required"}
            and publication["readiness"].get("verdict") == publication["verdict"]
            and isinstance(publication["export"].get("questions"), list), "assessment_publication_invalid")
    gaps = [name(item) for item in bounded_list(value["gaps"], maximum=256)]
    require(gaps == sorted(set(gaps)) and type(value["recorded_eligible"]) is bool and value["recorded_eligible"] == (not gaps), "assessment_outcome_inconsistent")
    require(value["assessment_id"] == content_id(SCHEMA, {key: item for key, item in value.items() if key != "assessment_id"}), "assessment_digest_mismatch")
    return value


def refresh_request(value):
    value = exact_object(detached(value), {"schema_version", "changed_sources", "evaluation_time", "limit", "cursor"})
    require(value["schema_version"] == REFRESH_REQUEST, "assessment_refresh_request_unsupported")
    changes = []
    for change in bounded_list(value["changed_sources"], maximum=128):
        exact_object(change, {"source_id", "source_revision"})
        name(change["source_id"])
        digest(change["source_revision"])
        changes.append((change["source_id"], change["source_revision"]))
    value["changed_sources"] = [{"source_id": source, "source_revision": revision} for source, revision in sorted(set(changes))]
    if value["evaluation_time"] is not None:
        timestamp(value["evaluation_time"])
    require(type(value["limit"]) is int and 1 <= value["limit"] <= MAX_ASSESSMENTS, "assessment_refresh_bound_invalid")
    if value["cursor"] is not None:
        cursor = exact_object(value["cursor"], {"basis", "after"})
        digest(cursor["basis"])
        digest(cursor["after"])
    return value


def refresh_document(value):
    value = exact_object(value, {"schema_version", "plan_id", "request", "basis", "evaluated_at", "entries", "coverage", "next_cursor"})
    require(value["schema_version"] == REFRESH_SCHEMA, "assessment_refresh_contract_unsupported")
    require(refresh_request(value["request"]) == value["request"], "assessment_refresh_request_not_canonical")
    digest(value["basis"])
    timestamp(value["evaluated_at"])
    seen = set()
    for entry in bounded_list(value["entries"], maximum=MAX_ASSESSMENTS):
        exact_object(entry, {"assessment_id", "question_slugs", "reasons", "reevaluation_id"})
        identifier = digest(entry["assessment_id"])
        require(identifier not in seen, "assessment_refresh_duplicate")
        seen.add(identifier)
        require(questions(entry["question_slugs"]) == entry["question_slugs"], "assessment_refresh_scope_not_canonical")
        reasons = [name(reason) for reason in bounded_list(entry["reasons"], maximum=256, minimum=1)]
        require(reasons == sorted(set(reasons)), "assessment_refresh_reasons_not_canonical")
        require(entry["reevaluation_id"] == content_id("evidence-reevaluation/v1", {"assessment_id": identifier}), "assessment_reevaluation_identity_mismatch")
    coverage = exact_object(value["coverage"], {"total", "scanned", "limit", "complete", "truncated", "previous_pages_not_included", "changed_sources_are_hints"})
    require(type(coverage["total"]) is int and 0 <= coverage["total"] <= 4096
            and type(coverage["scanned"]) is int and len(seen) <= coverage["scanned"] <= min(coverage["total"], value["request"]["limit"])
            and type(coverage["limit"]) is int and coverage["limit"] == value["request"]["limit"], "assessment_refresh_coverage_invalid")
    require(all(type(coverage[key]) is bool for key in ("complete", "truncated", "previous_pages_not_included", "changed_sources_are_hints"))
            and coverage["previous_pages_not_included"] == (value["request"]["cursor"] is not None)
            and coverage["complete"] == (not coverage["previous_pages_not_included"] and not coverage["truncated"])
            and coverage["changed_sources_are_hints"] is True, "assessment_refresh_coverage_invalid")
    if value["next_cursor"] is not None:
        cursor = exact_object(value["next_cursor"], {"basis", "after"})
        require(cursor["basis"] == value["basis"], "assessment_refresh_cursor_invalid")
        digest(cursor["after"])
    require(coverage["truncated"] == (value["next_cursor"] is not None), "assessment_refresh_cursor_invalid")
    require(value["plan_id"] == content_id(REFRESH_SCHEMA, {key: item for key, item in value.items() if key != "plan_id"}), "assessment_refresh_digest_mismatch")
    return value
