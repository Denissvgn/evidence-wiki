"""Private read-only evidence for an explicitly selected original review service."""

from __future__ import annotations

import re

from ._review_dependency_json import detached, digest
from .mutation_contracts import validate_request_id
from .pack_discovery import owner
from .research_journal_contracts import canonical_records, digest_record, digest_records
from .research_session_contracts import refuse

_COORDINATE = {"host_id", "case_id", "cycle", "claim_id", "config_ref"}


def _one(records, predicate):
    selected = [row for row in records if predicate(row)]
    if len(selected) != 1:
        refuse("RETAINED_REVIEW_PROOF_AMBIGUOUS")
    return selected[0]


def _reference(record):
    return digest_record({key: value for key, value in record.items() if key != "event_ref"})


def _selectors(session, supplied, coordinate):
    if (type(supplied) is not dict or set(supplied) != {"run_id", "record_sha256"}
            or type(coordinate) is not dict or set(coordinate) != _COORDINATE
            or any(type(value) is not str or not value or len(value) > 256 for value in coordinate.values())):
        refuse("RETAINED_REVIEW_SELECTOR_INVALID")
    if (validate_request_id(supplied["run_id"]) != session.run_id
            or type(supplied["record_sha256"]) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", supplied["record_sha256"]) is None):
        refuse("RETAINED_REVIEW_SELECTOR_INVALID")


def _journal_receipt(session, records, *, journal_collection=False):
    if type(journal_collection) is not bool:
        refuse("RETAINED_REVIEW_SELECTOR_INVALID")
    if journal_collection:
        from .research_journal_collections import collection_summary

        return {**collection_summary(records), "run_id": session.run_id, "binding_id": digest(session.binding),
            "head_ref": _reference(records[-1]) if records else None}
    raw = canonical_records(records)
    if len(raw) > 1_048_576:
        refuse("RETAINED_REVIEW_JOURNAL_BOUND")
    return {"run_id": session.run_id, "binding_id": digest(session.binding),
        "records_sha256": digest_records(records), "records_bytes": len(raw), "record_count": len(records),
        "head_ref": _reference(records[-1]) if records else None}


def _original(session, records, applied_ref, coordinate, *, journal_collection=False):
    applied = _one(records, lambda row: row["kind"] == "execution_applied" and _reference(row) == applied_ref)
    applied_data = applied["data"]
    if applied_data.get("operation") != "evaluate" or applied_data.get("claim_id") != coordinate["claim_id"]:
        refuse("RETAINED_REVIEW_COORDINATE_CHANGED")
    attempt_id = validate_request_id(applied_data["attempt_id"])
    saved = _one(records, lambda row: row["kind"] == "execution_attempt"
        and row["data"].get("attempt", {}).get("attempt_id") == attempt_id)
    attempt = session.execution.attempt(saved["data"]["attempt"])
    role = session.execution.profile.role(attempt.config_ref)
    scopes = session.execution.profile.role_scopes
    if scopes is None:
        refuse("RETAINED_REVIEW_ROLE_UNAVAILABLE")
    scope = scopes.scope(attempt.config_ref)
    if (attempt.config_ref != coordinate["config_ref"] or role.role != "evaluator"
            or scope.task_role not in {"evaluator", "critic"} or attempt.job.get("task_role") != scope.task_role
            or attempt.job.get("operation") != "evaluate" or attempt.run_id != session.run_id
            or attempt.binding_id != digest(session.binding)
            or saved["data"].get("claim_id") != coordinate["claim_id"]
            or applied_data.get("profile_ref") != attempt.config_ref):
        refuse("RETAINED_REVIEW_COORDINATE_CHANGED")
    observations = [row["data"]["observation"] for row in records if row["kind"] == "execution_observation"
        and row["data"].get("observation", {}).get("attempt_id") == attempt_id
        and row["data"]["observation"].get("final_accounting") is True]
    if len(observations) != 1:
        refuse("RETAINED_REVIEW_WORKER_PROOF_REQUIRED")
    observed = session.execution.authenticate(observations[0], attempt)
    from .execution_session import _model_review_observed

    if (not _model_review_observed(session, attempt, observed)
            or observed.get("state") != "succeeded" or observed.get("final_accounting") is not True
            or observed.get("config_ref") != attempt.config_ref or observed.get("controller_ref") != role.controller_ref):
        refuse("RETAINED_REVIEW_WORKER_PROOF_REQUIRED")
    application = _one(records, lambda row: row["kind"] == "execution_application" and row["data"].get("attempt_id") == attempt_id)
    data = application["data"]
    envelope = data.get("envelope")
    if (data.get("operation") != "evaluate" or data.get("claim_id") != coordinate["claim_id"]
            or data.get("profile_ref") != attempt.config_ref or type(envelope) is not dict
            or data.get("original_id") != saved["data"].get("original_id")
            or data.get("original_id") != applied_data.get("original_id")):
        refuse("RETAINED_REVIEW_APPLICATION_CHANGED")
    review = owner("_strict_contract").review_document(envelope["payload"]["body"]["review"])
    passed = all(verdict == "pass" for verdict in review["verdicts"].values())
    if (review["claim_id"] != coordinate["claim_id"] or data.get("passed") is not passed
            or applied_data.get("passed") is not passed or data.get("strict_basis_id") != review["basis_id"]
            or applied_data.get("strict_basis_id") != review["basis_id"]
            or observed.get("output", {}).get("verdicts") != review["verdicts"]):
        refuse("RETAINED_REVIEW_APPLICATION_CHANGED")
    native = _one(records, lambda row: row["kind"] == "execution_native"
        and row["data"].get("operation") == "strict.register_review"
        and row["data"].get("arguments") == {"envelope": envelope})
    native_id = native["data"]["reservation"]["attempt_id"]
    returned = _one(records, lambda row: row["kind"] == "execution_native_observation"
        and row["data"].get("observation", {}).get("attempt_id") == native_id)
    receipt = returned["data"].get("result")
    if (returned["data"].get("error_code") is not None or type(receipt) is not dict
            or receipt.get("operation") != "strict.register_review" or receipt.get("status") not in {"committed", "reconciled"}
            or not application["sequence"] < native["sequence"] < returned["sequence"] < applied["sequence"]):
        refuse("RETAINED_REVIEW_REGISTRATION_UNPROVEN")
    delivery = attempt.input_document.get("complete_review_delivery")
    if (type(delivery) is not dict or type(delivery.get("packet")) is not dict
            or delivery.get("packet_sha256") != digest(delivery["packet"])):
        refuse("RETAINED_REVIEW_DELIVERY_UNAVAILABLE")
    if review["schema_version"] == "evidence-strict-review/v3":
        link = _one(records, lambda row: row["kind"] == "execution_review_dependency_delivery"
            and row["data"].get("attempt_id") == attempt_id)
        opening = _one(records, lambda row: row["kind"] == "execution_review_dependency_capture"
            and _reference(row) == link["data"]["opening_record_ref"])
        closing = _one(records, lambda row: row["kind"] == "execution_review_dependency_capture"
            and row["data"].get("attempt_id") == attempt_id and row["data"].get("phase") == "admission_closing")
        custody = {"opening": opening, "closing": closing, "delivery_link": link}
    else:
        context = saved["data"].get("review_context")
        if type(context) is not dict:
            refuse("RETAINED_REVIEW_DELIVERY_UNAVAILABLE")
        custody = {"attempt_record": saved, "review_context": context, "delivery": delivery}
    original = {"run_id": session.run_id, "operation": data["operation"], "application_record_ref": _reference(application),
        "applied_record_ref": _reference(applied), "review_id": owner("_strict_contract").artifact_id(review),
        "authorship_id": owner("_evidence_revision").content_id("evidence-authenticated-payload/v1", review["generator"]["payload"]),
        "basis_id": review["basis_id"], "semantic_material_id": review.get("dependency_basis", {}).get("semantic_material_id"),
        "observed_workspace_basis_id": review.get("observed_workspace_basis_id", review["basis_id"]),
        "review_producer_id": review["observation"]["producer_id"] if review["schema_version"] == "evidence-strict-review/v3" else None,
        "passed": passed, "attempt_id": attempt_id, "config_ref": attempt.config_ref,
        "task_role": scope.task_role, "controller_ref": role.controller_ref, "envelope": detached(envelope),
        "attempt": detached(attempt.document()), "final_observation": detached(observed), "delivery": detached(delivery),
        "original_custody": custody, "journal_receipt": _journal_receipt(session, records, journal_collection=journal_collection)}
    return original, review, data



def _closing_native(session, native, original):
    """Reject a changed external ledger or elapsed live gate after qualification."""
    strict = owner("_strict_evidence")
    config = strict.configuration(session.root)
    policy = strict.resolve_policy(session.root, config)
    usage = owner("_evidence_usage")
    authority = owner("_evidence_authority")
    with usage.current_view(session.root, config) as view:
        observed = native["usage_authority"]
        if (observed is None or view.state.checkpoint != observed["checkpoint"]
                or authority.authority_basis(view.trust) != observed["authority"]):
            refuse("RETAINED_REVIEW_CUSTODY_CHANGED")
        capture = owner("_evidence_revision").capture_workspace(session.root)
        strict._dependency_inputs(session.root, config, capture, view)
        if native["state"] == "eligible":
            rows = {row["claim"]["id"]: row for row in native["strict"]["claims"]}
            pending, seen = [original["envelope"]["payload"]["body"]["review"]["claim_id"]], set()
            reviews = [original["envelope"]]
            while pending:
                identifier = pending.pop()
                if identifier in seen:
                    continue
                seen.add(identifier)
                row = rows[identifier]
                pending.extend(row["claim"]["premises"])
                selected = view.state.strict_reviews[row["review"]["review_id"]]
                reviews.append(selected["envelope"])
                for evidence in row["claim"]["evidence"]:
                    age = (view.now - authority.timestamp(evidence["observed_at"])).total_seconds()
                    if not 0 <= age <= policy["max_source_age_seconds"]:
                        refuse("RETAINED_REVIEW_CLOCK_CHANGED")
            for envelope in reviews:
                payload = envelope["payload"]["body"]["review"]
                role = rows[payload["claim_id"]]["required_review_role"]
                auth = authority.verify_attestation(envelope, role, view.trust, view.now)
                generator = authority.verify_attestation(payload["generator"], "generator", view.trust, view.now)
                age = (view.now - authority.timestamp(payload["reviewed_at"])).total_seconds()
                if (not auth.get("authenticated") or not generator.get("authenticated")
                        or auth.get("controller") == generator.get("controller")
                        or not 0 <= age <= policy["max_review_age_seconds"]):
                    refuse("RETAINED_REVIEW_CLOCK_CHANGED")
            if config.get("computation") is not None:
                computation = owner("_computation_service").required(session.root, config, view=view)
                if computation != original["envelope"]["payload"]["body"]["review"]["snapshot"]["computation"]:
                    refuse("RETAINED_REVIEW_CUSTODY_CHANGED")
        view.revalidate(session.root, config)


def observe(session, original_applied_ref, expected_coordinate, *, journal_collection=False):
    """Authenticate original joins and separately ask current native owners."""
    from . import execution_session as execution

    if type(journal_collection) is not bool:
        refuse("RETAINED_REVIEW_SELECTOR_INVALID")
    session._check("current")
    _selectors(session, original_applied_ref, expected_coordinate)
    revisions = owner("_evidence_revision")
    opening_revision = revisions.capture_workspace(session.root).revision_id
    records = execution.retained(session)
    original, review, application = _original(session, records, original_applied_ref["record_sha256"], expected_coordinate,
        **({"journal_collection": True} if journal_collection else {}))
    observed = None
    try:
        current, observed, packets, _records = execution._observe_current(session)
    except Exception:
        current = None
    native = owner("_strict_evidence").retained_review_eligibility(session.root, original["envelope"],
        claim_id=expected_coordinate["claim_id"], expected_review_id=original["review_id"])
    qualification = None
    try:
        if current is None or observed is None:
            refuse("RETAINED_REVIEW_QUALIFICATION_UNAVAILABLE")
        qualification = execution._qualifications(session, observed,
            originals=current["accounting"]["original_outcomes"], packets=packets, records=records)
        observed.finish()
        selected = [row for row in qualification["profiles"] if row.get("profile_ref") == original["config_ref"]]
        role_matches = (native["original_controller_ref"] == original["controller_ref"]
            and native["original_generator_controller_ref"] == session.execution.review_signer.generator.controller_ref)
        if (len(selected) != 1 or selected[0].get("qualified") is not True or selected[0].get("independent") is not True):
            native.update(state="unknown", reason="qualification_unavailable")
        elif native["state"] == "eligible" and not role_matches:
            native.update(state="ineligible", reason="role_or_controller_changed")
        if application["original_id"] not in {row["original_id"] for row in current["accounting"]["original_outcomes"]}:
            native.update(state="ineligible", reason="role_or_controller_changed")
        strict = native["strict"]
        if strict is not None:
            claim = next(row["claim"] for row in strict["claims"] if row["claim"]["id"] == expected_coordinate["claim_id"])
            question = next(row for row in strict["questions"] if row["slug"] == claim["question_slug"])
            if question["original_id"] != application["original_id"]:
                native.update(state="ineligible", reason="role_or_controller_changed")
    except Exception:
        native.update(state="unknown", reason="qualification_unavailable")
    if review["schema_version"] != "evidence-strict-review/v3":
        native.update(state="ineligible", reason="unsupported_contract")
    if native["usage_authority"] is not None:
        try:
            _closing_native(session, native, original)
        except Exception:
            native.update(state="unknown", reason="current_custody_changed")
    if execution.retained(session) != records:
        refuse("RETAINED_REVIEW_CUSTODY_CHANGED")
    session._check("current")
    if revisions.capture_workspace(session.root).revision_id != opening_revision:
        refuse("RETAINED_REVIEW_CUSTODY_CHANGED")
    return {key: native[key] for key in ("state", "reason", "current", "strict", "publication", "usage_authority", "custody")} | {
        "original": original, "qualification": qualification, "journal_receipt": _journal_receipt(session, records,
            **({"journal_collection": True} if journal_collection else {}))}
