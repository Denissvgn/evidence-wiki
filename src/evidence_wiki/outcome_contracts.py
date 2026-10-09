"""Closed observation contracts; decoding never authenticates an outcome."""

from __future__ import annotations

import json
from decimal import Decimal

from .errors import UsageError
from .mutation_contracts import _shape
from .telemetry_contracts import _clock, canonical, digest

BATCH = "outcome-observation-batch/v1"
VERIFICATION = "outcome-observation-verification/v1"
RETAINED = "outcome-observation-retained/v1"
REPORT = "outcome-observation-report/v1"
CLASSES = ("deterministic_verified", "agent_adjudicated", "simulated", "externally_observed")
SUBJECTS = ("automated", "agent", "simulation", "customer", "human", "business", "incident")
CLASS_SUBJECTS = {"deterministic_verified": {"automated"}, "agent_adjudicated": {"agent"},
                  "simulated": {"simulation"}, "externally_observed": {"customer", "human", "business", "incident"}}
# Metric, record kind, unit, denominator unit, admissible producer subjects.
METRICS = {
    "technical_success": ("usage", "count", "cases", ("automated", "agent", "simulation")),
    "sessions": ("usage", "count", "sessions", ("customer", "automated", "agent", "simulation")),
    "customer_adoption": ("usage", "count", "customers", ("customer",)),
    "incidents": ("incident", "count", "incidents", ("incident", "automated", "agent", "simulation")),
    "human_interventions": ("intervention", "count", "interventions", ("human",)),
    "human_work_seconds": ("intervention", "seconds", "work_records", ("human",)),
    "payments": ("business", "currency", "payments", ("business",)),
}
MAX_BATCHES = 64
MAX_RECORDS = 512


def refuse(reason):
    raise UsageError("OUTCOME_" + reason, "Outcome observation refused.", recoverable=False,
        details={"reason": reason}, remediation="Use an authorized current host verifier and retain exact scoped source evidence.") from None


def _obj(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _text(maximum=256):
    return {"type": "string", "minLength": 1, "maxLength": maximum,
            "pattern": r"^[^\x00-\x1f\x7f]+$(?![\s\S])"}


def _nullable(shape):
    return {"anyOf": [shape, {"type": "null"}]}


def _array(shape, maximum=MAX_RECORDS):
    return {"type": "array", "items": shape, "maxItems": maximum}


def schemas():
    """Return detached public shapes, including explicit inert report flags."""
    sha = {**_text(71), "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    identifier = {**_text(96), "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$(?![\s\S])"}
    utc = {**_text(32), "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)$(?![\s\S])"}
    decimal = {**_text(36), "pattern": r"^(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,9})?$(?![\s\S])"}
    count = {"type": "integer", "minimum": 0, "maximum": 1_000_000_000}
    source = _obj(source_id=identifier, origin=_text(4096), evidence_class={"enum": list(CLASSES)},
                  subject_kind={"enum": list(SUBJECTS)})
    binding = _obj(workspace=_obj(path=_text(4096), device=_text(64), inode=_text(64)), candidate_sha256=sha)
    period = _obj(started_at=utc, ended_at=utc)
    denominator = _nullable(_obj(value={**count, "minimum": 1}, unit=_text(32), population_ref=_text()))
    record = _obj(observation_id=identifier, entity_id=identifier, subject_id=identifier, occurred_at=utc,
                  value=_nullable(decimal))
    batch = _obj(schema_version={"const": BATCH}, batch_id=identifier, scope_id=identifier, binding=binding,
        source=source, source_receipt=_obj(ref=_text(4096), sha256=sha), period=period,
        record_kind={"enum": ["usage", "incident", "business", "intervention"]}, metric={"enum": list(METRICS)},
        unit={"enum": ["count", "seconds", "currency"]},
        currency=_nullable({**_text(3), "pattern": r"^[A-Z]{3}$(?![\s\S])"}), denominator=denominator,
        coverage={"enum": ["complete", "partial", "unknown"]}, gaps=_array(_text(), 32),
        exclusions=_array(_text(), 32), records=_array(record))
    verification = _obj(schema_version={"const": VERIFICATION}, batch_sha256=sha, scope_id=identifier,
        binding=binding, source=source, source_receipt_sha256=sha, controller_ref=_text(),
        authentication_ref=_text(4096), verified_at=utc, expires_at=utc)
    retained = _obj(schema_version={"const": RETAINED}, batch=batch, verification=verification)
    summary = _obj(source=source, period=period, metric={"enum": list(METRICS)}, unit=_text(32),
        currency=batch["properties"]["currency"], denominator=denominator, coverage=batch["properties"]["coverage"],
        gaps=_array(_text(), 32), exclusions=_array(_text(), 32), observed_records=count, unknown_records=count,
        known_subtotal=decimal, total=_nullable(decimal),
        rate=_nullable(_obj(numerator=decimal, denominator={**count, "minimum": 1}, population_ref=_text())),
        status={"enum": ["observed", "partial", "unknown"]}, batch_sha256s=_array(sha, MAX_BATCHES))
    statuses = _obj(adoption={"enum": ["adoption_observed", "adoption_partial", "adoption_unobserved"]},
        business_value={"const": "business_value_unobserved"},
        human_work={"enum": ["observed", "partial", "unobserved"]},
        human_interventions={"enum": ["observed", "partial", "unobserved"]},
        payments={"enum": ["observed", "partial", "unobserved"]},
        willingness_to_pay={"const": "unobserved"}, human_hours_saved={"const": "unobserved"})
    report = _obj(schema_version={"const": REPORT}, scope_id=identifier, binding=binding,
        evidence_classes=_obj(**{name: _array(summary, MAX_BATCHES) for name in CLASSES}),
        observations=_array(retained, MAX_BATCHES), statuses=statuses,
        technical_qualification_affected={"const": False}, authorized={"const": False},
        evidence_accepted={"const": False}, measurement_authentication={"const": "fresh_host_verification"},
        verified_at=utc, expires_at=_nullable(utc), limitations=_array(_text(2048), 16))
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
            for name, shape in ((BATCH, batch), (VERIFICATION, verification), (RETAINED, retained), (REPORT, report))}


def schema_document(name):
    if type(name) is not str or name not in schemas():
        refuse("VERSION_UNSUPPORTED")
    return schemas()[name]


def clock(value):
    try:
        return _clock(value)
    except UsageError:
        refuse("CLOCK_INVALID")


def validate_source(value):
    """Validate a host selection's closed identity and compatible class/subject."""
    value = clone(value)
    try:
        _shape(value, schema_document(BATCH)["properties"]["source"], "/source")
    except UsageError:
        refuse("SOURCE_INVALID")
    if value["subject_kind"] not in CLASS_SUBJECTS[value["evidence_class"]]:
        refuse("EVIDENCE_PROMOTION")
    return value


def clone(value):
    """Bound and detach caller JSON before any callback or schema walk."""
    try:
        raw = canonical(value)
        if len(raw) > 262_144:
            refuse("LIMIT")
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError, UsageError):
        refuse("INVALID")


def validate_document(name, value):
    """Decode exact shapes and semantic bounds without authenticating sources."""
    value = clone(value)
    try:
        _shape(value, schema_document(name), "/")
    except UsageError:
        refuse("INVALID")
    if name == BATCH:
        source = validate_source(value["source"])
        kind, unit, denominator_unit, allowed = METRICS[value["metric"]]
        if (source["subject_kind"] not in allowed or value["record_kind"] != kind or value["unit"] != unit
                or (unit == "currency") != (value["currency"] is not None)):
            refuse("EVIDENCE_PROMOTION")
        period = value["period"]
        start, end = clock(period["started_at"]), clock(period["ended_at"])
        if start >= end:
            refuse("PERIOD_INVALID")
        if value["denominator"] is not None and value["denominator"]["unit"] != denominator_unit:
            refuse("DENOMINATOR_INVALID")
        if value["coverage"] == "complete" and (value["gaps"] or value["exclusions"]):
            refuse("COVERAGE_CONFLICT")
        ids, entities = {}, {}
        for record in value["records"]:
            if not start <= clock(record["occurred_at"]) < end:
                refuse("RECORD_OUTSIDE_PERIOD")
            amount = record["value"]
            if amount is not None and unit == "count" and amount != "1":
                refuse("COUNT_INVALID")
            if value["metric"] == "customer_adoption" and record["entity_id"] != record["subject_id"]:
                refuse("CUSTOMER_IDENTITY_CONFLICT")
            if amount is not None and Decimal(amount) > Decimal("999999999999999.999999999"):
                refuse("VALUE_LIMIT")
            semantic = {key: item for key, item in record.items() if key != "observation_id"}
            prior = ids.setdefault(record["observation_id"], semantic)
            existing = entities.setdefault(record["entity_id"], semantic)
            if prior != semantic or existing != semantic:
                refuse("RECORD_CONFLICT")
        if value["denominator"] is not None and len(entities) > value["denominator"]["value"]:
            refuse("DENOMINATOR_CONFLICT")
    elif name == VERIFICATION:
        if clock(value["verified_at"]) >= clock(value["expires_at"]):
            refuse("VERIFICATION_INVALID")
    elif name == RETAINED:
        validate_document(BATCH, value["batch"])
        validate_document(VERIFICATION, value["verification"])
        verification, batch = value["verification"], value["batch"]
        if (verification["batch_sha256"] != digest(batch) or verification["scope_id"] != batch["scope_id"]
                or verification["binding"] != batch["binding"] or verification["source"] != batch["source"]
                or verification["source_receipt_sha256"] != batch["source_receipt"]["sha256"]):
            refuse("VERIFICATION_CONFLICT")
    return value
