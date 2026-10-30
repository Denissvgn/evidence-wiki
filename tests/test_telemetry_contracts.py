"""Closed measurement provenance, exact money and inert transport invariants."""

from __future__ import annotations

import copy

import pytest

from evidence_wiki.telemetry_contracts import (
    ACK,
    EVENT,
    JOURNAL,
    MAX_EVENT_BYTES,
    PROJECTION,
    RECEIPT,
    REPORT,
    VERIFICATION,
    decode_document,
    digest,
    encode_document,
    schema_document,
    schemas,
    validate_document,
)
from evidence_wiki.workflow_contracts import WorkflowContractError

HASH = "sha256:" + "a" * 64
CLOCK = "2026-10-04T00:00:00Z"


def metric_document(*, identifier="cost-one", name="cost", value="1.230000001", provenance="imported"):
    return {"logical_id": identifier, "name": name, "value": value,
        "unit": "currency" if name == "cost" else "seconds" if name.endswith("_seconds") else "count",
        "currency": "USD" if name == "cost" else None,
        "pricing_ref": {"id": "provider-price-v1", "as_of": CLOCK} if name == "cost" else None,
        "provenance": provenance, "source_ref": "provider-receipt",
        "trust_realm": {"imported": "caller_import", "estimated": "caller_estimate",
                        "measured": "local_artifact", "unavailable": "unavailable"}[provenance]}


def event_document(*, identifier="observation-one"):
    return {"schema_version": EVENT, "logical_id": identifier, "run_id": "trace", "agent_id": "worker",
        "request_id": None, "job_id": None, "job_sha256": None, "attempt_id": "attempt-one",
        "kind": "measurement", "occurred_at": CLOCK, "role": "host_worker", "controller_ref": "controller-one",
        "policy_id": HASH, "basis_id": HASH, "input_refs": [HASH], "output_refs": [],
        "model": {"id": "configured-model", "version": "1"}, "prompt": {"id": "configured-prompt", "version": "1"},
        "summary": {"text": "Receipt observation.", "evidence_refs": [HASH]}, "decision": None,
        "metrics": [metric_document()], "span": None, "attempt_status": None,
        "authorized": False, "evidence_accepted": False}


def report_document():
    event = event_document()
    return {"schema_version": REPORT, "run_id": "trace", "journal_sha256": HASH,
        "event_count": 1, "logical_event_count": 1,
        "trace": [{"event_ref": "evt-0001", "event_sha256": digest(event), "outer_event_sha256": HASH, "event": event}],
        "decisions": [], "metrics": [{"name": "cost", "unit": "currency", "currency": "USD",
            "pricing_ref": {"id": "provider-price-v1", "as_of": CLOCK}, "provenance": "imported",
            "trust_realm": "caller_import", "authenticated": False, "known_subtotal": "1.230000001",
            "total": None, "observations": 1, "unknown": 0, "logical_ids": ["cost-one"]}], "spans": [],
        "coverage": {"scope": "retained_journal", "expected_job_ids": [], "observed_job_ids": [], "missing_job_ids": [],
            "expected_attempt_ids": [], "observed_attempt_ids": ["attempt-one"], "missing_attempt_ids": [],
            "incomplete_attempt_ids": [], "whole_execution_authenticated": False, "unknown_event_count": 0},
        "observed_human_interventions": None, "limitations": ["Retained local observations do not prove complete execution."],
        "authorized": False, "evidence_accepted": False}


def documents():
    report = report_document()
    return {EVENT: event_document(), REPORT: report,
        PROJECTION: {"schema_version": PROJECTION, "run_id": "trace", "report_sha256": digest(report),
            "original_report_sha256": digest(report), "redaction_count": 0, "report": report,
            "measurement_authentication": "not_reverified", "report_is_declared": True,
            "authorized": False, "evidence_accepted": False},
        RECEIPT: {"schema_version": RECEIPT, "delivery_id": "delivery-one", "run_id": "trace",
            "projection_sha256": HASH, "sink_ref": "selected-sink", "status": "prepared", "attempts": 0,
            "max_attempts": 3, "deadline_utc": None, "ack_ref": None, "error_code": None,
            "historical": True, "authorized": False, "evidence_accepted": False},
        VERIFICATION: {"schema_version": VERIFICATION, "event_sha256": HASH,
            "workspace_binding": {"path": "/selected/workspace", "device": "1", "inode": "2"},
            "run_id": "trace", "agent_id": "worker", "controller_ref": "controller-one",
            "metric_ids": ["cost-one"], "verified_at": CLOCK, "expires_at": "2026-10-04T01:00:00Z",
            "authenticated": True, "scope": "event_measurements"},
        ACK: {"schema_version": ACK, "delivery_id": "delivery-one", "projection_sha256": HASH,
            "status": "acknowledged", "ack_ref": "sink-receipt"},
        JOURNAL: {"schema_version": JOURNAL, "run_id": "trace", "events": [{"event_id": "evt-0001"}],
            "sha256": HASH, "bytes": 24, "count": 1,
            "limits": {"max_bytes": 16777216, "max_event_bytes": 1048576, "max_events": 4096,
                "max_lines": 8192, "max_depth": 32}, "authorized": False, "evidence_accepted": False}}


@pytest.mark.parametrize("name", tuple(schemas()))
def test_roundtrip_detaches_every_supported_document(name):
    value = documents()[name]
    checked = validate_document(name, value)
    assert decode_document(name, encode_document(name, value)) == checked == value
    value["schema_version"] = "future/v9"
    assert checked["schema_version"] == name
    schema_document(name)["properties"].clear()
    assert schema_document(name)["properties"]


@pytest.mark.parametrize("name", [None, False, 5, [], {}, "future/v9", "x" * 10000],
                         ids=["null", "bool", "number", "list", "object", "future", "oversized"])
def test_unknown_schema_names_are_typed_refusals(name):
    with pytest.raises(WorkflowContractError) as error:
        schema_document(name)
    assert error.value.error_code == "TELEMETRY_VERSION_UNSUPPORTED"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "-0", "01", "1e3", "0.0000000001", 1.2, True])
def test_money_refuses_non_decimal_or_out_of_profile_values(value):
    event = event_document()
    event["metrics"][0]["value"] = value
    with pytest.raises(WorkflowContractError):
        validate_document(EVENT, event)


def test_unknown_billing_stays_null_and_preserves_receipt_provenance():
    event = event_document()
    event["metrics"][0]["value"] = None
    result = decode_document(EVENT, encode_document(EVENT, event))
    assert result["metrics"][0]["value"] is None
    assert result["metrics"][0]["provenance"] == "imported"
    assert result["metrics"][0]["source_ref"] == "provider-receipt"
    event["metrics"][0].update(provenance="unavailable", trust_realm="unavailable", value="0")
    with pytest.raises(WorkflowContractError):
        validate_document(EVENT, event)


@pytest.mark.parametrize("change", ["fractional_count", "wrong_unit", "missing_currency", "missing_price",
                                   "price_date", "duplicate_metric", "authority", "coverage", "raw_prompt"])
def test_closed_semantics_do_not_create_authority_or_hide_measurement_gaps(change):
    event = event_document()
    row = event["metrics"][0]
    if change == "fractional_count":
        event["metrics"] = [metric_document(name="tool_calls", value="1.5")]
    elif change == "wrong_unit":
        row["unit"] = "count"
    elif change == "missing_currency":
        row["currency"] = None
    elif change == "missing_price":
        row["pricing_ref"] = None
    elif change == "price_date":
        row["pricing_ref"]["as_of"] = "2026-02-30T00:00:00Z"
    elif change == "duplicate_metric":
        event["metrics"].append(copy.deepcopy(row))
    elif change == "authority":
        event["authorized"] = True
    elif change == "coverage":
        event["complete"] = True
    else:
        event["prompt"]["body"] = "Unbounded hidden input"
    with pytest.raises(WorkflowContractError):
        validate_document(EVENT, event)


def test_report_refuses_claimed_whole_execution_zero_and_total():
    report = report_document()
    for path, value in [("human", 0), ("coverage", True), ("total", "1.230000001")]:
        changed = copy.deepcopy(report)
        if path == "human":
            changed["observed_human_interventions"] = value
        elif path == "coverage":
            changed["coverage"]["whole_execution_authenticated"] = value
        else:
            changed["metrics"][0]["total"] = value
        with pytest.raises(WorkflowContractError):
            validate_document(REPORT, changed)


def test_aggregate_money_capacity_exceeds_individual_event_capacity_without_rounding():
    report = report_document()
    report["metrics"][0]["known_subtotal"] = "4095999999999999995.904000001"
    assert validate_document(REPORT, report)["metrics"][0]["known_subtotal"] == "4095999999999999995.904000001"


@pytest.mark.parametrize("kind", ["cycle", "depth", "huge", "duplicate_key", "nonfinite_json", "surrogate"])
def test_hostile_documents_and_json_refuse_before_unbounded_work(kind):
    if kind in {"duplicate_key", "nonfinite_json"}:
        raw = b'{"schema_version":"x","schema_version":"y"}' if kind == "duplicate_key" else b'{"number":NaN}'
        with pytest.raises(WorkflowContractError):
            decode_document(EVENT, raw)
        return
    value = event_document()
    if kind == "cycle":
        value["summary"]["text"] = value
    elif kind == "depth":
        raw = b"[" * 100 + b"0" + b"]" * 100
        with pytest.raises(WorkflowContractError) as error:
            decode_document(EVENT, raw)
        assert error.value.error_code == "TELEMETRY_LIMIT"
        return
    elif kind == "huge":
        value["summary"]["text"] = "x" * (MAX_EVENT_BYTES + 1)
    else:
        value["summary"]["text"] = "\ud800"
    with pytest.raises(WorkflowContractError):
        validate_document(EVENT, value)


def test_projection_separates_original_and_redacted_report_commitments():
    value = documents()[PROJECTION]
    value["report"]["trace"][0]["event"]["summary"]["text"] = "[redacted]"
    value["report"]["trace"][0]["event_sha256"] = digest(value["report"]["trace"][0]["event"])
    value["report_sha256"] = digest(value["report"])
    value["redaction_count"] = 1
    result = validate_document(PROJECTION, value)
    assert result["original_report_sha256"] != result["report_sha256"]
    assert result["report_is_declared"] and result["measurement_authentication"] == "not_reverified"
    value["report_sha256"] = HASH
    with pytest.raises(WorkflowContractError):
        validate_document(PROJECTION, value)


@pytest.mark.parametrize("change", ["attempts", "ack_missing", "expired", "wrong_scope", "job_binding"])
def test_delivery_and_verifier_shapes_do_not_imply_complete_measurement_coverage(change):
    name = RECEIPT if change in {"attempts", "ack_missing"} else VERIFICATION
    value = documents()[name]
    if change == "attempts":
        value.update(status="sending", attempts=3, max_attempts=2)
    elif change == "ack_missing":
        value.update(status="acknowledged", attempts=1)
    elif change == "expired":
        value["expires_at"] = value["verified_at"]
    elif change == "wrong_scope":
        value["scope"] = "complete_execution"
    else:
        name, value = EVENT, event_document()
        value["job_id"] = "job"
    with pytest.raises(WorkflowContractError):
        validate_document(name, value)
