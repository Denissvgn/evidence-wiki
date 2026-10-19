"""Dated native host observations, separate from model and kernel isolation."""

from __future__ import annotations

import hashlib
import platform
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from ._review_dependency_json import canonical, digest
from .agent_execution import BoundedSubprocessExecutor, ExecutionError, _utc
from .errors import ConfigError, EvidenceWikiError
from .research_journal_contracts import digest_record
from .research_session import ResearchSession

SCHEMA = "research-host-qualification/v1"
SCHEMA_V2 = "research-host-qualification/v2"
MAX_ATTEMPTS = 64


def _refuse(reason):
    raise ConfigError("HOST_QUALIFICATION_REFUSED", "Host qualification observation refused.",
        recoverable=False, details={"reason": reason})


def _boundary(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        from ._facades._base import _translated
        try:
            from ._review_dependency_inputs import bind_session
            with bind_session(args[0]) if type(args[0]) is ResearchSession else nullcontext():
                return function(*args, **kwargs)
        except EvidenceWikiError:
            raise
        except (Exception, SystemExit) as error:
            translated = _translated(error)
            if translated is not None:
                raise translated from error
            _refuse("native_observation_invalid")
    return guarded


@_boundary
def host_qualification(session):
    """Observe current native credentials and existing owned review receipts.

    This read-only operation challenges provisioned native credentials. It never
    dispatches a worker, probes a provider, supplies credentials or grants review
    eligibility. Distinct authenticated controllers remain a trusted parent
    boundary; their names and model references do not prove independent custody.
    """
    from . import execution_session as driver
    from .review_context import ReviewContext
    from .service_authentication import owned, status
    from .workspace import Workspace

    if type(session) is not ResearchSession or session.execution is None:
        _refuse("execution_session_required")
    session._check("current")
    session.context.require_grant("host_qualification.current")
    session.execution.check(session.binding)
    held_epoch = driver._CURRENT_FACTS.get() is not None
    before = driver._current_identity(session)
    records = session.execution.records(session)
    attempts = [row["data"] for row in records if row["kind"] == "execution_attempt"]
    if len(attempts) > MAX_ATTEMPTS:
        _refuse("attempt_inventory_bound_exceeded")
    credentials = status(session)
    configuration = driver.owner("_strict_evidence").configuration(session.root)
    sources = None
    observed = []
    report_schema = SCHEMA
    closing_reviews = []
    for retained in attempts:
        attempt = session.execution.attempt(retained["attempt"])
        role = session.execution.profile.role(attempt.config_ref)
        row = {"attempt_id": attempt.attempt_id, "config_ref": attempt.config_ref,
            "attempt_ref": digest(attempt.document()), "controller_ref": role.controller_ref,
            "model_ref": role.model_ref, "execution_state": "not_observed", "observation_ref": None,
            "context_state": "not_observed", "context_ref": None,
            "attestation_state": "not_observed", "attestation_ref": None,
            "attestation_principal": None, "attestation_key_id": None,
            "application_state": "not_observed", "context_reason": None,
            "lease_state": "expired" if _utc(attempt.job["deadline_utc"]) <= datetime.now(timezone.utc) else "current"}
        receipt = None
        prepared = None
        if any(item["kind"] == "execution_observation" and item["data"]["attempt_id"] == attempt.attempt_id for item in records):
            _retained, exact, receipt = owned(session, attempt.attempt_id)
            if exact.document() != attempt.document():
                _refuse("owned_attempt_changed")
            row.update(execution_state=receipt["state"], observation_ref=digest(receipt))
        if role.role == "evaluator" and "review_context" in retained:
            context = ReviewContext.restore(attempt.input_document["packet"], retained["review_context"])
            try:
                with Workspace.open(session.root) as workspace:
                    prepared = workspace.strict.prepare_review(retained["claim_id"])
            except EvidenceWikiError as error:
                if error.details.get("reason") != "strict_claim_unknown":
                    raise
                row.update(context_state="not_current", context_reason="retained_claim_not_current")
            if prepared is not None:
                if sources is None:
                    sources, _ = driver._host_sources(session)
                try:
                    driver._check_review_context(session, context, prepared, sources)
                    row.update(context_state="current_immutable_packet" if context.document()["manifest"]["context_complete"] else "incomplete")
                except ExecutionError:
                    row.update(context_state="not_current", context_reason="retained_context_does_not_match_current_evidence")
            row["context_ref"] = digest(context.binding())
        applications = [item["data"] for item in records if item["kind"] == "execution_application"
            and item["data"]["attempt_id"] == attempt.attempt_id and "envelope" in item["data"]]
        if applications:
            if len(applications) != 1 or role.role != "evaluator":
                _refuse("review_application_inventory_invalid")
            application = applications[0]
            envelope = application["envelope"]
            driver._verify_review_controller(session, attempt, envelope)
            identity = next((item for item in session.execution.review_signer.reviewers if item.config_ref == attempt.config_ref), None)
            if identity is None or any(envelope["authentication"].get(key) != getattr(identity, key) for key in ("principal", "key_id")):
                _refuse("attestation_selected_credential_mismatch")
            review = envelope["payload"]["body"]["review"]
            generator = session.execution.review_signer.generator
            authorship = review.get("generator", {})
            expected_authorship = {"schema_version": "evidence-strict-authorship/v1",
                "basis_id": review.get("basis_id"), "claim_id": review.get("claim_id")}
            selective = review.get("schema_version") == "evidence-strict-review/v3"
            if selective:
                report_schema = SCHEMA_V2
            if selective:
                expected_authorship.update(schema_version="evidence-strict-authorship/v2",
                    claim_schema_version=review["claim_schema_version"],
                    observed_workspace_basis_id=review["observed_workspace_basis_id"],
                    semantic_material_id=review["delivery"]["semantic_material_id"])
            if (identity.controller_ref == generator.controller_ref
                    or any(authorship.get("authentication", {}).get(key) != getattr(generator, key) for key in ("principal", "key_id"))
                    or authorship.get("payload") != expected_authorship):
                _refuse("attestation_generator_binding_mismatch")
            if (application.get("profile_ref") != attempt.config_ref or application.get("claim_id") != retained.get("claim_id")
                    or review.get("claim_id") != retained.get("claim_id") or application.get("strict_basis_id") != review.get("basis_id")
                    or receipt is None or receipt["state"] != "succeeded" or not receipt["final_accounting"]
                    or _utc(receipt["completed_utc"]) > _utc(attempt.job["deadline_utc"])):
                _refuse("attestation_attempt_binding_mismatch")
            if selective and prepared is not None:
                native = next((item for item in prepared["result"]["claims"] if item["claim"]["id"] == retained["claim_id"]), None)
                selected = native is not None and native["review"].get("review_id") == driver.owner("_strict_contract").artifact_id(review)
                if selected:
                    from ._review_dependency_custody import resolve_original
                    proof = resolve_original(review, purpose="reuse")
                    proof.assert_review_envelope(envelope)
                    if (review["basis_id"] != native["claim_basis_id"] or review["observation"] != native["observation"]
                            or not 0 <= (datetime.now(timezone.utc) - _utc(review["reviewed_at"])).total_seconds() <= prepared["snapshot"]["policy"]["max_review_age_seconds"]
                            or _utc(review["reviewed_at"]) > _utc(envelope["authentication"]["issued_at"])):
                        _refuse("attestation_current_context_mismatch")
                    row.update(context_state="current_selective_semantics", context_reason=None,
                        attestation_state="current_bound_review")
                else:
                    row["attestation_state"] = "native_controller_verified"
            elif prepared is not None and row["context_state"] == "current_immutable_packet":
                native = next((item for item in prepared["result"]["claims"] if item["claim"]["id"] == retained["claim_id"]), None)
                if (review.get("snapshot") != prepared["snapshot"] or review.get("basis_id") != prepared["result"]["basis_id"]
                        or native is None or review.get("observation") != native["observation"]
                        or not 0 <= (datetime.now(timezone.utc) - _utc(review["reviewed_at"])).total_seconds() <= prepared["snapshot"]["policy"]["max_review_age_seconds"]
                        or _utc(review["reviewed_at"]) > _utc(envelope["authentication"]["issued_at"])
                        or any(envelope["payload"].get(key) != prepared["registration"].get(key)
                            for key in ("schema_version", "state_id", "workspace_binding", "action"))):
                    _refuse("attestation_current_context_mismatch")
                row["attestation_state"] = "current_bound_review"
                if held_epoch:
                    closing_reviews.append((attempt, envelope, prepared["snapshot"]))
            else:
                row["attestation_state"] = "native_controller_verified"
            row.update(attestation_ref=digest(envelope), attestation_principal=identity.principal,
                attestation_key_id=identity.key_id, application_state="signed")
            applied = [item["data"] for item in records if item["kind"] == "execution_applied"
                and item["data"]["attempt_id"] == attempt.attempt_id and item["data"]["operation"] == "evaluate"]
            if applied:
                if len(applied) != 1 or any(applied[0].get(key) != application.get(key) for key in ("claim_id", "strict_basis_id")):
                    _refuse("registered_review_binding_mismatch")
                row["application_state"] = "registered"
        observed.append(row)
    if closing_reviews:
        current_claims = {}
        with Workspace.open(session.root) as workspace:
            for attempt, envelope, snapshot in closing_reviews:
                driver._verify_review_controller(session, attempt, envelope)
                claim_id = snapshot["claim"]["id"]
                if claim_id not in current_claims:
                    current_claims[claim_id] = workspace.strict.prepare_review(claim_id)
                closing = current_claims[claim_id]
                claim = next((item for item in closing["result"]["claims"] if item["claim"]["id"] == claim_id), None)
                if closing["snapshot"] != snapshot or claim is None or claim["accepted"] is not True:
                    _refuse("native_current_review_changed")
    session._check("current")
    session.execution.check(session.binding)
    after_records = session.execution.records(session)
    after = driver._current_identity(session)
    if driver.owner("_strict_evidence").configuration(session.root) != configuration:
        _refuse("native_configuration_changed")
    if credentials["authority_id"] is not None:
        authority = driver.owner("_evidence_authority")
        trust = authority.load_trust(session.root, configuration, datetime.now(timezone.utc))
        if authority.authority_basis(trust) != credentials["authority_id"]:
            _refuse("native_authority_changed")
    if (before["basis_id"] != after["basis_id"] or records != after_records
            or held_epoch and before["qualification"] != after["qualification"]):
        _refuse("observation_changed")
    native_reviewers = [row for row in observed if row["attestation_state"] == "current_bound_review"]
    controller_refs = sorted({row["controller_ref"] for row in native_reviewers})
    principal_refs = sorted({row["attestation_principal"] for row in native_reviewers})
    service_slots = {row["config_ref"]: row["slot_ref"] for row in credentials["services"]
        if row["credential_state"] == "verified" and row["qualification_state"] == "qualified"}
    represented = {service_slots.get(row["config_ref"]) for row in native_reviewers}
    required = {row["profile_ref"] for row in session.policy.to_dict()["required_evaluators"]}
    native_observed = bool(required and required <= represented and credentials["state"] == "ready")
    package = Path(__file__).resolve().parent
    generation = session.binding["owner_generation"]
    result = {"schema_version": report_schema, "observed_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": session.run_id, "binding_id": digest(session.binding), "basis_id": after["basis_id"],
        "policy_id": session.policy.policy_id, "execution_profile_sha256": session.execution.profile.sha256,
        "registry_sha256": session.execution.qualifications.sha256, "journal_head_ref": digest_record(records[-1]) if records else None,
        "runtime": {"os": sys.platform, "os_release": platform.release(), "machine": platform.machine(),
            "python_implementation": platform.python_implementation(), "python_version": platform.python_version(),
            "package_source_sha256": generation["package_sha256"], "native_script_sha256": generation["script_sha256"],
            "package_location_sha256": digest(str(package)),
            "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()},
        "credentials": credentials,
        "attempts": observed,
        "control": {"authenticated_review_controller_refs": controller_refs,
            "authenticated_review_principals": principal_refs,
            "distinct_native_review_controllers": len(controller_refs) > 1 if native_reviewers else None,
            "independent_credential_custody": "not_observed", "isolated_reasoning": "not_observed"},
        "capability_matrix": [
            {"boundary": "artifact_checked", "state": "unsupported" if session.policy.to_dict()["assurance"] != "artifact_checked" else "observed" if native_observed else "not_observed",
                "scope": "current native credentials and retained authenticated attempts, contexts and attestations only",
                "executor": "trusted_bounded_subprocess" if type(session.execution.executor) is BoundedSubprocessExecutor else "configured_host_service",
                "semantic_eligibility": after["qualification"]["state"]},
            {"boundary": "host_enforced", "state": "unsupported", "scope": "semantic execution and delivery",
                "reason": "separately_qualified_semantic_host_boundary_unavailable"},
            {"boundary": "remote_model", "state": "not_observed", "scope": "provider execution, model families and remote credential custody"}],
        "limitations": ["Existing receipts are verified; this observation dispatches no evaluator or provider probe.",
            "Native parent credential separation is not OS or independent service isolation.",
            "Kernel draft confinement requires a separate actual protected-host observation.",
            "This dated snapshot grants no permission and does not establish general model reliability."],
        "authorized": False, "evidence_accepted": False}
    if len(canonical(result)) > 262144:
        _refuse("report_bound_exceeded")
    from .host_qualification_contracts import validate
    return validate(report_schema, result)
