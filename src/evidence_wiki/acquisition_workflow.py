"""One retained native web acquisition becomes current evidence through owners."""

from __future__ import annotations

import base64

from evidence_wiki import Workspace, source_capture_inputs
from evidence_wiki import execution_session as native
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest, validate_request_id
from evidence_wiki.research_session import ResearchSession


def refuse(reason):
    raise UsageError("ACQUISITION_WORKFLOW_REFUSED", "Acquisition workflow refused.", details={"reason": reason})


def acquire(session, *, request_id, source_request_id, expected_request_basis, metadata, path):
    """Freeze host declarations, then transport/capture/normalize/fulfill once."""
    if type(session) is not ResearchSession or session.execution is None:
        refuse("bound_session_required")
    request_id = validate_request_id(request_id)
    supplied = source_capture_inputs.metadata_document(metadata)
    if supplied["request_id"] != source_request_id:
        refuse("capture_request_binding_changed")
    from evidence_wiki._pack_io import relative_path
    relative_path(path)
    session._check("acquire",write=True)
    for grant in ("source_capture_operations.deliver","source_inventory.run_inventory_report",
            "normalize_sources.run_normalization_report","request_lifecycle_operations.fulfill_current",
            "acquisition_operations.reconcile","acquisition_operations.fulfill"):
        session.context.require_grant(grant)
    intent = {"source_request_id": source_request_id, "expected_request_basis": expected_request_basis,
        "metadata": supplied, "path": path}
    with native.gate(session,"acquire"):
        records=native.retained(session)
        previous=next((row for row in records if row["kind"]=="execution_acquisition_workflow"
            and row["request_id"]==request_id),None)
        if previous is not None and previous["data"]!=intent:
            refuse("request_conflict")
        completed=next((row for row in records if row["kind"]=="execution_acquisition_workflow_result"
            and row["request_id"]==native.child_id(request_id,"result")),None)
        if completed is not None:
            return completed["data"]
        if previous is None:
            from evidence_wiki.acquisition_operations import _request
            _observed,view,record=_request(session.root,source_request_id)
            try:
                if digest(record)!=expected_request_basis or supplied["origin_url"]!=record["query_or_identifier"]:
                    refuse("capture_origin_binding_changed")
            finally:
                view.finish()
            native._begin(session)
            native.append(session,"execution_acquisition_workflow",request_id,intent)
    fetched = session.acquire(request_id=native.child_id(request_id, "transport"),
        source_request_id=source_request_id, expected_request_basis=expected_request_basis)
    raw = base64.b64decode(fetched["content_base64"], validate=True)
    capture_id = native.child_id(request_id, "capture")
    with Workspace.open(session.root) as workspace:
        delivery=workspace.sources.capture_bytes_document(raw,supplied)
    captured=native._mutation(session,capture_id,"sources.capture_revision",{"path":path,"delivery":delivery},retain_owner_result=True)
    if captured["status"] != "committed":
        return _outcome("capture_unknown", source_request_id, captured)
    source_id = captured["result"]["source_id"]
    for label, operation, arguments in (("inventory", "sources.inventory", {}),
            ("normalize", "normalize.sources", {"source_ids": [source_id]})):
        receipt = native._mutation(session, native.child_id(request_id, label), operation, arguments,retain_owner_result=True)
        if receipt["status"] != "committed":
            return _outcome("partial", source_request_id, receipt)
    with native.gate(session,"acquire"):
        retained=next((row["data"]["arguments"] for row in native.retained(session)
            if row["kind"]=="execution_native" and row["data"]["reservation"]["attempt_id"]==native.child_id(request_id,"fulfillment")),None)
    if retained is not None:
        fulfillment_arguments=retained
    else:
        with Workspace.open(session.root) as workspace:
            observed=workspace.source_requests.reconcile_acquisition(request_id=source_request_id,acquisition_ref=capture_id)
            if observed["source"]["current_ready"] is not True:
                return _outcome("partial",source_request_id,observed)
            fulfillment_arguments={"request_id":source_request_id,"acquisition_ref":capture_id,
                "expected_request_basis":observed["request_basis"],"expected_material_basis":observed["source"]["material_basis"],
                "mutation_request_id":native.child_id(request_id,"fulfill")}
    def complete(_control):
        with Workspace.open(session.root) as workspace:
            return workspace.source_requests.complete_acquisition(**fulfillment_arguments)
    fulfilled=native._native_call(session,native.child_id(request_id,"fulfillment"),"acquisition.fulfill",fulfillment_arguments,
        complete,recover=complete)
    result=_outcome("fulfilled" if fulfilled["status"]=="committed" else "fulfillment_unknown",source_request_id,fulfilled)
    with native.gate(session,"acquire"):
        native.append(session,"execution_acquisition_workflow_result",native.child_id(request_id,"result"),result)
    return result


def _outcome(status, source_request_id, receipt):
    value={"schema_version": "evidence-acquisition-workflow-result/v1", "status": status,
        "source_request_id": source_request_id, "owner_result": receipt,
        "source_accepted": False, "rights_inferred": False, "semantic_adequacy": "not_evaluated"}

    from .acquisition_contracts import schema_document
    from .onboarding_contract import _matches
    _matches(value,schema_document("evidence-acquisition-workflow-result/v1"))
    return value


def run(session, *, request_id, instruction, config_ref, metadata, path):
    """Apply one authenticated inert choice only through fixed owning evidence steps."""
    from .acquisition_contracts import checked_instruction
    from .mutation_contracts import detached

    instruction=checked_instruction(instruction)
    choice=session.choose_acquisition(request_id=native.child_id(request_id,"choice"),instruction=instruction,config_ref=config_ref)
    if choice["status"]!="ready_for_owner_check":
        return _agent_result({"schema_version":"evidence-acquisition-agent-workflow-result/v1","status":choice["status"],
            "choice":choice,"evidence":None,"source_accepted":False,"rights_inferred":False})
    selected=choice["proposal"]["alternative_id"]
    alternative=next(row for row in instruction["alternatives"] if row["alternative_id"]==selected)
    if alternative["provider_id"]!="web" or alternative["resource_ref"]!=instruction["query_or_identifier"]:
        return _agent_result({"schema_version":"evidence-acquisition-agent-workflow-result/v1","status":"blocked_external",
            "choice":choice,"evidence":None,"source_accepted":False,"rights_inferred":False})
    evidence=session.acquire_evidence(request_id=native.child_id(request_id,"evidence"),source_request_id=instruction["request_id"],
        expected_request_basis=instruction["request_basis"],metadata=metadata,path=path)
    return _agent_result({"schema_version":"evidence-acquisition-agent-workflow-result/v1","status":evidence["status"],
        "choice":choice,"evidence":detached(evidence),"source_accepted":False,"rights_inferred":False})


def _agent_result(value):
    from .acquisition_contracts import schema_document
    from .onboarding_contract import _matches
    _matches(value,schema_document("evidence-acquisition-agent-workflow-result/v1"))
    return value
