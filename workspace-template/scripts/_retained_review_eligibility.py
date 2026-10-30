#!/usr/bin/env python3
"""Native retained-review eligibility through the actual strict owner module."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import ModuleType


def _failure(error, core):
    reason = error.details.get("reason", "") if isinstance(error, core.ScriptRefusal) else ""
    if reason in {"strict_review_basis_changed", "selective_dependency_basis_changed"}:
        return "ineligible", "basis_changed"
    if reason == "selective_original_material_changed":
        return "ineligible", "semantic_material_changed"
    if reason == "selective_producer_changed":
        return "ineligible", "producer_changed"
    if reason == "strict_review_authority_changed" or "revoked" in reason:
        return "ineligible", "revoked_or_superseded"
    if reason == "strict_review_expired":
        return "ineligible", "review_expired"
    if reason in {"strict_review_not_independent", "strict_authorship_mismatch"}:
        return "ineligible", "role_or_controller_changed"
    if reason == "strict_review_authentication_failed":
        return "ineligible", "original_authentication_failed"
    if reason in {"strict_inputs_changed", "strict_review_snapshot_changed"}:
        return "unknown", "current_custody_changed"
    if "custody" in reason or reason.startswith("selective_original_"):
        return "unknown", "original_custody_unavailable"
    if "bound" in reason or "large" in reason:
        return "unknown", "bound_exceeded"
    if "time" in reason or "clock" in reason or "expired" in reason:
        return "ineligible", "clock_invalid"
    return "unknown", "native_owner_unavailable"


def observe(root, envelope, *, claim_id, expected_review_id, _core):
    """Observe one explicit retained service without registering or dispatching."""
    if (type(_core) is not ModuleType or getattr(_core, "_CORE_MODULE", None) is not _core
            or Path(_core.__file__).resolve() != Path(__file__).resolve().with_name("_strict_evidence.py")):
        raise ValueError("strict_retained_owner_invalid")
    core = _core
    root = Path(root).resolve()
    output = {"state": "unknown", "reason": "native_owner_unavailable",
        "current": dict.fromkeys(("basis_id", "semantic_material_id", "workspace_basis_id", "review_producer_id", "selected_review_id")),
        "strict": None, "publication": None, "usage_authority": None, "custody": None,
        "original_authenticated": False, "original_controller_ref": None, "original_generator_controller_ref": None}
    try:
        config = core.configuration(root)
        policy = core.resolve_policy(root, config)
        core.require(policy is not None, "strict_policy_missing")
        payload = core.review_document(envelope["payload"]["body"]["review"])
        core.require(payload["claim_id"] == claim_id and core.artifact_id(payload) == expected_review_id,
            "strict_authorship_mismatch")
        with core.sibling("_evidence_usage").current_view(root, config) as view:
            record = view.state.strict_reviews.get(expected_review_id)
            core.require(record is not None and record["envelope"] == envelope, "strict_review_authentication_failed")
            output["usage_authority"] = {"authority": core.sibling("_evidence_authority").authority_basis(view.trust),
                "state_id": view.state.state_id, "workspace_binding": view.state.binding, "checkpoint": view.state.checkpoint,
                "observed_at": view.now.isoformat()}
            core.require(record["authority"]["content_hash"] == view.trust["content_hash"], "strict_review_authority_changed")
            report = core.evaluate(root, config, policy, view=view)
            output["strict"] = report
            row = next((item for item in report["claims"] if item["claim"]["id"] == claim_id), None)
            core.require(row is not None, "strict_claim_unknown")
            output["publication"] = core.publication(root, view=view)
            output["current"].update(workspace_basis_id=report["basis_id"],
                review_producer_id=report.get("review_producer_id"), selected_review_id=row["review"].get("review_id"))
            if policy["schema_version"] != core.POLICY_SCHEMA_V2:
                output["current"]["basis_id"] = report["basis_id"]
                output.update(state="ineligible", reason="unsupported_contract")
                return output
            core.require(payload["schema_version"] == core.REVIEW_SCHEMA_V3, "strict_authorship_mismatch")
            capture, manifest, full_basis = core.selection_inputs(root, config, policy, view=view)
            producer = core._review_producer()
            current = core.dependency_current(root, config, policy, view, capture, manifest, claim_id)
            output["current"].update(basis_id=current["dependency_basis"]["basis_id"],
                semantic_material_id=current["dependency_basis"]["semantic_material_id"])
            core.require(payload["observation"]["producer_id"] == report["review_producer_id"], "selective_producer_changed")
            core.require(payload["basis_id"] == current["dependency_basis"]["basis_id"], "selective_dependency_basis_changed")
            auth = core._dependency_authenticated(envelope, payload, current, view, policy)
            proof = core._dependency_original(root, config, policy, payload, current, purpose="reuse")
            proof.assert_review_envelope(envelope)
            generator = core.verify_attestation(payload["generator"], "generator", view.trust, view.now)
            output.update(original_authenticated=True, original_controller_ref=auth["controller"],
                original_generator_controller_ref=generator["controller"])
            output["custody"] = {"review_id": expected_review_id,
                "basis_id": current["dependency_basis"]["basis_id"], "semantic_material_id": current["dependency_basis"]["semantic_material_id"],
                "original_snapshot_id": "sha256:" + hashlib.sha256(core.sibling("_evidence_revision").canonical_bytes(proof.original_snapshot())).hexdigest(),
                "delivery_binding_id": payload["delivery"]["binding_sha256"], "original_verified": True}
            own_pass = all(verdict == "pass" for verdict in payload["verdicts"].values())
            with core.internal_publication(root, config):
                readiness = core.sibling("_selected_publication").run_selected_publication(root, [row["claim"]["question_slug"]],
                    _usage_view=view, _purpose="qa-export", _consumer="evidence-wiki", _expected_config=config)
            if not own_pass or not row["accepted"] or readiness["verdict"] != "ship":
                output.update(state="ineligible", reason="claim_not_accepted")
            else:
                output.update(state="eligible", reason="validated_retained_review")
            core._dependency_closing(root, config, policy, view, capture, full_basis, producer,
                (current,), ((envelope, payload, current),) if own_pass else ())
            proof.assert_current()
        return output
    except Exception as error:
        output["state"], output["reason"] = _failure(error, core)
        return output
