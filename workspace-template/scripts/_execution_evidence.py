#!/usr/bin/env python3
"""Validate inert execution records separately from authenticated evaluation claims.

An observation, proposed hypothesis, and evaluator receipt retain separate content
identities. A failed run remains usable evidence; only the independent-pass policy
requires a passing, scope-complete receipt from a separately controlled principal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _evidence_authority import (
    EvidenceInvalid,
    bounded_list,
    digest,
    exact_object,
    load_trust,
    name,
    timestamp,
    verify_attestation,
)
from _evidence_revision import content_id
from _normalized_contract import safe_source_id
from _packet_vendor_services_context_packet import ContextPacketError, validate_context_packet
from _record_artifacts import (
    MAX_BYTES,
    MAX_ENTRIES,
    MAX_FILES,
    artifact_path,
    capture_artifacts,
    closure_identity,
    file_binding,
    json_document,
    validate_file_bounds,
    validate_members,
)
from _workspace_module_loader import load_workspace_module

PROFILE = "execution_evidence/v1"
SCHEMA = "execution-evidence/v1"
RECORD_FILE = "execution-record.json"
OUTCOMES = frozenset({"passed", "failed", "skipped", "inconclusive"})


def profile_description() -> dict[str, Any]:
    return {"name": PROFILE, "record_schema": SCHEMA, "network_io_executed": False, "producer_execution": False,
            "evaluator_authentication": "external-host-policy/hmac-sha256",
            "authority": "evaluated_separately_from_structure", "max_files": MAX_FILES,
            "max_entries": MAX_ENTRIES, "max_bytes": MAX_BYTES}


def profile_for(record: dict[str, Any], normalized: dict[str, Any] | None = None) -> str | None:
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise EvidenceInvalid("invalid_source_metadata")
    declarations = []
    if record.get("kind") == "execution_evidence":
        declarations.append(PROFILE)
    if "execution_profile" in metadata:
        declarations.append(metadata["execution_profile"])
    if normalized is not None and normalized.get("execution_evidence") is not None:
        report = normalized["execution_evidence"]
        declarations.append(report.get("profile") if isinstance(report, dict) else None)
    if not declarations:
        return None
    if any(value != PROFILE for value in declarations):
        raise EvidenceInvalid("unsupported_or_conflicting_execution_profile")
    return PROFILE


def text_list(value: Any) -> list[str]:
    values = bounded_list(value, maximum=128)
    if any(not isinstance(item, str) or len(item) > 4096 for item in values):
        raise EvidenceInvalid("invalid_execution_text")
    return values


def bound_artifact(value: Any, members: dict[str, dict[str, Any]], roles: set[str]) -> str:
    exact_object(value, {"path", "content_hash"})
    path = artifact_path(value["path"])
    digest(value["content_hash"])
    if path not in members or members[path]["content_hash"] != value["content_hash"] or members[path]["role"] not in roles:
        raise EvidenceInvalid("execution_artifact_binding_mismatch")
    return path


def optional_artifact(value: Any, members: dict[str, dict[str, Any]], roles: set[str]) -> str | None:
    if isinstance(value, str) and value in {"not-applicable", "unknown"}:
        return None
    return bound_artifact(value, members, roles)


def validate_generation(payload: Any, members: dict[str, dict[str, Any]], files: dict[str, bytes]) -> dict[str, Any]:
    exact_object(payload, {
        "record_type", "episode_id", "task_id", "run_id", "problem_id", "input_group_id", "held_out_role",
        "generating_agent", "predecessor", "hypothesis", "inputs", "patch", "workspace",
        "environment", "dependencies", "container", "tool", "model", "parameters", "seed",
        "started_at", "finished_at", "outputs", "logs", "outcome", "units", "warnings", "limits",
        "verification_scope", "temporal",
    })
    if payload["record_type"] not in ("observation", "hypothesis"):
        raise EvidenceInvalid("unsupported_execution_record_type")
    for field in ("episode_id", "task_id", "run_id", "problem_id", "input_group_id", "generating_agent"):
        name(payload[field])
    if payload["held_out_role"] not in ("training", "validation", "held-out", "unspecified"):
        raise EvidenceInvalid("invalid_held_out_role")
    for field in ("predecessor", "hypothesis"):
        if payload[field] is not None:
            digest(payload[field])
    referenced: set[str] = set()
    inputs = bounded_list(payload["inputs"], minimum=1)
    source_revisions = set()
    for item in inputs:
        exact_object(item, {"source_id", "revision_id", "artifact"})
        name(item["source_id"])
        digest(item["revision_id"])
        identity = (item["source_id"], item["revision_id"])
        if identity in source_revisions:
            raise EvidenceInvalid("duplicate_input_revision")
        source_revisions.add(identity)
        referenced.add(bound_artifact(item["artifact"], members, {"input", "context", "context-packet"}))
    workspace = exact_object(payload["workspace"], {"scope", "content_hash"})
    if workspace["scope"] != "declared-input-artifacts" or workspace["content_hash"] != closure_identity({path: files[path] for path in referenced}):
        raise EvidenceInvalid("workspace_input_binding_mismatch")
    for field, role in (("patch", "patch"), ("environment", "environment"), ("dependencies", "dependency"), ("container", "environment")):
        path = optional_artifact(payload[field], members, {role})
        if path is not None:
            referenced.add(path)
    tool = exact_object(payload["tool"], {"name", "version", "schema_version", "artifact"})
    for field in ("name", "version", "schema_version"):
        name(tool[field])
    referenced.add(bound_artifact(tool["artifact"], members, {"tool"}))
    model = exact_object(payload["model"], {"identity", "weights", "adapter", "quantization", "prompt", "context"})
    for field in ("identity", "weights", "adapter", "quantization"):
        name(model[field])
    for field in ("prompt", "context"):
        path = optional_artifact(model[field], members, {"context", "context-packet", "input"})
        if path is not None:
            referenced.add(path)
    if not isinstance(payload["parameters"], dict) or len(payload["parameters"]) > 128:
        raise EvidenceInvalid("invalid_execution_parameters")
    if not (type(payload["seed"]) is int or payload["seed"] in ("unknown", "not-applicable")):
        raise EvidenceInvalid("invalid_execution_seed")
    if timestamp(payload["finished_at"]) < timestamp(payload["started_at"]):
        raise EvidenceInvalid("invalid_execution_interval")
    for field, role in (("outputs", "output"), ("logs", "log")):
        paths = [bound_artifact(item, members, {role}) for item in bounded_list(payload[field], minimum=1)]
        if len(paths) != len(set(paths)):
            raise EvidenceInvalid("duplicate_execution_artifact")
        referenced.update(paths)
    if payload["outcome"] not in OUTCOMES:
        raise EvidenceInvalid("invalid_execution_outcome")
    if payload["record_type"] == "hypothesis" and payload["outcome"] != "inconclusive":
        raise EvidenceInvalid("hypothesis_cannot_assert_outcome")
    if not isinstance(payload["units"], dict) or len(payload["units"]) > 128 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in payload["units"].items()):
        raise EvidenceInvalid("invalid_execution_units")
    text_list(payload["warnings"])
    text_list(payload["limits"])
    scope = exact_object(payload["verification_scope"], {"suite_revision", "suite", "checks", "environment"})
    name(scope["suite_revision"])
    referenced.add(bound_artifact(scope["suite"], members, {"suite"}))
    checks = bounded_list(scope["checks"], minimum=1)
    if any(not isinstance(check, str) or not name(check) for check in checks) or len(checks) != len(set(checks)):
        raise EvidenceInvalid("invalid_verification_scope")
    if scope["environment"] != payload["environment"]:
        raise EvidenceInvalid("verification_environment_mismatch")
    temporal = exact_object(payload["temporal"], {"mode", "cutoff", "limitations"})
    if temporal["mode"] not in ("current", "historical-audit", "historical-available"):
        raise EvidenceInvalid("invalid_temporal_mode")
    if temporal["cutoff"] is not None:
        timestamp(temporal["cutoff"])
    if (temporal["mode"] == "current") != (temporal["cutoff"] is None):
        raise EvidenceInvalid("temporal_cutoff_required")
    text_list(temporal["limitations"])
    qualifications = {}
    for path in sorted(referenced):
        if members[path]["role"] in {"context", "context-packet"}:
            packet_required = members[path]["role"] == "context-packet"
            document = json_document(files[path]) if packet_required or path.endswith(".json") else {}
            if packet_required or str(document.get("schema_version", "")).startswith("llm-wiki-qualified-context-packet/"):
                try:
                    validated = validate_context_packet(files[path])
                except (ContextPacketError, ValueError, TypeError) as exc:
                    raise EvidenceInvalid("execution_context_packet_invalid") from exc
                qualified = validated.packet.to_payload()
                omitted = []
                if "content" in qualified["response"]:
                    del qualified["response"]["content"]
                    omitted.append("/response/content")
                qualifications[path] = {"packet_id": validated.packet_id, "native_validation": validated.to_payload(),
                                        "qualifications": qualified, "omitted_fields": omitted,
                                        "original": file_binding(files[path]), "authority": "not_established"}
    return {"record_id": content_id("evidence-execution-record/v1", payload), "record_type": payload["record_type"],
            "episode_id": payload["episode_id"], "task_id": payload["task_id"], "run_id": payload["run_id"],
            "problem_id": payload["problem_id"], "input_group_id": payload["input_group_id"], "held_out_role": payload["held_out_role"],
            "outcome": payload["outcome"], "referenced_artifacts": sorted(referenced), "qualified_context": qualifications,
            "temporal": temporal, "payload": payload}


def validate_receipt(payload: Any, records: dict[str, dict[str, Any]], members: dict[str, dict[str, Any]]) -> dict[str, Any]:
    exact_object(payload, {"record_type", "target_record_id", "episode_id", "task_id", "run_id", "evaluator",
                           "scope", "started_at", "finished_at", "outcome", "assertions", "counts", "logs", "warnings", "limits"})
    if payload["record_type"] != "verification-receipt":
        raise EvidenceInvalid("unsupported_execution_receipt_type")
    target = records.get(digest(payload["target_record_id"]))
    if target is None or target["record_type"] != "observation":
        raise EvidenceInvalid("receipt_target_missing_or_not_observation")
    original = target["payload"]
    if any(payload[key] != original[key] for key in ("episode_id", "task_id", "run_id")):
        raise EvidenceInvalid("receipt_target_binding_mismatch")
    name(payload["evaluator"])
    if payload["scope"] != original["verification_scope"]:
        raise EvidenceInvalid("receipt_scope_binding_mismatch")
    if not timestamp(original["finished_at"]) <= timestamp(payload["started_at"]) <= timestamp(payload["finished_at"]):
        raise EvidenceInvalid("invalid_receipt_interval")
    if payload["outcome"] not in OUTCOMES:
        raise EvidenceInvalid("invalid_receipt_outcome")
    expected_checks = set(payload["scope"]["checks"])
    seen = set()
    counts = dict.fromkeys(sorted(OUTCOMES), 0)
    for assertion in bounded_list(payload["assertions"]):
        exact_object(assertion, {"check_id", "outcome", "expected", "actual", "comparison"})
        check = name(assertion["check_id"])
        if check in seen or check not in expected_checks or assertion["outcome"] not in OUTCOMES:
            raise EvidenceInvalid("invalid_receipt_assertion")
        # Comparisons are evaluator claims, authenticated with the whole receipt.
        # Core does not execute arbitrary comparison or runner names.
        name(assertion["comparison"])
        seen.add(check)
        counts[assertion["outcome"]] += 1
    if (not isinstance(payload["counts"], dict) or any(type(value) is not int for value in payload["counts"].values())
            or payload["counts"] != counts):
        raise EvidenceInvalid("receipt_assertion_count_mismatch")
    complete = seen == expected_checks
    if payload["outcome"] == "passed" and (not complete or counts["passed"] != len(expected_checks)):
        raise EvidenceInvalid("passing_receipt_scope_incomplete")
    if payload["outcome"] == "failed" and not counts["failed"]:
        raise EvidenceInvalid("failed_receipt_has_no_failed_assertion")
    logs = [bound_artifact(item, members, {"log"}) for item in bounded_list(payload["logs"], minimum=1)]
    if len(logs) != len(set(logs)):
        raise EvidenceInvalid("duplicate_receipt_log")
    text_list(payload["warnings"])
    text_list(payload["limits"])
    return {"receipt_id": content_id("evidence-verification-receipt/v1", payload), "target_record_id": target["record_id"],
            "outcome": payload["outcome"], "counts": counts, "scope_complete": complete, "referenced_artifacts": logs,
            "payload": payload}


def validate_closure(source_id: str, files: dict[str, bytes]) -> dict[str, Any]:
    validate_file_bounds(files)
    if RECORD_FILE not in files:
        raise EvidenceInvalid("execution_record_missing")
    document = exact_object(json_document(files[RECORD_FILE]), {"schema_version", "profile", "source_id", "selected_record_id", "selected_receipt_id", "artifacts", "records", "receipts"})
    if document["schema_version"] != SCHEMA or document["profile"] != PROFILE:
        raise EvidenceInvalid("execution_schema_unsupported")
    if document["source_id"] != source_id:
        raise EvidenceInvalid("execution_source_binding_mismatch")
    members = validate_members(document["artifacts"], files, RECORD_FILE)
    records = {}
    used = set()
    for envelope in bounded_list(document["records"], maximum=128, minimum=1):
        exact_object(envelope, {"payload"}, {"authentication"})
        record = validate_generation(envelope["payload"], members, files)
        if record["record_id"] in records:
            raise EvidenceInvalid("duplicate_execution_record")
        for key, expected_type in (("predecessor", "observation"), ("hypothesis", "hypothesis")):
            link = record["payload"][key]
            if link is not None and (link not in records or records[link]["record_type"] != expected_type
                                     or records[link]["episode_id"] != record["episode_id"]
                                     or records[link]["task_id"] != record["task_id"]
                                     or timestamp(records[link]["payload"]["finished_at"]) > timestamp(record["payload"]["started_at"])):
                raise EvidenceInvalid("execution_history_link_invalid")
        hypothesis = record["payload"]["hypothesis"]
        if hypothesis is not None and records[hypothesis]["payload"]["predecessor"] != record["payload"]["predecessor"]:
            raise EvidenceInvalid("execution_hypothesis_predecessor_mismatch")
        records[record["record_id"]] = record
        used.update(record["referenced_artifacts"])
    selected = records.get(digest(document["selected_record_id"]))
    if selected is None or selected["record_type"] != "observation":
        raise EvidenceInvalid("selected_execution_record_missing")
    receipts = []
    receipt_ids = set()
    for envelope in bounded_list(document["receipts"], maximum=128):
        exact_object(envelope, {"payload"}, {"authentication"})
        receipt = validate_receipt(envelope["payload"], records, members)
        if receipt["receipt_id"] in receipt_ids:
            raise EvidenceInvalid("duplicate_verification_receipt")
        receipt_ids.add(receipt["receipt_id"])
        receipts.append(receipt)
        used.update(receipt["referenced_artifacts"])
    selected_receipt = document["selected_receipt_id"]
    if selected_receipt is not None:
        digest(selected_receipt)
        if not any(receipt["receipt_id"] == selected_receipt and receipt["target_record_id"] == selected["record_id"] for receipt in receipts):
            raise EvidenceInvalid("selected_verification_receipt_missing")
    if used != set(members):
        raise EvidenceInvalid("execution_unreferenced_artifacts")
    return {"schema_version": "execution-evidence-validation/v1", "profile": PROFILE, "source_id": source_id,
            "valid": True, "reason": "execution_structure_valid", "closure_id": closure_identity(files),
            "originals": {path: file_binding(data) for path, data in sorted(files.items())},
            "records": list(records.values()), "receipts": receipts, "selected_record_id": selected["record_id"],
            "selected_receipt_id": selected_receipt, "authority": "not_evaluated",
            "document": document}


def inspect_execution(root: Path, config: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    usage = load_workspace_module(Path(__file__).resolve().parent, "_usage_gate")

    try:
        if profile_for(record) is None:
            raise EvidenceInvalid("execution_profile_not_selected")
        source_id = safe_source_id(record["id"])
        files = usage.original_artifacts(root, config, record)
        if files is None:
            files = capture_artifacts(root, f"sources/evidence/{source_id}")
        return validate_closure(record["id"], files)
    except EvidenceInvalid as exc:
        reason = str(exc)
    except (KeyError, ValueError, TypeError, AttributeError, OSError, RecursionError):
        reason = "invalid_execution_evidence"
    return {"schema_version": "execution-evidence-validation/v1", "profile": PROFILE, "source_id": record.get("id"),
            "valid": False, "reason": reason, "authority": "not_evaluated"}


def assess_verification(report: dict[str, Any], root: Path, config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Evaluate one live clock and external authority; never persist it as structural truth."""
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"eligible": False, "reason": "execution_structure_invalid", "evaluated_at": now.isoformat(), "receipts": []}
    if not report.get("valid"):
        return result
    try:
        trust = load_trust(root, config, now)
    except (EvidenceInvalid, TypeError, ValueError, KeyError):
        trust = None
    document = report["document"]
    generators = {}
    for envelope, record in zip(document["records"], report["records"], strict=True):
        authentication = verify_attestation(envelope, "generator", trust, now)
        if authentication.get("principal") != record["payload"]["generating_agent"]:
            authentication = {"authenticated": False, "reason": "generator_principal_mismatch"}
        elif authentication["authenticated"] and timestamp(authentication["issued_at"]) < timestamp(record["payload"]["finished_at"]):
            authentication = {"authenticated": False, "reason": "generator_receipt_predates_completion"}
        generators[record["record_id"]] = authentication
    for envelope, receipt in zip(document["receipts"], report["receipts"], strict=True):
        auth = verify_attestation(envelope, "evaluator", trust, now)
        generator = generators[receipt["target_record_id"]]
        reason = "independent_evaluation_authenticated"
        if not auth["authenticated"]:
            reason = auth["reason"]
        elif auth["principal"] != receipt["payload"]["evaluator"]:
            reason = "evaluator_principal_mismatch"
        elif not generator["authenticated"]:
            reason = "generator_authentication_required"
        elif auth["controller"] == generator["controller"]:
            reason = "self_certification_not_independent"
        elif timestamp(auth["issued_at"]) < timestamp(receipt["payload"]["finished_at"]):
            reason = "evaluator_receipt_predates_completion"
        independent = reason == "independent_evaluation_authenticated"
        target = next(record for record in report["records"] if record["record_id"] == receipt["target_record_id"])
        eligible = independent and receipt["outcome"] == "passed" and receipt["scope_complete"] and target["outcome"] == "passed"
        result["receipts"].append({"receipt_id": receipt["receipt_id"], "target_record_id": receipt["target_record_id"],
                                   "outcome": receipt["outcome"], "independent": independent, "positive_eligible": eligible,
                                   "reason": reason, "authentication": auth, "generator_authentication": generator})
    result["eligible"] = any(receipt["positive_eligible"] and receipt["receipt_id"] == report["selected_receipt_id"]
                             for receipt in result["receipts"])
    result["reason"] = "independent_pass" if result["eligible"] else ("no_independent_pass" if result["receipts"] else "verification_receipt_missing")
    return result


def normalized_issues(root: Path | None, config: dict[str, Any], record: dict[str, Any], normalized: dict[str, Any]) -> list[str]:
    try:
        if profile_for(record, normalized) is None:
            return []
    except EvidenceInvalid as exc:
        return [str(exc)]
    if root is None:
        return ["execution_original_context_required"]
    report = inspect_execution(root, config, record)
    if report != normalized.get("execution_evidence"):
        return ["execution_normalized_binding_mismatch"]
    return [] if report["valid"] else [report["reason"]]
