"""Current exact artifact references feed an inert Release proposal boundary."""

from .artifact_delivery_contracts import INPUT, MAX_BYTES, PROPOSAL, checked, refuse
from .brief_operations import _held_brief
from .brief_projection import render


def prepare(session):
    """Read current identity only; no provider, file writer or approval starts."""
    session._check("prepare_delivery")
    session.context.require_grant("artifact_delivery_operations.prepare")
    if session.execution is None or session.execution.profile.artifact_delivery is None:
        refuse("delivery_plan_required")
    plan = session.execution.profile.artifact_delivery
    policy = session.policy.to_dict()
    if "deliver" not in policy["allowed_effects"] or policy["host_capability_refs"].get("deliver") is None:
        refuse("delivery_capability_missing")
    with _held_brief(session) as (brief, observed):
        if (brief["verdict"] not in {"accept", "partial"} or brief["global_blockers"]
                or not brief["accounting_complete"] or brief["readability"]["state"] in {"pending", "refused"}):
            refuse("delivery_current_brief_ineligible")
        rendered = render(brief)
        raw = rendered["markdown"].encode()
        if len(raw) > min(MAX_BYTES, plan.max_output_bytes):
            refuse("delivery_artifact_bound")
        for destination in plan.destinations:
            session.context.require_path(session.root / "runs" / session.run_id / "deliveries" / destination.filename, write=True)
        result = checked(INPUT, {"schema_version": INPUT, "artifact": {"artifact_id": rendered["markdown_sha256"],
            "artifact_sha256": rendered["markdown_sha256"], "bytes": len(raw),
            "media_type": "text/markdown; charset=utf-8", "basis_id": observed._basis_id,
            "check_scope": "current_native_brief", "authorized": False},
            "approved_destination_refs": [row.destination_ref for row in plan.destinations],
            "plan_sha256": plan.sha256, "proposal_scope": "exact_identity_selection",
            "output_schema": PROPOSAL, "authorized": False})
    return result


def deliver(session, *, request_id, proposal, effect_deadline=None):
    """An exact proposal selects only a fresh, host-owned staged effect."""
    from . import execution_session as native
    from .artifact_delivery_contracts import RESULT
    from .artifact_delivery_input import publication_scope
    from .artifact_publication import retained_writer
    from .mutation_contracts import digest, validate_request_id
    from .workspace import Workspace

    request_id = validate_request_id(request_id)
    proposed = checked(PROPOSAL, proposal)
    session._check("deliver", write=True)
    session.context.require_grant("artifact_delivery_operations.deliver")
    current = prepare(session)
    plan = session.execution.profile.artifact_delivery
    expected = current["artifact"]
    if (proposed["plan_sha256"] != plan.sha256 or proposed["artifact_id"] != expected["artifact_id"]
            or proposed["artifact_sha256"] != expected["artifact_sha256"] or proposed["artifact_bytes"] != expected["bytes"]):
        refuse("delivery_proposal_changed")
    plan.destination(proposed["destination_ref"])
    arguments = {"plan_sha256": plan.sha256, "proposal": proposed}
    with native.gate(session):
        records = native.retained(session)
        previous = next((row for row in records if row["kind"] == "execution_native"
            and row["data"]["reservation"]["attempt_id"] == request_id), None)
        if previous is None and native._document(session)["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
            refuse("delivery_run_terminal")
    def invoke(control):
        with publication_scope(session, request_id, arguments, control), Workspace.open(session.root) as workspace:
            request = workspace.mutations.prepare(request_id=request_id, operation="artifact.deliver", arguments=arguments)
            return workspace.mutations.submit(request, operation_control=control)
    receipt = native._native_call(session, request_id, "artifact.deliver", arguments, invoke, effect_deadline=effect_deadline)
    complete = receipt["status"] in {"committed", "reconciled"}
    if complete:
        writer = retained_writer(session.context.mutation_control_directory.resolve(strict=True), request_id, receipt["input_sha256"])
        if writer is not None:
            writer.close_committed()
    return checked(RESULT, {"schema_version": RESULT, "request_id": request_id,
        "status": "committed" if complete else "commit_unknown", "artifact": expected if complete else None,
        "destination_ref": proposed["destination_ref"] if complete else None,
        "receipt_ref": digest(receipt) if complete else None, "boundary": "artifact_checked" if complete else "not_observed",
        "current_eligibility": "checked_at_delivery" if complete else "unavailable", "historical": previous is not None,
        "reasons": [] if complete else ["publication_uncertain"], "authorized": False, "evidence_accepted": False})
