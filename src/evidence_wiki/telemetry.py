"""Bounded retained-journal telemetry; declarations never confer authority."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from ._facades._base import _process_exit, _translated, call_seam
from ._pack_io import canonical, identity
from .errors import EvidenceWikiError, UsageError
from .host_context import checked_operation, current_context
from .mutation_store import MutationStore
from .pack_discovery import owner
from .telemetry_contracts import EVENT, JOURNAL, PROJECTION, REPORT, VERIFICATION, metric_binding, validate_document


def refuse(reason):
    raise UsageError("TELEMETRY_" + reason, "Telemetry operation refused.", recoverable=False,
        details={"reason": reason}, remediation="Use bounded canonical events and explicit current host scope; preserve uncertain records.") from None


def _contained(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SystemExit as error:
            raise _process_exit(error) from None
        except EvidenceWikiError:
            raise
        except Exception as error:
            try:
                translated = _translated(error)
            except SystemExit as renderer_exit:
                raise _process_exit(renderer_exit) from None
            except Exception:
                raise error from None
            if translated is not None:
                raise translated from None
            raise
    return invoke


def _sha(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            refuse("CLOCK_INVALID")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError):
        refuse("CLOCK_INVALID")


def _scaled(total, places):
    if not places:
        return str(total)
    digits = str(total).zfill(places + 1)
    fraction = digits[-places:].rstrip("0")
    return digits[:-places] + ("." + fraction if fraction else "")


def _sum_values(values):
    parts = [value.split(".") for value in values]
    places = max((len(item[1]) if len(item) == 2 else 0 for item in parts), default=0)
    total = sum(int("".join(item)) * 10 ** (places - (len(item[1]) if len(item) == 2 else 0)) for item in parts)
    return _scaled(total, places)


def _scope(root, operation):
    context = current_context()
    if context is None:
        refuse("HOST_REQUIRED")
    context.require_grant("telemetry." + operation)
    return context.require_path(Path(root).expanduser().absolute()), context


@dataclass(frozen=True)
class MeasurementVerifier:
    """A trusted host callback that authenticates event measurement bindings.

    The host supplies this object outside worker JSON. Its reference is a
    correlation value; neither construction nor the output codec authenticates
    a principal. The callback must verify the actual measurement producer.
    """

    controller_ref: str
    verify: object = field(repr=False, compare=False)

    def __post_init__(self):
        if type(self.controller_ref) is not str or not 1 <= len(self.controller_ref) <= 256 or not callable(self.verify):
            refuse("VERIFIER_INVALID")


@_contained
def record(root, *, run_id, agent_id, event):
    """Record one exact logical event through the canonical run journal."""
    root, context = _scope(root, "record")
    if agent_id != context.actor_ref:
        refuse("ACTOR_CONFLICT")
    value = validate_document(EVENT, event)
    if value["agent_id"] != agent_id or value["run_id"] != run_id:
        refuse("EVENT_CONFLICT")
    context.require_grant("run_controller.run_telemetry_record")
    context.require_path(root, write=True)
    if context.mutation_control_directory is None:
        refuse("CONTROL_REQUIRED")
    with MutationStore(root, context.mutation_control_directory).workspace_locked():
        return call_seam(owner, "run_controller", "run_telemetry_record", root,
                         run_id=run_id, agent_id=agent_id, event=value)


def _verified(root, event_ref, outer, event, verifier):
    if verifier is None or verifier.controller_ref != event["controller_ref"]:
        return None
    request = {"event_ref": event_ref, "event_sha256": _sha(outer), "event": event,
        "workspace_binding": {"path": str(root), **identity(root)}}
    try:
        value = validate_document(VERIFICATION, verifier.verify(validate_detached(request)))
        now = datetime.now(timezone.utc)
        matched = (value["event_sha256"] == request["event_sha256"] and value["workspace_binding"] == request["workspace_binding"]
            and value["run_id"] == event["run_id"] and value["agent_id"] == event["agent_id"]
            and value["controller_ref"] == event["controller_ref"]
            and sorted(value["metric_ids"]) == sorted(metric["logical_id"] for metric in event["metrics"])
            and _time(value["verified_at"]) <= now < _time(value["expires_at"]))
        return _time(value["expires_at"]) if matched else None
    except (Exception, SystemExit):
        return None


def validate_detached(value):
    from .mutation_contracts import detached
    return detached(value)


def _coverage(events, expected, unknown):
    observed, complete, started = set(), set(), set()
    for _ref, event, _authenticated in events:
        attempt = event["attempt_id"]
        if attempt is None:
            continue
        observed.add(attempt)
        if event["attempt_status"] in {"completed", "failed"}:
            complete.add(attempt)
        if event["attempt_status"] in {"started", "incomplete"}:
            started.add(attempt)
    observed_jobs = {event["job_id"] for _ref, event, _authenticated in events if event["job_id"] is not None}
    return {"scope": "retained_journal", "expected_attempt_ids": sorted(observed), "observed_attempt_ids": sorted(observed),
        "missing_attempt_ids": [], "incomplete_attempt_ids": sorted((observed | started) - complete),
        "expected_job_ids": sorted(expected), "observed_job_ids": sorted(observed_jobs), "missing_job_ids": sorted(expected - observed_jobs),
        "whole_execution_authenticated": False, "unknown_event_count": unknown}


def _metrics(events, coverage):
    logical = {}
    for _ref, event, authenticated in events:
        for metric in event["metrics"]:
            key = metric["logical_id"]
            binding = metric_binding(event, metric)
            previous = logical.get(key)
            if previous is not None:
                if previous[0] != binding:
                    refuse("METRIC_CONFLICT")
                logical[key] = (binding, previous[1] or authenticated)
            else:
                logical[key] = (binding, authenticated)
    groups = {}
    for binding, authenticated in logical.values():
        metric = binding["metric"]
        group = tuple(metric[name] for name in ("name", "unit", "currency", "provenance", "trust_realm")) + (
            canonical(metric["pricing_ref"]), authenticated)
        groups.setdefault(group, []).append(binding)
    result = []
    for group, entries in sorted(groups.items(), key=lambda row: repr(row[0])):
        name, unit, currency, provenance, realm, _pricing, authenticated = group
        known = [row["metric"]["value"] for row in entries if row["metric"]["value"] is not None]
        subtotal = _sum_values(known) if known else None
        unknown = len(entries) - len(known)
        total = None
        if name == "human_interventions" and subtotal == "0":
            subtotal = total = None
        result.append({"name": name, "unit": unit, "currency": currency, "pricing_ref": entries[0]["metric"]["pricing_ref"],
            "provenance": provenance, "trust_realm": realm, "authenticated": authenticated, "known_subtotal": subtotal,
            "total": total, "observations": len(entries), "unknown": unknown,
            "logical_ids": sorted(row["metric"]["logical_id"] for row in entries)})
    return result


def _spans(events, coverage):
    groups = {}
    for reference, event, authenticated in events:
        span = event["span"]
        if span is not None:
            key = (span["kind"], event["controller_ref"], span["clock_ref"], authenticated)
            groups.setdefault(key, []).append((reference, event["attempt_id"], span))
    result = []
    for (kind, controller, clock, authenticated), rows in sorted(groups.items()):
        intervals, incomplete = [], 0
        for _reference, _attempt, span in rows:
            if span["ended_at"] is None:
                incomplete += 1
            else:
                intervals.append((_time(span["started_at"]), _time(span["ended_at"])))
        intervals.sort()
        overlap = any(right[0] < left[1] for left, right in zip(intervals, intervals[1:], strict=False))
        def duration(start, end):
            elapsed = end - start
            return elapsed.days * 86400000000 + elapsed.seconds * 1000000 + elapsed.microseconds
        milliseconds = _scaled(sum(duration(start, end) for start, end in intervals), 3)
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        union = _scaled(sum(duration(start, end) for start, end in merged), 3)
        result.append({"kind": kind, "controller_ref": controller, "clock_ref": clock, "authenticated": authenticated,
            "interval_sum_milliseconds": milliseconds, "union_milliseconds": union, "duration_basis": "declared_utc", "total_milliseconds": None,
            "overlap": overlap, "incomplete": incomplete, "event_refs": sorted(reference for reference, _attempt, _span in rows)})
    return result


def _pending_state(root, run_id):
    with checked_operation("run_controller", "run_telemetry_events", (root,), {"run_id": run_id}) as revalidate:
        try:
            state = owner("run_controller")._session_state(root, run_id)
        except Exception as error:
            if getattr(error, "error_code", None) == "RUN_STATE_INVALID":
                refuse("JOB_CONFLICT")
            raise
        revalidate()
        return state


@_contained
def report(root, *, run_id, verifier=None):
    """Reconstruct decisions and declared measurements from one captured journal."""
    root, _context = _scope(root, "report")
    if verifier is not None and type(verifier) is not MeasurementVerifier:
        refuse("VERIFIER_INVALID")
    snapshot = validate_document(JOURNAL, call_seam(owner, "run_controller", "run_telemetry_events", root, run_id=run_id))
    events, trace, decisions, expected, jobs, logical, unknown = [], [], [], set(), {}, {}, 0
    pending_work = []
    for outer in snapshot["events"]:
        reference = outer["event_id"]
        data = outer.get("data") or {}
        if outer.get("run_id") != run_id:
            refuse("EVENT_CONFLICT")
        if outer["event_type"] == "custom.research.telemetry":
            event = validate_document(EVENT, data["event"])
            if event["run_id"] != run_id or event["agent_id"] != outer["agent_id"] or data.get("logical_id") != event["logical_id"]:
                refuse("EVENT_CONFLICT")
            previous = logical.get(event["logical_id"])
            if previous is not None:
                if previous != event:
                    refuse("EVENT_CONFLICT")
                continue
            logical[event["logical_id"]] = event
            events.append((reference, event, outer))
            trace.append({"event_ref": reference, "event_sha256": _sha(event), "outer_event_sha256": _sha(outer), "event": event})
        elif outer["event_type"] == "custom.research.decision":
            record = data.get("record")
            if type(record) is not dict:
                refuse("DECISION_INVALID")
            if record.get("kind") == "host_job":
                from .workflow_contracts import JOB
                from .workflow_contracts import validate_document as job_document
                job = job_document(JOB, record["job"])
                expected.add(job["id"])
                if job["id"] in jobs and jobs[job["id"]] != job:
                    refuse("JOB_CONFLICT")
                jobs[job["id"]] = job
            else:
                from .research_session_contracts import STEP, checked
                retained = checked(STEP, record)
                job = retained["job"]
                if job is not None:
                    expected.add(job["id"])
                    if job["id"] in jobs and jobs[job["id"]] != job:
                        refuse("JOB_CONFLICT")
                    jobs[job["id"]] = job
                decisions.append({"event_ref": reference, "request_id": retained["request_id"], "decision": retained["decision"],
                    "job_id": job["id"] if job is not None else None, "job_operation": job["operation"] if job is not None else None})
        elif outer["event_type"] == "custom.research.pending":
            from .pending_work_contracts import PENDING
            from .pending_work_contracts import validate_document as pending_document
            record = data.get("record")
            if (type(record) is not dict or set(record) != {"kind", "operation", "receipt", "authorized", "evidence_accepted"}
                    or record["kind"] != "pending_work" or record["operation"] not in {"reconcile", "cancel"}
                    or record["authorized"] is not False or record["evidence_accepted"] is not False):
                refuse("JOB_CONFLICT")
            pending_work.append(pending_document(PENDING, record["receipt"]))
        elif outer["event_type"].startswith("custom."):
            unknown += 1
    from .workflow_contracts import JOB, document_sha256
    pending_state = _pending_state(root, run_id) if pending_work else None
    if pending_state is not None:
        from .research_session_contracts import BINDING, checked
        if (type(pending_state) is not dict or type(pending_state.get("research_session")) is not dict
                or type(pending_state.get("run_id")) is not str or type(pending_state.get("started_at")) is not str):
            refuse("JOB_CONFLICT")
        try:
            binding = checked(BINDING, pending_state["research_session"])
            run_deadline = _time(pending_state["started_at"]) + timedelta(seconds=binding["policy"]["budgets"]["seconds"])
        except (EvidenceWikiError, KeyError, TypeError, ValueError, OverflowError):
            refuse("JOB_CONFLICT")
        if pending_state["run_id"] != run_id or binding["run_id"] != run_id:
            refuse("JOB_CONFLICT")
        previous_pending = {}
        from .pending_work_contracts import create, validate_transition
        for receipt in pending_work:
            job = jobs.get(receipt["job_id"])
            if (job is None or receipt["job_sha256"] != _sha(job) or receipt["run_id"] != run_id
                    or receipt["request_id"] != job["id"] or receipt["state"] not in {"expired", "unknown"}
                    or receipt["binding_id"] != _sha(binding) or _time(receipt["issued_utc"]) != _time(job["issued_utc"])
                    or _time(receipt["job_deadline_utc"]) != _time(job["deadline_utc"])
                    or _time(receipt["run_deadline_utc"]) != run_deadline
                    or _time(receipt["effective_deadline_utc"]) != min(run_deadline, _time(job["deadline_utc"]))):
                refuse("JOB_CONFLICT")
            previous = previous_pending.get(receipt["job_id"])
            if previous is None:
                previous = create(job, run_id=run_id, binding_id=_sha(binding), request_id=job["id"],
                    run_deadline_utc=run_deadline.isoformat(), observed_utc=job["issued_utc"])
            try:
                validate_transition(previous, receipt)
            except EvidenceWikiError:
                refuse("JOB_CONFLICT")
            previous_pending[receipt["job_id"]] = receipt
    for _reference, event, _outer in events:
        if event["job_id"] is None:
            continue
        job = jobs.get(event["job_id"])
        if (job is None or event["job_sha256"] != document_sha256(JOB, job)
                or event["request_id"] != job["id"] or event["controller_ref"] != job["controller_ref"]
                or event["role"] != job["role"] or event["policy_id"] != job["policy_id"]
                or event["basis_id"] != job["basis_id"] or event["prompt"] is None
                or event["prompt"]["version"] != job["prompt_version"]):
            refuse("JOB_CONFLICT")
    verified = [(reference, event, _verified(root, reference, outer, event, verifier)) for reference, event, outer in events]
    if current_context() is not _context:
        refuse("HOST_SELECTION_CHANGED")
    root = _context.require_path(root)
    now = datetime.now(timezone.utc)
    events = [(reference, event, expires is not None and now < expires) for reference, event, expires in verified]
    if pending_state is not None and _pending_state(root, run_id) != pending_state:
        refuse("JOB_CONFLICT")
    coverage = _coverage(events, expected, unknown)
    return validate_document(REPORT, {"schema_version": REPORT, "run_id": run_id, "journal_sha256": snapshot["sha256"],
        "event_count": snapshot["count"], "logical_event_count": len(events), "trace": trace, "decisions": decisions,
        "metrics": _metrics(events, coverage), "spans": _spans(events, coverage), "coverage": coverage,
        "observed_human_interventions": None, "limitations": ["Measurements are declarations unless verified by an explicitly selected host verifier.",
            "Coverage describes retained journal attempts; complete execution and whole-run human intervention remain unknown."]
            + (["Pending-work receipts record control state; they establish no executed attempt, measured usage or complete coverage."] if pending_work else []),
        "authorized": False, "evidence_accepted": False})


_SECRET = re.compile(r"(?i)(?:bearer\s+\S+|(?:sk|api)[-_][A-Za-z0-9_-]{12,}|(?:password|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*\S+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|https?://[^/\s]*:[^/@\s]+@[^\s]+)")


@_contained
def project(value):
    """Freeze a redacted public projection; no callback or destination is selected."""
    value = validate_document(REPORT, value)
    redactions = 0
    def clean(item):
        nonlocal redactions
        if type(item) is str:
            changed, count = _SECRET.subn("redacted", item)
            if item.startswith(("/", "~", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", item):
                changed, count = "redacted", count + 1
            redactions += count
            return changed
        if type(item) is list:
            return [clean(child) for child in item]
        if type(item) is dict:
            return {key: clean(child) for key, child in item.items()}
        return item
    redacted = clean(value)
    for trace in redacted["trace"]:
        trace["event_sha256"] = _sha(trace["event"])
    return validate_document(PROJECTION, {"schema_version": PROJECTION, "run_id": redacted["run_id"],
        "report_sha256": _sha(redacted), "original_report_sha256": _sha(value), "redaction_count": redactions, "report": redacted,
        "measurement_authentication": "not_reverified", "report_is_declared": True,
        "authorized": False, "evidence_accepted": False})
