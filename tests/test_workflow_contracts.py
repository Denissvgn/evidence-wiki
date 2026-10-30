"""Serialization, finite leases and inert workflow proposal boundaries."""

from __future__ import annotations

import copy
import json
import math

import pytest

from evidence_wiki.errors import UsageError
from evidence_wiki.workflow_contracts import (
    DECISION,
    DECISION_OUTCOMES,
    JOB,
    JOB_V2,
    JOB_V3,
    JOB_V4,
    JOB_V5,
    JOB_V6,
    MAX_BYTES,
    MAX_DEPTH,
    MAX_NODES,
    RESULT,
    WorkflowContractError,
    decode_document,
    document_sha256,
    encode_document,
    schema_catalog,
    schema_document,
    schemas,
    validate_decision,
    validate_document,
    validate_job,
    validate_result,
)

ISSUED = "2026-01-01T00:00:00Z"
NOW = "2026-01-01T00:00:05Z"
DEADLINE = "2026-01-01T00:01:00Z"
ORIGINAL = "  Compare α and β.\r\nKeep \"quoted\" text and trailing spaces.  "


def qualifications():
    return {
        "assurance": "artifact_checked", "status": "declared", "evaluator_ref": "evaluator-declaration",
        "qualification_ref": "qualification-declaration", "context_basis": "context-declaration",
    }


def job(operation="generate"):
    payloads = {
        "generate": {"question": ORIGINAL},
        "acquire": {"request_id": "request-1", "source_ref": "source-1"},
        "evaluate": {"claim_id": "claim-1", "packet_id": "packet-1"},
        "probe": {"capability": "offline-check"},
        "poll": {"job_id": "job-generate"}, "cancel": {"job_id": "job-generate"},
        "schedule": {"work_id": "work-1"},
        "deliver": {"artifact_id": "artifact-1", "destination_ref": "configured-destination"},
    }
    return {
        "schema_version": JOB, "id": f"job-{operation}", "operation": operation,
        "role": "evaluator" if operation == "evaluate" else "host_worker",
        "controller_ref": "registered-controller", "grant_ref": "registered-grant",
        "input_id": "immutable-input", "basis_id": "immutable-basis", "policy_id": "policy-identity",
        "policy_version": "policy/v1", "prompt_version": "prompt/v1", "revision_precondition": "revision-1",
        "original_text": ORIGINAL if operation == "generate" else None,
        "scope": ["question-1"], "required_capabilities": ["offline-check"], "payload": payloads[operation],
        "issued_utc": ISSUED, "deadline_utc": DEADLINE, "timeout_seconds": 30, "max_attempts": 2,
        "expected_result_schema": RESULT,
        "budgets": {"model_calls": 0, "tool_calls": 5, "tokens": 0, "cost": "0"},
        "qualifications": qualifications(),
    }


def result(basis=None, outcome="succeeded"):
    basis = basis or job()
    return {
        "schema_version": RESULT, "job_id": basis["id"], "job_sha256": document_sha256(JOB, basis),
        **{field: basis[field] for field in ("operation", "role", "input_id", "basis_id", "policy_id", "controller_ref", "grant_ref", "deadline_utc")},
        "controller_receipt_ref": "receipt-declaration", "outcome": outcome,
        "observed_usage": {"seconds": 1, "model_calls": 0, "tool_calls": 2, "tokens": 0, "cost": "0"},
        "artifact_ref": None if outcome in {"pending", "unsupported", "unavailable"} else "artifact-declaration",
        "qualifications": qualifications(),
    }


def decision(outcome="accepted"):
    return {
        "schema_version": DECISION, "id": "decision-1", "job_id": "job-generate",
        "input_id": "immutable-input", "basis_id": "immutable-basis", "policy_id": "policy-identity",
        "policy_version": "policy/v1", "prompt_version": "prompt/v1", "role": "host_worker",
        "controller_ref": "registered-controller", "revision_precondition": "revision-1", "original_text": ORIGINAL,
        "outcome": outcome, "reason": "proposed_conclusion", "rationale": "See the declared source reference.",
        "source_refs": ["source-declaration"],
        "accounting": {"original_count": 2, "proposed_count": 1, "unresolved_count": 1},
        "uncertainty": {
            "status": "unknown", "description": "Support has not been independently accepted.", "confidence": "0.8",
            "interpretation": "agent_self_assessment_not_probability",
        },
        "retry_budget": {"max_attempts": 2, "attempts_used": 1, "attempts_remaining": 1},
        "validation_provenance": [{"owner": "quote-owner", "check": "grounding", "reference": "receipt-declaration", "basis_id": "immutable-basis", "verdict": "unknown"}],
        "qualifications": qualifications(), "authorized": False, "evidence_accepted": False,
    }


@pytest.mark.parametrize("name,value", [(JOB, job()), (RESULT, result()), (DECISION, decision())])
def test_exact_roundtrip_and_detachment(name, value):
    raw = encode_document(name, value)
    decoded = decode_document(name, raw)
    assert decoded == value
    assert encode_document(name, decoded) == raw
    assert document_sha256(name, value) == document_sha256(name, decoded)
    decoded["qualifications"]["context_basis"] = "mutated"
    assert value["qualifications"]["context_basis"] == "context-declaration"
    if "original_text" in value:
        assert decode_document(name, raw)["original_text"] == ORIGINAL


def test_schema_catalog_is_detached_and_does_not_grant_authority():
    assert set(schemas()) == {JOB, JOB_V2, JOB_V3, JOB_V4, JOB_V5, JOB_V6, RESULT, DECISION}
    first = schema_document(RESULT)
    first["properties"]["schema_version"]["const"] = "invented"
    assert schema_document(RESULT)["properties"]["schema_version"]["const"] == RESULT
    catalog = schema_catalog()
    assert catalog["authority_granted"] is False
    assert catalog["evidence_accepted"] is False
    assert catalog["limits"] == {"bytes": MAX_BYTES, "depth": MAX_DEPTH, "nodes": MAX_NODES, "lease_seconds": 600}


@pytest.mark.parametrize("operation", ["generate", "acquire", "evaluate", "probe", "schedule", "deliver"])
def test_closed_job_variants_and_role_separation(operation):
    value = job(operation)
    assert validate_job(value, now_utc=NOW) == value
    value["role"] = "host_worker" if operation == "evaluate" else "evaluator"
    with pytest.raises(WorkflowContractError):
        validate_job(value, now_utc=NOW)


@pytest.mark.parametrize("name,make", [(JOB, job), (RESULT, result), (DECISION, decision)])
def test_unknown_version_fields_and_nested_fields_refuse(name, make):
    for location in (None, "qualifications"):
        value = make()
        target = value if location is None else value[location]
        target["hidden_authority"] = True
        with pytest.raises(WorkflowContractError) as exc:
            validate_document(name, value)
        assert "hidden_authority" not in str(exc.value.details)
    value = make()
    value["schema_version"] = "invented/v9"
    with pytest.raises(WorkflowContractError, match="Workflow contract") as exc:
        validate_document(name, value)
    assert isinstance(exc.value, UsageError)
    assert exc.value.error_code == "WORKFLOW_VERSION_UNSUPPORTED"
    with pytest.raises(WorkflowContractError):
        schema_document("invented/v9")


@pytest.mark.parametrize("raw", [
    b'{"schema_version":"research-host-job/v1","schema_version":"research-host-job/v1"}',
    b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
    b'{"x":1e999}', b'"\xff"', b'{"x":"\\ud800"}', b'{}{}', b'[]', b'null',
])
def test_hostile_json_refuses_with_public_typed_error(raw):
    with pytest.raises(WorkflowContractError):
        decode_document(JOB, raw)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, (1,), {1: "nonstring-key"}, object()])
def test_non_json_python_values_refuse(value):
    candidate = job()
    candidate["payload"]["injected"] = value
    with pytest.raises(WorkflowContractError):
        encode_document(JOB, candidate)


def test_cycles_and_container_subclasses_refuse_before_encoding():
    cyclic = job()
    cyclic["payload"]["cycle"] = cyclic
    with pytest.raises(WorkflowContractError):
        encode_document(JOB, cyclic)
    loop = []
    loop.append(loop)
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, loop)

    class UntrustedDict(dict):
        def items(self):
            raise AssertionError("Untrusted container methods must not execute")

    with pytest.raises(WorkflowContractError):
        validate_document(JOB, UntrustedDict(job()))


def test_bounded_tree_json_and_encoded_bytes():
    for raw in (b" " * (MAX_BYTES + 1), b"[" * (MAX_DEPTH + 1) + b"]" * (MAX_DEPTH + 1)):
        with pytest.raises(WorkflowContractError) as exc:
            decode_document(JOB, raw)
        assert exc.value.error_code == "WORKFLOW_LIMIT"
    for candidate in ([0] * MAX_NODES, {"x": "a" * (MAX_BYTES + 1)}, {"x": 1 << 256}):
        with pytest.raises(WorkflowContractError) as exc:
            validate_document(JOB, candidate)
        assert exc.value.error_code == "WORKFLOW_LIMIT"
    nested = 0
    for _ in range(MAX_DEPTH):
        nested = [nested]
    with pytest.raises(WorkflowContractError) as exc:
        validate_document(JOB, nested)
    assert exc.value.error_code == "WORKFLOW_LIMIT"
    oversized = decision()
    # Quoted reference text fits the schema and tree byte limits, but escaping
    # both bounded reference collections exceeds the serialized byte ceiling.
    oversized["source_refs"] = ['"' * 4090 + str(i) for i in range(64)]
    oversized["validation_provenance"] = [
        {"owner": "a", "check": "b", "reference": '"' * 4096, "basis_id": "c", "verdict": "pass"} for _ in range(64)
    ]
    with pytest.raises(WorkflowContractError) as exc:
        encode_document(DECISION, oversized)
    assert exc.value.error_code == "WORKFLOW_LIMIT"


def test_raw_byte_ceiling_exact_original_boundary_and_zero_budget():
    candidate = job()
    candidate["original_text"] = candidate["payload"]["question"] = "α" * 65_536
    candidate["budgets"] = {"model_calls": 0, "tool_calls": 0, "tokens": 0, "cost": "0"}
    raw = encode_document(JOB, candidate)
    padded = raw + b" " * (MAX_BYTES - len(raw))
    assert len(padded) == MAX_BYTES
    assert decode_document(JOB, padded) == candidate
    with pytest.raises(WorkflowContractError) as exc:
        decode_document(JOB, padded + b" ")
    assert exc.value.error_code == "WORKFLOW_LIMIT"
    candidate["original_text"] += "x"
    candidate["payload"]["question"] += "x"
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, candidate)


def test_shared_noncyclic_values_and_literal_brackets_roundtrip():
    value = job()
    value["payload"]["question"] = value["original_text"] = " [ { \" \\\\ } ] "
    shared = "shared"
    value["qualifications"]["evaluator_ref"] = shared
    value["qualifications"]["qualification_ref"] = shared
    assert decode_document(JOB, encode_document(JOB, value)) == value


@pytest.mark.parametrize("timestamp", [
    "2026-01-01", "2026-01-01T00:01:00", "2026-01-01T01:01:00+01:00", "2026-01-01T00:01:00-00:00",
    "2026-02-30T00:01:00Z", "2026-01-01T00:01:60Z", "2026-01-01T00:01:00Z\n", "2026-01-01T00:01:00.1234567Z",
])
def test_strict_utc_calendar_deadlines(timestamp):
    candidate = job()
    candidate["deadline_utc"] = timestamp
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, candidate)


def test_lease_boundaries_clock_and_remaining_timeout():
    candidate = job()
    candidate["deadline_utc"] = "2026-01-01T00:10:00+00:00"
    candidate["timeout_seconds"] = 600
    assert validate_job(candidate, now_utc=ISSUED) == candidate
    assert validate_job(candidate, now_utc="2026-01-01T00:09:59.999999Z") == candidate
    with pytest.raises(WorkflowContractError) as exc:
        validate_job(candidate, now_utc=candidate["deadline_utc"])
    assert exc.value.error_code == "WORKFLOW_DEADLINE_EXPIRED"
    for deadline in (ISSUED, "2025-12-31T23:59:59Z", "2026-01-01T00:10:00.000001Z"):
        candidate["deadline_utc"] = deadline
        with pytest.raises(WorkflowContractError):
            validate_document(JOB, candidate)
    with pytest.raises(WorkflowContractError):
        validate_job(job(), now_utc="2025-12-31T23:59:59Z")
    short = job()
    short["timeout_seconds"] = 61
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, short)


@pytest.mark.parametrize("field,bad", [
    ("cost", "NaN"), ("cost", "Infinity"), ("cost", "-1"), ("cost", "1e999"), ("cost", " 0"),
    ("cost", "0\n"), ("cost", "9999999999"), ("model_calls", True), ("tokens", -1),
    ("tool_calls", 101), ("tokens", 1.5), ("cost", 0),
])
def test_finite_closed_budgets(field, bad):
    candidate = job()
    candidate["budgets"][field] = bad
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, candidate)


@pytest.mark.parametrize("operation", ["poll", "cancel"])
def test_followup_requires_stored_parent_and_preserves_basis(operation):
    parent = job()
    followup = job(operation)
    assert followup["original_text"] is None
    assert validate_job(followup, now_utc=NOW, parent_job=parent) == followup
    with pytest.raises(WorkflowContractError) as exc:
        validate_job(followup, now_utc=NOW)
    assert exc.value.error_code == "WORKFLOW_CONTEXT_REQUIRED"
    evaluator = job("evaluate")
    followup["role"] = "evaluator"
    followup["payload"]["job_id"] = evaluator["id"]
    assert validate_job(followup, now_utc=NOW, parent_job=evaluator) == followup
    followup["role"] = "host_worker"
    with pytest.raises(WorkflowContractError):
        validate_job(followup, now_utc=NOW, parent_job=evaluator)


@pytest.mark.parametrize("field,replacement", [
    ("input_id", "fresh-input"), ("basis_id", "fresh-basis"), ("controller_ref", "other-controller"),
    ("grant_ref", "other-grant"), ("policy_id", "other-policy"), ("policy_version", "policy/v2"),
    ("prompt_version", "prompt/v2"), ("revision_precondition", "revision-2"), ("scope", ["question-2"]),
    ("required_capabilities", []), ("issued_utc", "2026-01-01T00:00:01Z"),
    ("deadline_utc", "2026-01-01T00:01:01Z"), ("max_attempts", 3), ("timeout_seconds", 31),
    ("budgets", {"model_calls": 1, "tool_calls": 5, "tokens": 0, "cost": "0"}),
])
def test_followup_cannot_reset_identity_lease_scope_or_budget(field, replacement):
    followup = job("poll")
    followup[field] = replacement
    with pytest.raises(WorkflowContractError) as exc:
        validate_job(followup, now_utc=NOW, parent_job=job())
    assert exc.value.error_code == "WORKFLOW_CORRELATION_MISMATCH"


def test_followup_rejects_parent_chains_self_reference_and_original_leak():
    followup = job("poll")
    for parent in (job("cancel"), {**job(), "id": followup["id"]}):
        with pytest.raises(WorkflowContractError):
            validate_job(followup, now_utc=NOW, parent_job=parent)
    followup["original_text"] = ORIGINAL
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, followup)


@pytest.mark.parametrize("outcome", ["pending", "incomplete", "unsupported", "succeeded", "failed", "cancelled", "unavailable"])
def test_result_outcomes_correlate_and_preserve_usage_without_acceptance(outcome):
    candidate = result(outcome=outcome)
    candidate["observed_usage"]["model_calls"] = 1
    validated = validate_result(candidate, job=job(), now_utc=NOW)
    assert validated["outcome"] == outcome
    assert validated["observed_usage"]["model_calls"] == 1
    assert "evidence_accepted" not in validated
    assert "authorized" not in validated


@pytest.mark.parametrize("outcome", ["pending", "failed", "unavailable", "succeeded"])
def test_unknown_measured_usage_roundtrips_without_zero_or_authority(outcome):
    candidate = result(outcome=outcome)
    candidate["observed_usage"] = {field: None for field in candidate["observed_usage"]}
    raw = encode_document(RESULT, candidate)
    decoded = decode_document(RESULT, raw)
    validated = validate_result(decoded, job=job(), now_utc=NOW)
    assert validated == candidate
    assert all(value is None for value in validated["observed_usage"].values())
    assert validated["qualifications"] == job()["qualifications"]
    assert "authorized" not in validated and "evidence_accepted" not in validated
    for field in ("authorized", "evidence_accepted"):
        changed = copy.deepcopy(candidate)
        changed[field] = True
        with pytest.raises(WorkflowContractError):
            validate_result(changed, job=job(), now_utc=NOW)
    for field in candidate["observed_usage"]:
        changed = job()
        if field in changed["budgets"]:
            changed["budgets"][field] = None
            with pytest.raises(WorkflowContractError):
                validate_document(JOB, changed)


def test_mixed_known_and_unknown_usage_preserves_fractional_elapsed_time():
    candidate = result(outcome="pending")
    candidate["observed_usage"].update(seconds=0.125, model_calls=0, tokens=None, cost=None)
    decoded = decode_document(RESULT, encode_document(RESULT, candidate))
    assert validate_result(decoded, job=job(), now_utc=NOW)["observed_usage"] == candidate["observed_usage"]
    assert decoded["observed_usage"]["seconds"] == 0.125
    assert type(decoded["observed_usage"]["seconds"]) is float
    seconds_schema = schema_document(RESULT)["properties"]["observed_usage"]["properties"]["seconds"]
    assert seconds_schema["anyOf"][0] == {"type": "number", "minimum": 0, "maximum": 600}


@pytest.mark.parametrize("seconds", [0.0, 0.000001, 599.999999, 600.0])
def test_fractional_elapsed_seconds_boundaries(seconds):
    candidate = result()
    candidate["observed_usage"]["seconds"] = seconds
    assert decode_document(RESULT, encode_document(RESULT, candidate))["observed_usage"]["seconds"] == seconds


@pytest.mark.parametrize("seconds", [True, -0.001, 600.000001, "0.125", math.inf, math.nan, [], {}])
def test_elapsed_seconds_refuse_wrong_types_nonfinite_and_out_of_bounds(seconds):
    candidate = result()
    candidate["observed_usage"]["seconds"] = seconds
    with pytest.raises(WorkflowContractError):
        validate_document(RESULT, candidate)


@pytest.mark.parametrize("field,replacement", [
    ("assurance", "host_enforced"), ("status", "unknown"), ("evaluator_ref", "substituted-evaluator"),
    ("qualification_ref", "substituted-qualification"), ("context_basis", "substituted-context"),
])
@pytest.mark.parametrize("outcome", ["succeeded", "pending", "failed"])
def test_result_cannot_substitute_job_qualification_context(field, replacement, outcome):
    basis = job()
    candidate = result(basis, outcome=outcome)
    candidate["observed_usage"]["tokens"] = None
    candidate["qualifications"][field] = replacement
    assert candidate["job_sha256"] == document_sha256(JOB, basis)
    # Shape alone is inert; the correlated intake must pin every declaration.
    assert validate_document(RESULT, candidate)["qualifications"][field] == replacement
    with pytest.raises(WorkflowContractError) as exc:
        validate_result(candidate, job=basis, now_utc=NOW)
    assert exc.value.error_code == "WORKFLOW_CORRELATION_MISMATCH"
    assert exc.value.details == {"field": "/qualifications"}


def test_parent_poll_result_preserves_unknown_usage_and_parent_qualifications():
    parent = job("evaluate")
    followup = job("poll")
    followup["role"] = parent["role"]
    followup["payload"]["job_id"] = parent["id"]
    candidate = result(followup, outcome="pending")
    candidate["observed_usage"] = {field: None for field in candidate["observed_usage"]}
    assert validate_result(candidate, job=followup, now_utc=NOW, parent_job=parent) == candidate
    candidate["qualifications"]["context_basis"] = None
    with pytest.raises(WorkflowContractError):
        validate_result(candidate, job=followup, now_utc=NOW, parent_job=parent)


@pytest.mark.parametrize("field", ["job_id", "job_sha256", "input_id", "basis_id", "policy_id", "controller_ref", "grant_ref"])
def test_result_identity_mismatch_refuses(field):
    candidate = result()
    candidate[field] = "a" * 64 if field == "job_sha256" else "other-identity"
    with pytest.raises(WorkflowContractError) as exc:
        validate_result(candidate, job=job(), now_utc=NOW)
    assert exc.value.error_code == "WORKFLOW_CORRELATION_MISMATCH"


def test_full_job_digest_binds_payload_original_versions_and_qualifications():
    candidate = result()
    for field, replacement in (
        ("prompt_version", "prompt/v2"), ("policy_version", "policy/v2"),
        ("original_text", "changed original"), ("qualifications", {**qualifications(), "status": "unknown"}),
    ):
        changed = job()
        changed[field] = replacement
        if field == "original_text":
            changed["payload"]["question"] = replacement
        with pytest.raises(WorkflowContractError):
            validate_result(candidate, job=changed, now_utc=NOW)
    with pytest.raises(WorkflowContractError):
        validate_result(candidate, job=job(), now_utc=DEADLINE)


def test_original_text_scope_and_result_artifact_invariants():
    candidate = job()
    candidate["original_text"] = ORIGINAL.strip()
    with pytest.raises(WorkflowContractError):
        validate_document(JOB, candidate)
    for field in ("scope", "required_capabilities"):
        candidate = job()
        candidate[field] *= 2
        with pytest.raises(WorkflowContractError):
            validate_document(JOB, candidate)
    for outcome, artifact in (("succeeded", None), ("pending", "pretend-artifact"), ("unavailable", "pretend-artifact")):
        candidate = result(outcome=outcome)
        candidate["artifact_ref"] = artifact
        with pytest.raises(WorkflowContractError):
            validate_document(RESULT, candidate)


@pytest.mark.parametrize("outcome", DECISION_OUTCOMES)
def test_decision_proposals_cannot_assert_authority_or_acceptance(outcome):
    candidate = decision(outcome)
    assert validate_decision(candidate)["authorized"] is False
    assert validate_decision(candidate)["evidence_accepted"] is False
    for field in ("authorized", "evidence_accepted"):
        changed = copy.deepcopy(candidate)
        changed[field] = True
        with pytest.raises(WorkflowContractError):
            validate_decision(changed)
    candidate["llm_success"] = True
    with pytest.raises(WorkflowContractError):
        validate_decision(candidate)


def test_decision_accounting_uncertainty_retry_and_provenance_are_explicit():
    candidate = decision()
    candidate["accounting"]["proposed_count"] = 2
    with pytest.raises(WorkflowContractError):
        validate_decision(candidate)
    candidate = decision()
    candidate["retry_budget"]["attempts_remaining"] = 2
    with pytest.raises(WorkflowContractError):
        validate_decision(candidate)
    candidate = decision()
    candidate["uncertainty"]["interpretation"] = "factual_probability"
    with pytest.raises(WorkflowContractError):
        validate_decision(candidate)
    candidate = decision()
    candidate["validation_provenance"][0]["private_reasoning"] = "unpublished reasoning"
    with pytest.raises(WorkflowContractError):
        validate_decision(candidate)
    candidate = decision()
    candidate["source_refs"] *= 2
    with pytest.raises(WorkflowContractError):
        validate_decision(candidate)


def test_errors_do_not_echo_unknown_key_or_payload_value():
    candidate = job()
    candidate["confidential-secret-key"] = "confidential-secret-value"
    with pytest.raises(WorkflowContractError) as exc:
        decode_document(JOB, json.dumps(candidate).encode())
    envelope = str(exc.value) + str(exc.value.details) + exc.value.remediation
    assert "confidential-secret" not in envelope
