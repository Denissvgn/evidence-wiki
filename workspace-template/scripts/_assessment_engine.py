#!/usr/bin/env python3
"""Prepare and consume authenticated research assessments without external actions."""

from __future__ import annotations

import copy

from _assessment_contract import CAPABILITIES, SCHEMA, assessment_document, detached, request_document
from _evidence_authority import EvidenceInvalid, timestamp
from _evidence_revision import capture_workspace, content_id
from _evidence_usage import authenticated, current_view, validate_command, workspace_binding
from _script_errors import is_refusal
from _selected_publication import producer_identity, run_selected_publication
from _snapshot_verifier import SnapshotInvalid
from _temporal_contract import require, source_times
from _temporal_replay import Selection


def configuration_id(config):
    return content_id("evidence-assessment-configuration/v1", config)


def current_history(view, inputs):
    """A withdrawn or expired correction never revives its predecessor."""
    sources = {view.state.revisions[revision]["source_id"] for revision in inputs["ancestors"]}
    candidates = {revision: record for revision, record in view.state.revisions.items() if record["source_id"] in sources}
    require(len(candidates) <= 64, "assessment_history_bound_exceeded")
    active, predecessors = {}, set()
    ordered = list(candidates)
    for revision, record in candidates.items():
        times = source_times(record)
        require(times["available"] is not None, "assessment_source_history_incomplete")
        if times["available"] > view.now:
            continue
        active.setdefault(record["source_id"], set()).add(revision)
        prior = times["claims"]["supersedes"]
        if prior is not None:
            require(prior in candidates and candidates[prior]["source_id"] == record["source_id"]
                    and ordered.index(prior) < ordered.index(revision), "assessment_correction_chain_invalid")
            prior_time = source_times(candidates[prior])["available"]
            require(prior_time is not None and prior_time <= times["available"], "assessment_correction_order_invalid")
            predecessors.add(prior)
    for revision in inputs["ancestors"]:
        require(revision not in predecessors, "assessment_source_superseded")
        tips = active.get(view.state.revisions[revision]["source_id"], set()) - predecessors
        require(tips == {revision}, "assessment_source_history_ambiguous")


def stable_publication(publication):
    """Exclude only the three presentation clocks from independent proof comparison."""
    result = copy.deepcopy(publication)
    for document in (result["export"], result["readiness"], result["readiness"]["workspace_status"]):
        timestamp(document.pop("generated_at"))
    return result


def unavailable_reason(exc):
    if isinstance(exc, (EvidenceInvalid, SnapshotInvalid)):
        return str(exc)
    if is_refusal(exc):
        return "assessment_current_publication_unavailable"
    if isinstance(exc, (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError)):
        return "assessment_current_input_invalid"
    raise exc


def qualifications(view, request, inputs, evaluated_at):
    """Keep current rights, historical cutoffs and source validity independent."""
    gaps = set()
    limits = [timestamp(view.trust["policy"]["expires_at"])]
    if request["expires_at"] is not None:
        limits.append(timestamp(request["expires_at"]))
    mode = request["temporal"]["mode"]
    cutoff = evaluated_at if mode == "current" else timestamp(request["temporal"]["cutoff"])
    require(cutoff <= evaluated_at <= view.now, "assessment_evaluation_clock_invalid")
    if mode != "current":
        gaps.add("assessment_replay_only")
    else:
        try:
            current_history(view, inputs)
        except EvidenceInvalid as exc:
            gaps.add(str(exc))
    selection = Selection(view.state, view, {"mode": mode, "purpose": request["purpose"], "consumer": request["consumer"]}, cutoff)
    unknown_expiry = False
    for revision in inputs["ancestors"]:
        record = view.state.revisions.get(revision)
        require(record is not None, "assessment_dependency_missing")
        try:
            selection.inspect(revision)
        except (EvidenceInvalid, SnapshotInvalid) as exc:
            gaps.add(str(exc))
        try:
            times = source_times(record)
            if times["expires"] is None:
                unknown_expiry = True
                gaps.add("assessment_input_validity_unknown")
            else:
                limits.append(times["expires"])
            if times["effective"] is not None and times["effective"][1] is not None:
                limits.append(times["effective"][1])
        except EvidenceInvalid as exc:
            unknown_expiry = True
            gaps.add(str(exc))
        limits.extend(timestamp(value) for value in (
            record["grant"]["payload"]["expires_at"], record["grant"]["authentication"]["expires_at"],
            record["scrub"]["authentication"]["expires_at"],
        ))
    expires = None if unknown_expiry else min(limits)
    if expires is not None and expires <= evaluated_at:
        gaps.add("assessment_expired")
    if request["review"] != "approved":
        gaps.add("assessment_review_not_approved")
    return expires, gaps


def build(root, config, request, view, *, evaluated_at=None, expected_revision=None):
    request = request_document(request)
    evaluated_at = evaluated_at or view.now
    publication = run_selected_publication(root, request["question_slugs"], expected_revision=expected_revision,
        _usage_view=view, _purpose=request["purpose"], _consumer=request["consumer"], _expected_config=config)
    inputs = publication.pop("authorized_inputs")
    expires, gaps = qualifications(view, request, inputs, evaluated_at)
    if publication["verdict"] != "ship":
        gaps.add("assessment_publication_not_ready")
    payload = {"schema_version": SCHEMA, "required_capabilities": CAPABILITIES, "request": request,
               "evaluated_at": evaluated_at.isoformat(), "expires_at": None if expires is None else expires.isoformat(),
               "inputs": inputs, "basis": {"workspace_revision": publication["revision"]["revision_id"],
                    "configuration": configuration_id(config), "producer": publication["producer_id"], "authority": view.trust["content_hash"]},
               "publication": publication, "gaps": sorted(gaps), "recorded_eligible": not gaps}
    payload["assessment_id"] = content_id(SCHEMA, payload)
    return assessment_document(detached(payload))


def prepare(root, config, request):
    with current_view(root, config) as view:
        payload = build(root, config, request, view)
        view.revalidate(root, config)
        require(payload["expires_at"] is None or view.now < timestamp(payload["expires_at"]), "assessment_expired_during_evaluation")
        return {"assessment": payload, "registration": {"schema_version": "evidence-usage-command/v1", "state_id": view.state.state_id,
                "workspace_binding": view.state.binding, "request_id": None, "expected_checkpoint": view.state.checkpoint,
                "action": "register-assessment", "body": {"assessment": payload, "supersedes": []}}}


def envelope_payload(root, config, envelope, view):
    command = validate_command(envelope, view.state.state_id, workspace_binding(root))
    require(command["action"] == "register-assessment", "assessment_envelope_action_invalid")
    authenticated(envelope, "assessment", view.trust, view.now)
    return assessment_document(command["body"]["assessment"])


def compare_current(root, config, payload, view, *, issuing=False):
    """Recompute selected publication and input authority; a stored verdict is insufficient."""
    require(payload["basis"]["configuration"] == configuration_id(config), "assessment_configuration_changed")
    require(payload["basis"]["authority"] == view.trust["content_hash"], "assessment_policy_changed")
    require(payload["basis"]["producer"] == producer_identity(), "assessment_implementation_changed")
    fresh = build(root, config, payload["request"], view, evaluated_at=timestamp(payload["evaluated_at"]) if issuing else None,
                  expected_revision=payload["basis"]["workspace_revision"] if issuing else None)
    require(payload["inputs"] == fresh["inputs"], "assessment_source_revision_changed")
    if issuing:
        require(stable_publication(payload["publication"]) == stable_publication(fresh["publication"]),
                "assessment_publication_proof_mismatch")
    require(payload["publication"]["question_slugs"] == fresh["publication"]["question_slugs"]
            and payload["publication"]["export"]["questions"] == fresh["publication"]["export"]["questions"],
            "assessment_selected_answers_changed")
    require(payload["publication"]["verdict"] == fresh["publication"]["verdict"], "assessment_publication_changed")
    if not issuing:
        require(not fresh["gaps"], fresh["gaps"][0] if fresh["gaps"] else "assessment_qualification_changed")
    require(payload["expires_at"] == fresh["expires_at"] and payload["gaps"] == fresh["gaps"], "assessment_qualification_changed")
    return fresh


def validate_issue(root, config, envelope, view):
    payload = envelope_payload(root, config, envelope, view)
    require(timestamp(payload["evaluated_at"]) <= view.now, "assessment_evaluation_clock_invalid")
    compare_current(root, config, payload, view, issuing=True)
    for previous in envelope["payload"]["body"]["supersedes"]:
        require(previous in view.state.assessments, "assessment_superseded_identity_unknown")
        old = view.state.assessments[previous]["envelope"]["payload"]["body"]["assessment"]
        require(old["request"]["question_slugs"] == payload["request"]["question_slugs"], "assessment_superseded_scope_mismatch")
    return payload


def closing_issue(root, config, payload, view):
    require(capture_workspace(root).revision_id == payload["basis"]["workspace_revision"], "assessment_workspace_changed")
    view.revalidate(root, config)
    require(payload["expires_at"] is None or view.now < timestamp(payload["expires_at"]), "assessment_expired_during_evaluation")


def check_with_view(root, config, envelope, view, *, at=None):
    payload = envelope_payload(root, config, envelope, view)
    identifier = payload["assessment_id"]
    report = {"schema_version": "evidence-assessment-check/v1", "assessment_id": identifier,
              "eligible": False, "evaluated_at": view.now.isoformat(), "checkpoint": view.state.checkpoint,
              "reasons": [], "external_action_authorized": False}
    try:
        recorded = view.state.assessments.get(identifier)
        require(recorded is not None and recorded["envelope"] == envelope, "assessment_not_registered")
        require(identifier not in view.state.assessment_invalidations, "assessment_invalidated")
        require(payload["request"]["temporal"]["mode"] == "current", "assessment_replay_only")
        require(payload["request"]["review"] == "approved", "assessment_review_not_approved")
        require(payload["expires_at"] is not None, "assessment_input_validity_unknown")
        require(timestamp(payload["evaluated_at"]) <= view.now < timestamp(payload["expires_at"]), "assessment_expired")
        if at is not None:
            require(at < timestamp(payload["expires_at"]), "assessment_expired")
        require(payload["recorded_eligible"], "assessment_recorded_ineligible")
        fresh = compare_current(root, config, payload, view)
        require(capture_workspace(root).revision_id == fresh["basis"]["workspace_revision"], "assessment_workspace_changed")
        view.revalidate(root, config)
        require(view.now < timestamp(payload["expires_at"]), "assessment_expired_during_evaluation")
        report.update(eligible=True, evaluated_at=view.now.isoformat())
    except (Exception, SystemExit) as exc:
        report["reasons"] = [unavailable_reason(exc)]
    return report


def check(root, config, envelope):
    envelope = detached(envelope)
    with current_view(root, config) as view:
        return check_with_view(root, config, envelope, view)
