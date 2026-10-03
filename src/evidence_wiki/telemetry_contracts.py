"""Bounded inert telemetry, decimal measurements and delivery contracts.

Validation authenticates neither a metric producer nor a verifier. Whole-run
coverage and human-intervention totals remain unsupported by these schemas.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal

from .mutation_contracts import _shape
from .workflow_contracts import WorkflowContractError

EVENT = "research-telemetry-event/v1"
REPORT = "research-telemetry-report/v1"
PROJECTION = "research-telemetry-projection/v1"
RECEIPT = "research-telemetry-receipt/v1"
VERIFICATION = "research-telemetry-verification/v1"
ACK = "research-telemetry-ack/v1"
JOURNAL = "research-telemetry-journal/v1"
MAX_EVENT_BYTES = 65_536
MAX_REPORT_BYTES = 262_144
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 4096
MAX_LINES = 8192
MAX_DEPTH = 16
MAX_NODES = 32_768
METRICS = ("tokens", "input_tokens", "output_tokens", "model_calls", "tool_calls", "cost",
           "human_interventions", "autonomous_interventions", "evaluator_disagreements",
           "repaired_defects", "circuit_breaker_trips", "recovery_count", "retry_attempts",
           "active_seconds", "waiting_seconds", "provider_seconds", "elapsed_seconds")
PROVENANCES = ("measured", "imported", "estimated", "unavailable")
TRUST_REALMS = ("local_artifact", "caller_import", "caller_estimate", "unavailable")
_UTC = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)$(?![\s\S])"


def refuse(reason, field="/"):
    raise WorkflowContractError("TELEMETRY_" + reason, "Telemetry contract refused the input.",
        recoverable=False, details={"field": field},
        remediation="Use the closed bounded schema, exact retained identities and explicit measurement provenance.") from None


def _object(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _text(maximum=256):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _nullable(shape):
    return {"anyOf": [shape, {"type": "null"}]}


def _array(shape, maximum=64):
    return {"type": "array", "items": shape, "maxItems": maximum}


def schemas():
    """Return detached schema documents; truth and authentication need an owner."""
    identifier = {**_text(96), "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$(?![\s\S])"}
    hashed = {**_text(71), "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    ref = {**_text(), "pattern": r"^[^\x00-\x20\x7f]+$(?![\s\S])"}
    utc = {**_text(32), "pattern": _UTC}
    decimal = {**_text(28), "pattern": r"^(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,9})?$(?![\s\S])"}
    aggregate_decimal = {**_text(36), "pattern": r"^(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,9})?$(?![\s\S])"}
    count = {"type": "integer", "minimum": 0, "maximum": 1_000_000}
    flags = dict(authorized={"const": False}, evidence_accepted={"const": False})
    pricing = _nullable(_object(id=ref, as_of=utc))
    provenance = {"enum": list(PROVENANCES)}
    trust = {"enum": list(TRUST_REALMS)}
    currency = _nullable({**_text(3), "pattern": r"^[A-Z]{3}$(?![\s\S])"})
    metric = _object(logical_id=identifier, name={"enum": list(METRICS)}, value=_nullable(decimal),
        unit={"enum": ["count", "seconds", "currency"]}, currency=currency, pricing_ref=pricing,
        provenance=provenance, source_ref=_nullable(ref), trust_realm=trust)
    model = _nullable(_object(id=ref, version=ref))
    span = _nullable(_object(kind={"enum": ["active", "waiting", "provider", "elapsed"]},
        started_at=utc, ended_at=_nullable(utc), clock_ref=ref))
    event = _object(schema_version={"const": EVENT}, logical_id=identifier, run_id=identifier, agent_id=identifier,
        request_id=_nullable(identifier), job_id=_nullable(identifier),
        job_sha256=_nullable({**_text(64), "pattern": r"^[a-f0-9]{64}$(?![\s\S])"}),
        attempt_id=_nullable(identifier), kind={"enum": ["decision", "attempt", "measurement", "span"]},
        occurred_at=utc, role=ref, controller_ref=ref, policy_id=_nullable(hashed), basis_id=_nullable(hashed),
        input_refs=_array(hashed, 32), output_refs=_array(hashed, 32), model=model, prompt=model,
        summary=_object(text={"type": "string", "maxLength": 2048}, evidence_refs=_array(hashed, 32)),
        decision=_nullable(_object(decision_ref=ref, outcome=_text(128), reason=_text(256))),
        metrics=_array(metric, 16), span=span,
        attempt_status=_nullable({"enum": ["started", "completed", "failed", "incomplete"]}), **flags)
    from .run_policy import DECISION
    from .run_policy import schema_document as policy_schema
    trace = _object(event_ref=ref, event_sha256=hashed, outer_event_sha256=hashed, event=event)
    decision = _object(event_ref=ref, request_id=identifier, decision=policy_schema(DECISION),
        job_id=_nullable(identifier), job_operation=_nullable(_text(32)))
    subtotal = _object(name={"enum": list(METRICS)}, unit={"enum": ["count", "seconds", "currency"]},
        currency=currency, pricing_ref=pricing, provenance=provenance, trust_realm=trust,
        authenticated={"type": "boolean"}, known_subtotal=_nullable(aggregate_decimal), total={"type": "null"},
        observations=count, unknown=count, logical_ids=_array(identifier, MAX_EVENTS))
    span_total = _object(kind={"enum": ["active", "waiting", "provider", "elapsed"]},
        controller_ref=ref, clock_ref=ref, authenticated={"type": "boolean"},
        interval_sum_milliseconds=aggregate_decimal, union_milliseconds=aggregate_decimal,
        duration_basis={"const": "declared_utc"}, total_milliseconds={"type": "null"}, overlap={"type": "boolean"},
        incomplete=count, event_refs=_array(ref, MAX_EVENTS))
    report = _object(schema_version={"const": REPORT}, run_id=identifier, journal_sha256=hashed,
        event_count=count, logical_event_count=count, trace=_array(trace, MAX_EVENTS),
        decisions=_array(decision, MAX_EVENTS), metrics=_array(subtotal, 1024), spans=_array(span_total, 1024),
        coverage=_object(scope={"const": "retained_journal"}, expected_attempt_ids=_array(identifier, MAX_EVENTS),
            expected_job_ids=_array(identifier, MAX_EVENTS), observed_job_ids=_array(identifier, MAX_EVENTS),
            missing_job_ids=_array(identifier, MAX_EVENTS),
            observed_attempt_ids=_array(identifier, MAX_EVENTS), missing_attempt_ids=_array(identifier, MAX_EVENTS),
            incomplete_attempt_ids=_array(identifier, MAX_EVENTS), whole_execution_authenticated={"const": False},
            unknown_event_count=count),
        observed_human_interventions={"type": "null"}, limitations=_array(_text(2048), 32), **flags)
    projection = _object(schema_version={"const": PROJECTION}, run_id=identifier, report_sha256=hashed,
        original_report_sha256=hashed,
        redaction_count=count, report=report, measurement_authentication={"const": "not_reverified"},
        report_is_declared={"const": True}, **flags)
    receipt = _object(schema_version={"const": RECEIPT}, delivery_id=identifier, run_id=identifier,
        projection_sha256=hashed, sink_ref=ref, status={"enum": ["prepared", "sending", "acknowledged", "failed", "unknown"]},
        attempts={"type": "integer", "minimum": 0, "maximum": 3},
        max_attempts={"type": "integer", "minimum": 1, "maximum": 3},
        deadline_utc=_nullable(utc), ack_ref=_nullable(ref), error_code=_nullable(identifier),
        historical={"const": True}, **flags)
    verification = _object(schema_version={"const": VERIFICATION}, event_sha256=hashed,
        workspace_binding=_object(path=_text(4096), device=_text(64), inode=_text(64)),
        run_id=identifier, agent_id=identifier, controller_ref=ref, metric_ids=_array(identifier, 16),
        verified_at=utc, expires_at=utc, authenticated={"const": True}, scope={"const": "event_measurements"})
    ack = _object(schema_version={"const": ACK}, delivery_id=identifier, projection_sha256=hashed,
        status={"enum": ["acknowledged", "failed"]}, ack_ref=_nullable(ref))
    journal = _object(schema_version={"const": JOURNAL}, run_id=identifier,
        events=_array({"type": "object", "maxProperties": 4096, "additionalProperties": True}, MAX_EVENTS),
        sha256=hashed, bytes={"type": "integer", "minimum": 0, "maximum": MAX_JOURNAL_BYTES}, count=count,
        limits=_object(max_bytes={"const": MAX_JOURNAL_BYTES}, max_event_bytes={"const": 1_048_576},
            max_events={"const": MAX_EVENTS}, max_lines={"const": MAX_LINES}, max_depth={"const": 32}), **flags)
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape} for name, shape in (
        (EVENT, event), (REPORT, report), (PROJECTION, projection), (RECEIPT, receipt),
        (VERIFICATION, verification), (ACK, ack), (JOURNAL, journal))}


def schema_document(name):
    if type(name) is not str or len(name) > 64 or name not in schemas():
        refuse("VERSION_UNSUPPORTED", "/schema_version")
    return schemas()[name]


def _bounded(value, *, journal=False):
    pending, ancestors = [(value, 1, False)], set()
    nodes = strings = 0
    maximum = MAX_JOURNAL_BYTES if journal else MAX_REPORT_BYTES
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            ancestors.remove(id(item))
            continue
        nodes += 1
        if nodes > (524288 if journal else MAX_NODES) or depth > (32 if journal else MAX_DEPTH):
            refuse("LIMIT")
        if type(item) in (dict, list):
            if id(item) in ancestors:
                refuse("INVALID")
            if len(item) > (4096 if journal or type(item) is list else 128):
                refuse("LIMIT")
            if type(item) is dict and any(type(key) is not str for key in item):
                refuse("INVALID")
            ancestors.add(id(item))
            pending.append((item, depth, True))
            children = [child for pair in item.items() for child in pair] if type(item) is dict else item
            pending.extend((child, depth + 1, False) for child in children)
        elif type(item) is str:
            if len(item) > maximum:
                refuse("LIMIT")
            try:
                strings += len(item.encode("utf-8"))
            except UnicodeError:
                refuse("INVALID")
            if strings > maximum:
                refuse("LIMIT")
        elif type(item) is int:
            if item.bit_length() > 256:
                refuse("LIMIT")
        elif type(item) is float:
            if not math.isfinite(item):
                refuse("INVALID")
        elif item is not None and type(item) is not bool:
            refuse("INVALID")


def canonical(value):
    """Canonical metadata bytes; this helper provides no provenance validation."""
    _bounded(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def metric_binding(event, metric):
    """Bind one logical measurement to every immutable execution correlation."""
    fields = ("run_id", "agent_id", "request_id", "job_id", "job_sha256", "attempt_id",
              "role", "controller_ref", "policy_id", "basis_id", "input_refs", "output_refs", "model", "prompt")
    value = {"metric": metric, **{name: event[name] for name in fields},
             "decision_ref": None if event["decision"] is None else event["decision"]["decision_ref"]}
    return json.loads(canonical(value))


def _clock(value):
    try:
        if type(value) is not str or re.fullmatch(_UTC, value) is None:
            refuse("INVALID", "/clock")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        refuse("INVALID", "/clock")


def _metric(value):
    name, amount = value["name"], value.get("value", value.get("known_subtotal"))
    expected = "currency" if name == "cost" else "seconds" if name.endswith("_seconds") else "count"
    if value["unit"] != expected:
        refuse("INVALID", "/metrics/unit")
    if amount is not None and expected == "count" and Decimal(amount) != Decimal(amount).to_integral_value():
        refuse("INVALID", "/metrics/value")
    if expected == "currency":
        if amount is not None and (value["currency"] is None or value["pricing_ref"] is None):
            refuse("INVALID", "/metrics/pricing_ref")
    elif value["currency"] is not None or value["pricing_ref"] is not None:
        refuse("INVALID", "/metrics/currency")
    if value["pricing_ref"] is not None:
        _clock(value["pricing_ref"]["as_of"])


def validate_document(name, value):
    """Detach a closed document. Verifier outputs still require trusted intake."""
    try:
        shape = schema_document(name)
        _bounded(value, journal=name == JOURNAL)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        maximum = MAX_JOURNAL_BYTES if name == JOURNAL else MAX_EVENT_BYTES if name == EVENT else MAX_REPORT_BYTES
        if len(raw) > maximum:
            refuse("LIMIT")
        value = json.loads(raw)
        _shape(value, shape)
        if name == EVENT:
            _clock(value["occurred_at"])
            if (value["job_id"] is None) != (value["job_sha256"] is None):
                refuse("INVALID", "/job_sha256")
            if len({row["logical_id"] for row in value["metrics"]}) != len(value["metrics"]):
                refuse("INVALID", "/metrics/logical_id")
            for row in value["metrics"]:
                _metric(row)
                if row["provenance"] == "unavailable" and row["value"] is not None:
                    refuse("INVALID", "/metrics/provenance")
                if row["provenance"] == "unavailable" and row["trust_realm"] != "unavailable":
                    refuse("INVALID", "/metrics/trust_realm")
            if value["kind"] == "measurement" and not value["metrics"]:
                refuse("INVALID", "/metrics")
            if value["kind"] == "span" and value["span"] is None:
                refuse("INVALID", "/span")
            if value["kind"] == "decision" and value["decision"] is None:
                refuse("INVALID", "/decision")
            if value["kind"] == "attempt" and (value["attempt_id"] is None or value["attempt_status"] is None):
                refuse("INVALID", "/attempt_id")
            if value["span"] is not None:
                span = value["span"]
                start = _clock(span["started_at"])
                if span["ended_at"] is not None and _clock(span["ended_at"]) < start:
                    refuse("INVALID", "/span/ended_at")
        elif name == REPORT:
            if value["logical_event_count"] != len(value["trace"]) or value["logical_event_count"] > value["event_count"]:
                refuse("INVALID", "/logical_event_count")
            for row in value["trace"]:
                event = validate_document(EVENT, row["event"])
                if event["run_id"] != value["run_id"] or digest(event) != row["event_sha256"]:
                    refuse("INVALID", "/trace")
            for row in value["metrics"]:
                _metric(row)
                if row["unknown"] > row["observations"] or row["unknown"] and row["total"] is not None:
                    refuse("INVALID", "/metrics/total")
                if len(row["logical_ids"]) != len(set(row["logical_ids"])):
                    refuse("INVALID", "/metrics/logical_ids")
            for row in value["spans"]:
                if (row["overlap"] or row["incomplete"]) and row["total_milliseconds"] is not None:
                    refuse("INVALID", "/spans/total_milliseconds")
                if Decimal(row["union_milliseconds"]) > Decimal(row["interval_sum_milliseconds"]):
                    refuse("INVALID", "/spans/union_milliseconds")
        elif name == PROJECTION:
            report = validate_document(REPORT, value["report"])
            if report["run_id"] != value["run_id"] or digest(report) != value["report_sha256"]:
                refuse("INVALID", "/report_sha256")
        elif name == RECEIPT:
            if value["attempts"] > value["max_attempts"] or (value["status"] == "prepared") != (value["attempts"] == 0):
                refuse("INVALID", "/attempts")
            if value["status"] == "acknowledged" and value["ack_ref"] is None:
                refuse("INVALID", "/ack_ref")
            if value["deadline_utc"] is not None:
                _clock(value["deadline_utc"])
        elif name == ACK:
            if value["status"] == "acknowledged" and value["ack_ref"] is None:
                refuse("INVALID", "/ack_ref")
        elif name == VERIFICATION:
            if _clock(value["expires_at"]) <= _clock(value["verified_at"]) or len(value["metric_ids"]) != len(set(value["metric_ids"])):
                refuse("INVALID", "/expires_at")
        elif name == JOURNAL:
            if value["count"] != len(value["events"]):
                refuse("INVALID", "/count")
        return value
    except WorkflowContractError:
        raise
    except Exception:
        refuse("INVALID")


def encode_document(name, value):
    checked = validate_document(name, value)
    return json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode_document(name, raw):
    schema_document(name)
    maximum = MAX_JOURNAL_BYTES if name == JOURNAL else MAX_EVENT_BYTES if name == EVENT else MAX_REPORT_BYTES
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        refuse("LIMIT")
    try:
        depth = tokens = 0
        quoted = escaped = scalar = False
        for char in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:
                    escaped = True
                elif char == 34:
                    quoted = False
                continue
            if char == 34:
                quoted, scalar = True, False
                tokens += 1
            elif char in (123, 91):
                depth += 1
                tokens += 1
                scalar = False
            elif char in (125, 93):
                depth -= 1
                tokens += 1
                scalar = False
            elif char in (44, 58):
                tokens += 1
                scalar = False
            elif char in (9, 10, 13, 32):
                scalar = False
            elif not scalar:
                scalar = True
                tokens += 1
            if depth > (32 if name == JOURNAL else MAX_DEPTH) or tokens > (1048576 if name == JOURNAL else MAX_NODES * 4):
                refuse("LIMIT")
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    refuse("INVALID")
                result[key] = value
            return result
        return validate_document(name, json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=lambda _: refuse("INVALID")))
    except WorkflowContractError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        refuse("INVALID")
