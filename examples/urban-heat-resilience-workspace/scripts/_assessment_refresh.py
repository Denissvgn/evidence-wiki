#!/usr/bin/env python3
"""Bounded dependency scans and monotone assessment invalidation in the usage ledger."""

from __future__ import annotations

from _assessment_contract import REFRESH_SCHEMA, refresh_document, refresh_request
from _assessment_engine import check_with_view, unavailable_reason
from _evidence_authority import timestamp
from _evidence_revision import capture_workspace, content_id
from _evidence_usage import current_view
from _temporal_contract import require


def dependency_basis(root, view):
    # Invalidation cannot revive evidence and does not disturb a resume cursor.
    # Every event that could change evidence, authority or the assessment set does.
    events = [event["event_id"] for event in view.state.events
              if event["command"]["payload"]["action"] != "invalidate-assessments"]
    return content_id("evidence-assessment-refresh-basis/v1", {
        "workspace_revision": capture_workspace(root).revision_id, "authority": view.trust["content_hash"], "events": events,
    })


def plan_with_view(root, config, request, view):
    request = refresh_request(request)
    basis = dependency_basis(root, view)
    cursor = request["cursor"]
    if cursor is not None:
        require(cursor["basis"] == basis and cursor["after"] in view.state.assessments, "assessment_refresh_cursor_stale")
    identities = sorted(view.state.assessments)
    remaining = [identifier for identifier in identities if cursor is None or identifier > cursor["after"]]
    selected = remaining[:request["limit"]]
    future = None if request["evaluation_time"] is None else timestamp(request["evaluation_time"])
    entries = []
    for identifier in selected:
        if identifier in view.state.assessment_invalidations:
            continue
        envelope = view.state.assessments[identifier]["envelope"]
        payload = envelope["payload"]["body"]["assessment"]
        try:
            result = check_with_view(root, config, envelope, view, at=future)
            reasons = result["reasons"]
        except (Exception, SystemExit) as exc:
            reasons = [unavailable_reason(exc)]
        if reasons:
            entries.append({"assessment_id": identifier, "question_slugs": payload["request"]["question_slugs"],
                            "reasons": reasons, "reevaluation_id": content_id("evidence-reevaluation/v1", {"assessment_id": identifier})})
    more = len(remaining) > len(selected)
    require(dependency_basis(root, view) == basis, "assessment_refresh_inputs_changed")
    report = {"schema_version": REFRESH_SCHEMA, "request": request, "basis": basis,
              "evaluated_at": view.now.isoformat(), "entries": entries,
              "coverage": {"total": len(identities), "scanned": len(selected), "limit": request["limit"],
                           "complete": cursor is None and not more, "truncated": more,
                           "previous_pages_not_included": cursor is not None, "changed_sources_are_hints": True},
              "next_cursor": {"basis": basis, "after": selected[-1]} if more else None}
    report["plan_id"] = content_id(REFRESH_SCHEMA, report)
    return refresh_document(report)


def plan(root, config, request):
    with current_view(root, config) as view:
        report = plan_with_view(root, config, request, view)
        return {"plan": report, "application": {"schema_version": "evidence-usage-command/v1", "state_id": view.state.state_id,
                "workspace_binding": view.state.binding, "request_id": None, "expected_checkpoint": view.state.checkpoint,
                "action": "invalidate-assessments", "body": {"plan": report}}}


def validate_apply(root, config, report, view):
    refresh_document(report)
    require(dependency_basis(root, view) == report["basis"], "assessment_refresh_inputs_changed")
    fresh = plan_with_view(root, config, report["request"], view)
    require(report["entries"] == fresh["entries"] and report["coverage"] == fresh["coverage"]
            and report["next_cursor"] == fresh["next_cursor"], "assessment_refresh_plan_changed")


def closing_apply(root, config, report, view):
    require(dependency_basis(root, view) == report["basis"], "assessment_refresh_inputs_changed")
    view.revalidate(root, config)
