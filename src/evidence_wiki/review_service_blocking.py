"""Conservative current projection when a bound service authority is unavailable."""

from datetime import datetime, timezone

from ._pack_io import yaml_document
from .mutation_contracts import digest
from .pack_discovery import owner
from .research_session_contracts import CURRENT_V2, checked


def current(session):
    """Retain actual originals and occupied accounting; certify no current evidence."""
    from . import execution_session as native
    from .research_session import TERMINAL_GRANTS, _run_summary

    capture = owner("_evidence_revision").capture_workspace(session.root)
    config = yaml_document(capture.files["research.yml"])
    from .requirements_operations import read_effective
    effective = read_effective(session.root, raw=capture.files.get("docs/research-requirements.json"))
    mapping = owner("_caller_context").original_questions(session.root, config)
    if not effective:
        native.refuse("ORIGINALS_CHANGED")
    if mapping["gaps"]:
        native.refuse("ORIGINALS_CHANGED")
    rows = []
    for original in mapping["originals"]:
        rows.append({"original_id": original["id"], "original_text": original["text"],
            "questions": [{"question_id": qid, "slug": mapping["expected"][qid], "accepted": False,
                "native_acceptance": "unavailable", "gaps": ["service_authority_unavailable"]}
                for qid in original["question_ids"] if qid in mapping["expected"]],
            "missing_question_ids": [qid for qid in original["question_ids"] if qid not in mapping["expected"]],
            "accepted": False, "gaps": ["service_authority_unavailable"]})
    ids = [row["original_id"] for row in rows]
    records = native.retained(session)
    document = native._document(session) if records else None
    ledger = session.execution.ledger(session, records)
    projection = None if ledger is None else native.accounting.projection(ledger, limits=native._limits(session))
    usage = {key: None for key in ("seconds", "model_calls", "tool_calls", "tokens", "cost_limit", "claims", "sources",
        "repair_cycles", "adjudication_cycles", "retry_attempts", "acquisition_attempts")}
    if projection is not None:
        for target, key in (("model_calls", "model_calls"), ("tool_calls", "tool_calls"), ("tokens", "tokens"), ("cost_limit", "cost")):
            usage[target] = projection["usage"][key]
        usage["seconds"] = (datetime.now(timezone.utc) - datetime.fromisoformat(document["started_at"].replace("Z", "+00:00"))).total_seconds()
    # Only caller-owned original declarations/policy enter this outward commitment.
    basis = digest({"binding": session.binding, "originals": rows, "authority_state": "unavailable"})
    if (owner("_evidence_revision").capture_workspace(session.root).revision_id != capture.revision_id
            or session.execution.records(session) != records):
        native.refuse("BASIS_CHANGED")
    return checked(CURRENT_V2, {"schema_version": CURRENT_V2, "target": str(session.root), "run_id": session.run_id,
        "basis_id": basis, "policy_id": session.policy.policy_id, "observed_utc": datetime.now(timezone.utc).isoformat(),
        "accounting": {"original_ids": ids, "original_outcomes": rows, "accounting_complete": True,
            "missing_mapping": False, "accepted_count": 0, "unresolved_count": len(rows)},
        "canonical_release": {"status": "unavailable", "research_complete": False, "publication": None,
            "gaps": ["service_authority_unavailable"], "assurance": "not_established"}, "run": _run_summary(document),
        "sources": {"inventory": {}, "requests": []}, "qualification": {"state": "unknown", "reason": "service_authority_unavailable", "profiles": []},
        "usage": usage, "policy_compatible": set(ids) == set(session.policy.to_dict()["scope"]["question_ids"]),
        "authority": {"terminal_grants_available": all(grant in session.context.grants for grant in TERMINAL_GRANTS),
            "required_terminal_grants": list(TERMINAL_GRANTS)},
        "execution": {"profile_id": "sha256:" + session.execution.profile.sha256, "scope_id": native.SCOPE,
            "source_bindings": [], "steps_used": sum(row["kind"] == "execution_advance" for row in records),
            "steps_limit": session.execution.profile.total_steps, "accounting": projection,
            "measurement_report": None if ledger is None else native.accounting.report(ledger),
            "operational_qualification": "unavailable", "human_intervention_scope": "unobserved", "pending": native._pending(records, projection)},
        "limitations": ["Unavailable host authority supplies no current source access, qualification or acceptance."],
        "authorized": False, "evidence_accepted": False})
