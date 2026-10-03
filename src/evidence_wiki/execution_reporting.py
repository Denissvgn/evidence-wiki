"""Bounded public metadata over already authenticated canonical execution records.

This projection is not a journal authenticator. The owning session verifies its
retained host signatures before calling it. Private inputs, outputs and signing
material remain in that journal; reports expose their content commitments.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import execution_accounting as accounting
from .agent_execution import ExecutionProfile
from .mutation_contracts import _shape, canonical, detached, digest
from .run_policy import DECISION, OBSERVATION, OBSERVATION_V2, RunPolicy
from .run_policy import validate_document as policy_document
from .telemetry import _SECRET
from .workflow_contracts import WorkflowContractError

REPORT = "research-execution-report/v2"
MAX_RECORDS = 4096
MAX_PAGE = 64
MAX_BYTES = 262_144
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_FIELDS = {"schema_version", "kind", "binding_id", "request_id", "sequence", "previous_record_ref", "data", "authentication", "event_ref"}
_COUNTERS = ("durable_advance_intents", "durable_steps", "native_attempt_intents", "external_attempt_intents",
    "retry_attempt_intents", "repair_attempt_intents", "cancel_requests", "cancellation_confirmed_attempts",
    "native_recovery_observations", "native_history_unknown_records", "historical_poll_intents", "reconciliation_steps",
    "failed_review_applications", "critical_review_failures", "conflicting_review_basis_groups", "budget_stop_decisions",
    "application_failure_records", "host_unknown_records", "terminal_step_records", "policy_revalidation_step_records")
_UNOBSERVED = ("autonomous_interventions", "repaired_defects", "circuit_breaker_trips",
    "active_execution_seconds_total", "waiting_seconds_total")


def refuse(reason):
    raise WorkflowContractError("EXECUTION_REPORT_" + reason, "Execution report refused the input.",
        recoverable=False, details={"reason": reason},
        remediation="Authenticate the canonical journal and preserve bounded redacted metadata.") from None


def _object(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def _text(size=256):
    return {"type": "string", "minLength": 1, "maxLength": size}


def _nullable(shape):
    return {"anyOf": [shape, {"type": "null"}]}


def _array(shape, maximum):
    return {"type": "array", "items": shape, "maxItems": maximum}


def schema_document(name=REPORT):
    """Export the closed metadata codec; it conveys no acceptance authority."""
    if name != REPORT:
        refuse("VERSION_UNSUPPORTED")
    text, hashed = _text(), {**_text(71), "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    count = {"type": "integer", "minimum": 0, "maximum": MAX_RECORDS}
    boolean = {"type": "boolean"}
    usage = _object(**{key: _nullable({"type": "number", "minimum": 0} if key == "seconds" else
        {"type": "integer", "minimum": 0} if key in accounting.COUNT_FIELDS else _text(36)) for key in accounting.USAGE_FIELDS})
    seconds = {**_text(28), "pattern": r"^(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,9})?$(?![\s\S])"}
    timing = _nullable(_object(provenance={"enum": ["canonical_native_return", "host_executor_receipt"]},
        elapsed_scope={"enum": ["in_process_effect_elapsed", "utc_lifetime_including_recovery_wait",
            "host_observed_usage_seconds", "unavailable"]}, lifetime_seconds=_nullable(seconds),
        active_execution_seconds=_nullable(seconds), waiting_seconds={"type": "null"},
        invocation_proof={"enum": ["desired_state_only", "canonical_owner_receipt", "observed_owner_call", "not_invoked", "unavailable"]}))
    decision = _object(outcome=text, reason=text, terminal=boolean, original_count=count, eligible_count=count,
        unresolved_count=count, selected_outcome_refs=_array(text, 100), basis_ref=_nullable(text), policy_id=hashed,
        decision_input_ref=hashed, input_kind={"enum": ["policy_observation", "native_terminal"]}, reproduced={"const": True})
    row = _object(sequence=count, event_ref=text, record_ref=hashed, kind=text, request_id=text,
        attempt_id=_nullable(text), job_id=_nullable(text), purpose=_nullable(text), role=_nullable(text), controller_ref=_nullable(text), model_ref=_nullable(text),
        prompt_version=_nullable(text), input_ref=_nullable(hashed), output_ref=_nullable(hashed), basis_ref=_nullable(text),
        issued_at=_nullable(text), deadline_at=_nullable(text), started_at=_nullable(text), completed_at=_nullable(text),
        observed_at=_nullable(text), state=_nullable(text), usage=_nullable(usage), timing=timing, decision=_nullable(decision))
    accounting_shape = accounting.schema_document(accounting.REPORT)
    summary = _nullable(_object(audit_ref=hashed, scope_id=text,
        coverage=accounting_shape["properties"]["coverage"], totals=accounting_shape["properties"]["totals"],
        observed_human_interventions=accounting_shape["properties"]["observed_human_interventions"],
        limitations=_array(text, 64)))
    profile = _object(profile_ref=text, profile_id=hashed, currency=_text(3), pricing_ref=text,
        roles=_array(_object(config_ref=text, role=text, controller_ref=text, model_ref=text, prompt_version=text), 32))
    shape = _object(schema_version={"const": REPORT}, run_id=text, binding_id=hashed, policy_id=hashed,
        profile=profile, snapshot_ref=hashed, record_count=count, trace=_array(row, MAX_PAGE),
        trace_complete=boolean, chain_complete=boolean, missing_sequences=_array(count, MAX_RECORDS),
        paging=_object(after_sequence=count, max_records={"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
            returned=count, next_after_sequence=_nullable(count), has_more=boolean, truncated=boolean),
        accounting=summary, activity_summary=_object(count_scope={"const": "retained_canonical_records"},
            record_count=count, snapshot_complete=boolean, execution_closed=boolean, totals_complete=boolean,
            counts=_object(**dict.fromkeys(_COUNTERS, count)),
            unobserved_measures=_object(**{name: {"type": "null"} for name in _UNOBSERVED}), limitations=_array(text, 8)),
        redaction_count=count, limitations=_array(text, 64),
        authorized={"const": False}, evidence_accepted={"const": False})
    return json.loads(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}))


def validate_report(value):
    """Validate an inert report, without trusting its claim of authentication."""
    value = detached(value)
    if len(canonical(value)) > MAX_BYTES:
        refuse("LIMIT")
    try:
        _shape(value, schema_document())
    except (Exception, SystemExit):
        refuse("INVALID")
    if value["paging"]["returned"] != len(value["trace"]):
        refuse("PAGING_CONFLICT")
    if value["trace_complete"] and (not value["trace"] or not value["chain_complete"]
            or value["paging"]["has_more"] or value["paging"]["after_sequence"] != 0
            or len(value["trace"]) != value["record_count"]):
        refuse("COVERAGE_CONFLICT")
    activity = value["activity_summary"]
    if (activity["record_count"] != value["record_count"]
            or any(count > value["record_count"] for count in activity["counts"].values())
            or activity["snapshot_complete"] != (value["record_count"] > 0 and value["chain_complete"])
            or activity["totals_complete"] and (not activity["snapshot_complete"] or not activity["execution_closed"]
                or value["accounting"] is None or not value["accounting"]["coverage"]["coverage_complete"])):
        refuse("SUMMARY_CONFLICT")
    return value


def _hash(value):
    if value is None:
        return None
    if type(value) is str and re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", value):
        return value if value.startswith("sha256:") else "sha256:" + value
    return digest(value)


def _clean(value, redactions):
    if value is None:
        return None
    if type(value) is dict:
        return {key: _clean(child, redactions) for key, child in value.items()}
    if type(value) is list:
        return [_clean(child, redactions) for child in value]
    if type(value) is str and (_SECRET.search(value) or value.startswith(("/", "~", "\\"))
            or re.match(r"^[A-Za-z]:[\\/]", value) or len(value) > 256):
        redactions[0] += 1
        return digest(value)
    return value


def _decision(policy, value, observed):
    value = policy_document(DECISION, value)
    if type(observed) is not dict or value["policy_id"] != policy.policy_id:
        refuse("DECISION_CONFLICT")
    if observed.get("schema_version") in {OBSERVATION, OBSERVATION_V2}:
        reconstructed = policy.decide(observed)
        kind = "policy_observation"
    else:
        fields = {"basis_id", "policy_id", "outcome", "reason", "original_ids", "policy_compatible", "authority",
            "qualification", "usage", "steps_used", "steps_limit", "pending"}
        if (set(observed) != fields or observed["policy_id"] != policy.policy_id
                or type(observed["original_ids"]) is not list
                or sorted(observed["original_ids"]) != sorted(policy.to_dict()["scope"]["question_ids"])
                or observed["outcome"] in {"accepted", "partial"}):
            refuse("DECISION_CONFLICT")
        count = len(observed["original_ids"])
        reconstructed = {"schema_version": DECISION, "policy_id": policy.policy_id,
            "observation_sha256": hashlib.sha256(canonical(observed)).hexdigest(),
            "basis_id": observed["basis_id"], "outcome": observed["outcome"], "reason": observed["reason"],
            "terminal": True, "next_effect": None, "original_count": count, "eligible_count": 0,
            "unresolved_count": count, "selected_outcome_refs": [], "authorized": False, "evidence_accepted": False}
        kind = "native_terminal"
    if reconstructed != value:
        refuse("DECISION_CONFLICT")
    evidence = {key: row for key, row in observed.items() if key != "agent_rationale"}
    return {key: value[key] for key in ("outcome", "reason", "terminal", "original_count", "eligible_count",
        "unresolved_count", "selected_outcome_refs", "policy_id")} | {"basis_ref": value["basis_id"],
        "decision_input_ref": digest(evidence), "input_kind": kind, "reproduced": True}


def _metadata(record, policy, attempts=None, configured_roles=None):
    data = record["data"]
    result = {"sequence": record["sequence"], "event_ref": record["event_ref"],
        "record_ref": digest({key: value for key, value in record.items() if key != "event_ref"}),
        "kind": record["kind"], "request_id": record["request_id"],
        **dict.fromkeys(("attempt_id", "job_id", "purpose", "role", "controller_ref", "model_ref", "prompt_version", "input_ref", "output_ref",
            "basis_ref", "issued_at", "deadline_at", "started_at", "completed_at", "observed_at", "state", "usage", "timing", "decision"))}
    attempt = data.get("attempt") if record["kind"] == "execution_attempt" else None
    observed = data.get("observation") if record["kind"] in {"execution_observation", "execution_native_observation"} else None
    if type(attempt) is dict:
        job = attempt.get("job", {})
        result.update(attempt_id=attempt.get("attempt_id"), role=job.get("role"), controller_ref=job.get("controller_ref"),
            job_id=job.get("id"), purpose=data.get("purpose", job.get("operation")),
            prompt_version=job.get("prompt_version"), input_ref=digest(attempt.get("input")),
            basis_ref=job.get("basis_id"), issued_at=job.get("issued_utc"), deadline_at=job.get("deadline_utc"))
        if configured_roles and attempt.get("config_ref") in configured_roles:
            result["model_ref"] = configured_roles[attempt["config_ref"]]["model_ref"]
    if record["kind"] == "execution_native":
        reservation = data.get("reservation", {})
        result.update(attempt_id=reservation.get("attempt_id"), job_id=reservation.get("job_id"),
            purpose=data.get("operation"), controller_ref=reservation.get("controller_ref"), role="canonical_owner",
            input_ref=digest(data.get("arguments")), issued_at=reservation.get("issued_at"))
    if type(observed) is dict:
        result.update(attempt_id=observed.get("attempt_id"), controller_ref=observed.get("controller_ref"),
            job_id=observed.get("job_id"),
            model_ref=observed.get("model_ref"), prompt_version=observed.get("prompt_version"),
            input_ref=_hash(observed.get("input_sha256")), output_ref=_hash(observed.get("output_sha256")),
            issued_at=observed.get("issued_utc"), deadline_at=observed.get("deadline_utc"),
            started_at=observed.get("started_utc"), completed_at=observed.get("completed_utc"),
            observed_at=observed.get("observed_utc", observed.get("observed_at")), state=observed.get("state"),
            usage=observed.get("observed_usage", observed.get("usage")))
        if result["usage"] is not None:
            result["usage"] = detached(result["usage"])
            if type(result["usage"]["seconds"]) is str:
                result["usage"]["seconds"] = float(result["usage"]["seconds"])
        if record["kind"] == "execution_native_observation":
            result["output_ref"] = digest(data["result"]) if data.get("result") is not None else None
            result["basis_ref"] = data.get("after_input_basis")
            result["timing"] = {"provenance": "canonical_native_return", "elapsed_scope": data.get("elapsed_scope", "unavailable"),
                "lifetime_seconds": data.get("lifetime_seconds"), "active_execution_seconds": data.get("active_execution_seconds"),
                "waiting_seconds": None, "invocation_proof": data.get("invocation_proof", "unavailable")}
        else:
            result["timing"] = {"provenance": "host_executor_receipt", "elapsed_scope": "host_observed_usage_seconds",
                "lifetime_seconds": None, "active_execution_seconds": None, "waiting_seconds": None, "invocation_proof": "unavailable"}
    if record["kind"] == "execution_step":
        step = data.get("step")
        if type(step) is not dict:
            refuse("STEP_INVALID")
        result.update(attempt_id=step.get("attempt_id"), basis_ref=step.get("before_basis_id"), state=step.get("outcome"))
        if step.get("decision") is not None:
            result["decision"] = _decision(policy, step["decision"], data.get("decision_input"))
    if attempts and result["attempt_id"] in attempts:
        intent = attempts[result["attempt_id"]]
        result["purpose"] = intent.get("purpose", intent.get("operation"))
        reservation = intent.get("reservation", {})
        result["job_id"] = result["job_id"] or reservation.get("job_id")
    return result


def _activity_summary(records, *, chain_complete, accounting_summary):
    """Count canonical lifecycle facts, without accepting model-provided counters."""
    counts = dict.fromkeys(_COUNTERS, 0)
    cancelled, reviews = set(), {}
    kinds = {"execution_advance": "durable_advance_intents", "execution_step": "durable_steps",
        "execution_native": "native_attempt_intents", "execution_attempt": "external_attempt_intents",
        "execution_cancel": "cancel_requests", "execution_native_history_unknown": "native_history_unknown_records",
        "execution_historical_poll": "historical_poll_intents", "execution_application_failed": "application_failure_records",
        "execution_host_unknown": "host_unknown_records"}
    for record in records:
        kind, data = record["kind"], record["data"]
        if kind in kinds:
            counts[kinds[kind]] += 1
        if kind == "execution_attempt" and data.get("purpose") in {"retry", "repair"}:
            counts[data["purpose"] + "_attempt_intents"] += 1
        if kind == "execution_native_observation" and (data.get("elapsed_scope") == "utc_lifetime_including_recovery_wait"
                or data.get("invocation_proof") in {"canonical_owner_receipt", "desired_state_only"}):
            counts["native_recovery_observations"] += 1
        if kind == "execution_observation":
            observed = data.get("observation", {})
            if observed.get("cancellation_confirmed") is True and observed.get("state") == "cancelled":
                cancelled.add(observed["attempt_id"])
        if kind == "execution_applied" and data.get("operation") == "evaluate":
            if data.get("passed") is False:
                counts["failed_review_applications"] += 1
            if data.get("critical_failure") is True:
                counts["critical_review_failures"] += 1
            key = (data.get("claim_id"), data.get("strict_basis_id"))
            if all(type(item) is str for item in (*key, data.get("profile_ref"))) and type(data.get("passed")) is bool:
                reviews.setdefault(key, []).append((data["profile_ref"], data["passed"]))
        if kind == "execution_step":
            step = data.get("step", {})
            counts["reconciliation_steps"] += step.get("action") == "reconcile"
            counts["terminal_step_records"] += step.get("terminal") is True
            counts["policy_revalidation_step_records"] += type(data.get("revalidates_request_id")) is str
            counts["budget_stop_decisions"] += (step.get("decision") or {}).get("outcome") == "stop_budget"
    counts["cancellation_confirmed_attempts"] = len(cancelled)
    counts["conflicting_review_basis_groups"] = sum(len({profile for profile, _passed in group}) > 1
        and len({passed for _profile, passed in group}) > 1 for group in reviews.values())
    closed = any(record["kind"] == "execution_close" for record in records)
    complete = bool(records) and chain_complete
    return {"count_scope": "retained_canonical_records", "record_count": len(records), "snapshot_complete": complete,
        "execution_closed": closed, "totals_complete": complete and closed and accounting_summary is not None
            and accounting_summary["coverage"]["coverage_complete"], "counts": counts,
        "unobserved_measures": dict.fromkeys(_UNOBSERVED),
        "limitations": ["Counts describe canonical retained lifecycle records, including records outside the metadata page.",
            "Repair intent and conflicting reviews do not establish repaired defects or a calibrated disagreement total.",
            "Active and waiting totals and caller autonomy are unobserved; native lifetime is not active execution."]}


def report(policy, profile, binding_id, run_id, records, accounting_report, *, after_sequence=0, max_records=64):
    """Project an authenticated snapshot; cursor zero means no consumed records.

    The next cursor is one greater than the last delivered sequence. It is bound
    to ``snapshot_ref`` so consumers can detect changes between paged reads.
    """
    if (type(policy) is not RunPolicy or type(after_sequence) is not int or not 0 <= after_sequence <= MAX_RECORDS
            or type(max_records) is not int or not 1 <= max_records <= MAX_PAGE
            or type(records) is not list or len(records) > MAX_RECORDS):
        refuse("LIMIT")
    if type(profile) is ExecutionProfile:
        profile = profile.document()
    elif type(profile) is dict:
        profile = detached(profile)
    else:
        refuse("PROFILE_INVALID")
    if type(profile) is not dict or any(key not in profile for key in ("profile_ref", "currency", "pricing_ref", "roles")):
        refuse("PROFILE_INVALID")
    previous, ids, sequences, missing, checked_records = None, set(), [], [], []
    snapshot = hashlib.sha256(b"[")
    snapshot_bytes = 2
    chain_complete = True
    for record in records:
        record = detached(record)
        raw = canonical(record)
        snapshot_bytes += len(raw) + (1 if checked_records else 0)
        if snapshot_bytes > MAX_JOURNAL_BYTES:
            refuse("LIMIT")
        if checked_records:
            snapshot.update(b",")
        snapshot.update(raw)
        if (type(record) is not dict or set(record) != _FIELDS or record["binding_id"] != binding_id
                or record["schema_version"] != "research-execution-journal/v1"
                or type(record["request_id"]) is not str
                or type(record["sequence"]) is not int or not 0 <= record["sequence"] < MAX_RECORDS
                or record["request_id"] in ids or type(record["data"]) is not dict):
            refuse("JOURNAL_CONFLICT")
        ids.add(record["request_id"])
        expected_sequence = 0 if previous is None else previous["sequence"] + 1
        if record["sequence"] < expected_sequence:
            refuse("JOURNAL_CONFLICT")
        if record["sequence"] != expected_sequence:
            chain_complete = False
            missing.extend(range(expected_sequence, record["sequence"]))
        elif record["previous_record_ref"] != (None if previous is None else digest(previous)):
            refuse("JOURNAL_CONFLICT")
        sequences.append(record["sequence"])
        previous = {key: value for key, value in record.items() if key != "event_ref"}
        checked_records.append(record)
    snapshot.update(b"]")
    records = checked_records
    page = [record for record in records if record["sequence"] >= after_sequence][:max_records]
    more = bool(page and any(sequence > page[-1]["sequence"] for sequence in sequences))
    attempts = {}
    for record in records:
        if record["kind"] == "execution_attempt":
            attempts[record["data"]["attempt"]["attempt_id"]] = record["data"]
        elif record["kind"] == "execution_native":
            attempts[record["data"]["reservation"]["attempt_id"]] = record["data"]
    configured_roles = {role["config_ref"]: role for role in profile["roles"]}
    metadata = [_metadata(record, policy, attempts, configured_roles) for record in page]
    # Reconstruct every retained decision, including those outside this page.
    for record in records:
        if record["kind"] == "execution_step":
            _metadata(record, policy)
    summary = None
    if accounting_report is not None:
        accounting_report = accounting.validate_document(accounting.REPORT, accounting_report)
        if accounting_report["binding"].get("session_binding_id") != binding_id:
            refuse("ACCOUNTING_BINDING_CONFLICT")
        declared = set()
        for row in records:
            if row["kind"] == "execution_attempt":
                declared.add(row["data"]["attempt"]["attempt_id"])
            elif row["kind"] == "execution_native":
                declared.add(row["data"]["reservation"]["attempt_id"])
        if set(accounting_report["coverage"]["expected_attempt_ids"]) != declared:
            refuse("ACCOUNTING_INVENTORY_CONFLICT")
        summary = {key: accounting_report[key] for key in ("scope_id", "coverage", "totals", "observed_human_interventions", "limitations")}
        summary["audit_ref"] = digest(accounting_report)
        if (not records or not chain_complete) and summary["coverage"]["coverage_complete"]:
            refuse("ACCOUNTING_COVERAGE_CONFLICT")
    redactions = [0]
    result = {"schema_version": REPORT, "run_id": run_id, "binding_id": binding_id, "policy_id": policy.policy_id,
        "profile": {key: profile[key] for key in ("profile_ref", "currency", "pricing_ref", "roles")}
            | {"profile_id": digest(profile)},
        "snapshot_ref": "sha256:" + snapshot.hexdigest(), "record_count": len(records), "trace": metadata,
        "trace_complete": bool(records) and chain_complete and after_sequence == 0 and len(page) == len(records),
        "chain_complete": bool(records) and chain_complete, "missing_sequences": missing,
        "paging": {"after_sequence": after_sequence, "max_records": max_records, "returned": len(page),
            "next_after_sequence": page[-1]["sequence"] + 1 if more else None, "has_more": more,
            "truncated": more or after_sequence != 0},
        "accounting": summary, "activity_summary": _activity_summary(records, chain_complete=chain_complete, accounting_summary=summary), "redaction_count": 0,
        "limitations": [] if records and chain_complete else ["No complete authenticated execution trace is available."],
        "authorized": False, "evidence_accepted": False}
    if more or after_sequence != 0:
        result["limitations"].append("This page contains a bounded portion of the snapshot metadata.")
    result = _clean(result, redactions)
    result["redaction_count"] = redactions[0]
    return validate_report(result)
