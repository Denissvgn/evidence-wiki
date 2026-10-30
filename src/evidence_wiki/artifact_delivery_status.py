"""Historical delivery completion never substitutes for current eligibility."""

from datetime import datetime, timezone

from . import execution_accounting as accounting
from . import execution_session as native
from .artifact_delivery_contracts import RESULT, checked, refuse
from .artifact_publication import retained_writer
from .errors import EvidenceWikiError
from .mutation_contracts import detached, digest, validate_request_id
from .mutation_store import MutationStore


def completion(records, request_id):
    """Select a settled delivery observation from the authenticated journal."""
    intent = next((row["data"] for row in records if row["kind"] == "execution_native"
        and row["data"]["reservation"]["attempt_id"] == request_id), None)
    if intent is None or intent["operation"] != "artifact.deliver":
        return None
    candidates = [row["data"] for row in records if row["kind"] == "execution_native_observation"
        and row["data"]["observation"]["attempt_id"] == request_id]
    latest = max(candidates, key=lambda row: row["observation"]["sequence"], default=None)
    if (latest is None or not latest["observation"]["final_usage"] or latest["error_code"] is not None
            or type(latest["result"]) is not dict or latest["result"].get("status") not in {"committed", "reconciled"}
            or latest["result"].get("operation") != "artifact.deliver"
            or latest["result"].get("request_id") != request_id):
        return None
    return latest


def _selected(session, request_id, operation):
    request_id = validate_request_id(request_id)
    session._check(operation, write=operation == "reconcile_delivery")
    session.context.require_grant("artifact_delivery_operations." + operation)
    if session.execution is None or session.execution.profile.artifact_delivery is None:
        refuse("delivery_plan_required")
    records = native.retained(session)
    intent = next((row["data"] for row in records if row["kind"] == "execution_native"
        and row["data"]["reservation"]["attempt_id"] == request_id), None)
    if intent is None:
        refuse("delivery_request_unknown")
    if intent["operation"] != "artifact.deliver" or intent["arguments"]["plan_sha256"] != session.execution.profile.artifact_delivery.sha256:
        refuse("delivery_request_conflict")
    return request_id, records, intent


def _receipt(session, request_id, intent):
    """Read fixed protected custody, never generic outward transport inspection."""
    control = session.context.mutation_control_directory
    if control is None:
        refuse("delivery_control_unavailable")
    store = MutationStore(session.root, control)
    with store.historical_locked(request_id):
        saved = store._read("intent.json")
        if saved is None:
            return None
        if (saved["operation"] != "artifact.deliver" or saved["arguments"] != intent["arguments"]
                or saved["request_id"] != request_id or saved["workspace_binding"] != session.binding["workspace_binding"]
                or saved["host_context_sha256"] != session.binding["host_context_sha256"]
                or saved["owner_generation"] != session.binding["owner_generation"]):
            refuse("delivery_owner_receipt_conflict")
        receipt = store.receipt()
        if receipt is None or receipt["status"] not in {"committed", "reconciled"}:
            return None
        result = receipt["result"]
        proposed = intent["arguments"]["proposal"]
        destination = session.execution.profile.artifact_delivery.destination(proposed["destination_ref"])
        if (type(result) is not dict or result.get("schema_version") != "research-artifact-publication-return/v1"
                or result.get("request_id") != request_id
                or result.get("path") != "runs/" + session.run_id + "/deliveries/" + destination.filename
                or result.get("artifact_sha256") != proposed["artifact_sha256"]
                or result.get("bytes") != proposed["artifact_bytes"]
                or result.get("destination_ref") != proposed["destination_ref"]
                or result.get("boundary") != "artifact_checked" or result.get("evidence_accepted") is not False):
            refuse("delivery_owner_result_conflict")
        return detached(receipt)


def _result(request_id, *, status="commit_unknown", artifact=None, proposal=None, receipt=None, eligible="unavailable", reasons=None):
    return checked(RESULT, {"schema_version": RESULT, "request_id": request_id, "status": status,
        "artifact": artifact, "destination_ref": proposal["destination_ref"] if artifact is not None else None,
        "receipt_ref": digest(receipt) if artifact is not None else None,
        "boundary": "artifact_checked" if artifact is not None else "not_observed", "current_eligibility": eligible,
        "historical": True, "reasons": reasons or [], "authorized": False, "evidence_accepted": False})


def _current_result(session, request_id, intent, receipt):
    if receipt is None:
        return _result(request_id, reasons=["publication_uncertain_no_redispatch"])
    try:
        selected = session.prepare_delivery()
    except EvidenceWikiError:
        return _result(request_id, status="unavailable", reasons=["current_artifact_eligibility_unavailable"])
    proposed = intent["arguments"]["proposal"]
    artifact = selected["artifact"]
    if (artifact["artifact_id"] != proposed["artifact_id"] or artifact["artifact_sha256"] != proposed["artifact_sha256"]
            or artifact["bytes"] != proposed["artifact_bytes"]):
        return _result(request_id, status="unavailable", reasons=["current_artifact_changed"])
    # A terminal annotation can change deterministic Markdown without changing
    # the prior artifact. That history never becomes a new approval.
    return _result(request_id, status="committed", artifact=artifact, proposal=proposed, receipt=receipt,
        eligible="not_evaluated", reasons=["historical_delivery_current_identity_matches"])


def status(session, *, request_id):
    request_id, records, intent = _selected(session, request_id, "delivery_status")
    receipt = _receipt(session, request_id, intent)
    if native.retained(session) != records:
        refuse("delivery_history_changed")
    session._check("delivery_status")
    # Current source closing must be the final guarded read before outward
    # metadata, rather than preceding additional journal or binding work.
    return _current_result(session, request_id, intent, receipt)


def reconcile(session, *, request_id):
    request_id, _records, intent = _selected(session, request_id, "reconcile_delivery")
    receipt = _receipt(session, request_id, intent)
    if receipt is None:
        return _result(request_id, reasons=["publication_uncertain_no_redispatch"])
    with native.gate(session):
        records = native.retained(session)
        ledger = session.execution.ledger(session, records)
        if ledger["closure"] is not None:
            return _result(request_id, status="unavailable", reasons=["terminal_accounting_immutable"])
        earlier = [row["data"]["observation"] for row in records if row["kind"] == "execution_native_observation"
            and row["data"]["observation"]["attempt_id"] == request_id]
        if not any(row["final_usage"] for row in earlier):
            sequence = max([row["sequence"] for row in earlier], default=0) + 1
            issued = datetime.fromisoformat(intent["reservation"]["issued_at"].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - issued).total_seconds()
            if elapsed < 0:
                refuse("delivery_clock_regressed")
            observation = session.execution.native_observation(session, request_id, seconds=elapsed, succeeded=True)
            observation.update(observation_id=request_id + ".receipt-recovery." + str(sequence), sequence=sequence,
                correction=bool(earlier), authentication=None)
            observation = session.execution.sign(observation)
            accounting.observe_attempt(ledger, observation, session.execution.verifier())
            native.append(session, "execution_native_observation", native.child_id(request_id, "receipt-recovery." + str(sequence)),
                {"observation": observation, "result": receipt, "error_code": None, "owner_reason": None,
                    "elapsed_scope": "utc_lifetime_including_recovery_wait", "lifetime_seconds": format(elapsed, ".9f"),
                    "invocation_proof": "protected_committed_owner_receipt", "active_execution_seconds": None,
                    "after_input_basis": None})
    writer = retained_writer(session.context.mutation_control_directory.resolve(strict=True), request_id, receipt["input_sha256"])
    if writer is not None:
        writer.close_committed()
    return _current_result(session, request_id, intent, receipt)
