"""Read current acquisition instructions and genuine retained capture custody."""

from __future__ import annotations

import hashlib
from pathlib import Path

from evidence_wiki import mutation_operations as mutations
from evidence_wiki import request_lifecycle_operations as lifecycle
from evidence_wiki import source_capture_contracts as captures
from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.errors import UsageError
from evidence_wiki.host_context import _HISTORICAL_READ, current_context
from evidence_wiki.mutation_contracts import INTENT, REQUEST, checked, make_intent
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.pack_discovery import owner

from .acquisition_contracts import instruction

OUTER_SLOT = "acquisition-outer-v1"


def refuse(field):
    raise UsageError("ACQUISITION_OBSERVATION_REFUSED", "Acquisition observation refused.", details={"field": field})


def _host(root, seam):
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("current_host_control_required")
    context.require_grant("acquisition_operations." + seam)
    write=seam == "fulfill"
    if write and _HISTORICAL_READ.get():
        refuse("historical_read_custody")
    return context.require_path(Path(root),write=write).resolve(strict=True), context


def _request(root, request_id):
    if type(request_id) is not str or not 1 <= len(request_id) <= 128:
        refuse("request_id")
    observed = lifecycle.current(root, request_ids=[request_id])
    view = lifecycle._ScopedView(root)
    _path, records = lifecycle._records(root, view)
    record = next((row for row in records if row["request_id"] == request_id), None)
    if record is None:
        refuse("request_unknown")
    lifecycle._guard(root, observed["workspace_revision"])
    return observed, view, record


def prepare(root, *, request_id, alternative_ids=(), run_id=None):
    """Read bounded retained request/candidate labels without planning side effects."""
    root, _context = _host(root, "prepare")
    alternative_ids = lifecycle._ids(alternative_ids, maximum=32)
    observed, view, record = _request(root, request_id)
    native = owner("source_requests")
    configured = native.acquisition_plan_context(view.config)
    path = native.candidate_store_path(root, view.config)
    current_context().require_path(path)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        refuse("candidate_store_outside_workspace")
    raw = view.read(relative, 8_388_608)
    candidates, seen = [], set()
    lines = (raw or b"").splitlines()
    if len(lines) > 4096:
        refuse("candidate_store_bound")
    for line in lines:
        if not line.strip():
            continue
        row = json_document(line)
        if type(row) is not dict:
            refuse("candidate_store_invalid")
        identifier = row.get("candidate_id")
        if type(identifier) is not str or identifier in seen:
            refuse("candidate_identity_invalid")
        seen.add(identifier)
        if row.get("status") != "selected" or native.selected_candidate_request_id(row) != request_id:
            continue
        if alternative_ids and identifier not in alternative_ids:
            continue
        if len(candidates) >= 32:
            refuse("candidate_selection_bound")
        route = native.candidate_acquisition_route(row, configured, request_id)
        provider = route.get("provider")
        if type(provider) is not str:
            continue
        candidates.append({"alternative_id": identifier, "provider_id": provider,
            "resource_ref": row.get("url"), "scope": row.get("scope") or record.get("scope") or {}})
    if alternative_ids and not set(alternative_ids) <= {row["alternative_id"] for row in candidates}:
        refuse("candidate_unknown_or_unmapped")
    if run_id is not None:
        # Explicit run selection is retained, but no inactive/arbitrary run JSON
        # supplies an executable budget before the owning workflow integration.
        lifecycle._ids([run_id])
    value = instruction(record, required_scope=observed["requests"][0]["required_scope"],
        alternatives=candidates, max_attempts=0)
    value.update(provider_configuration={"enabled": configured["enabled"], "providers": configured["providers"]},
        run_id=run_id, budget_status="owning_run_reservation_required", status="blocked_external",
        reason="selected_session_budget_recheck_required", workspace_revision=observed["workspace_revision"])
    view.finish()
    lifecycle._guard(root, observed["workspace_revision"])
    return value


def reconcile(root, *, request_id, acquisition_ref):
    """Observe only a native private capture receipt and current source readiness."""
    root, context = _host(root, "reconcile")
    lifecycle._ids([acquisition_ref])
    observed, view, record = _request(root, request_id)
    store = MutationStore(root, context.mutation_control_directory)
    with store.historical_locked(acquisition_ref):
        intent = store._read("intent.json")
        receipt = store.receipt()
        if (type(intent) is not dict or intent.get("host_context_sha256") != mutations._host_selection()
                or intent.get("workspace_binding") != store.workspace_binding):
            refuse("capture_current_host_binding_required")
    if (type(receipt) is not dict or receipt.get("status") != "committed"
            or receipt.get("operation") != "sources.capture_revision"):
        refuse("native_capture_receipt_required")
    result = receipt.get("result")
    delivery = captures.validate(intent["arguments"]["delivery"])
    capture = delivery["capture"]
    if (capture.get("request_id") != request_id or type(result) is not dict
            or result.get("schema_version") != "evidence-host-delivery-result/v2"
            or result.get("status") not in {"delivered", "already_present"}
            or result.get("capture_id") != capture["capture_id"]
            or result.get("content_sha256") != capture["content_sha256"]
            or result.get("content_bytes") != capture["content_bytes"]
            or result.get("path") != intent["arguments"]["path"]
            or result.get("authorized") is not False or result.get("evidence_accepted") is not False):
        refuse("native_capture_result_binding_required")
    from evidence_wiki.onboarding_contract import _matches
    from evidence_wiki.source_contracts import schema_document
    _matches(result, schema_document("evidence-host-delivery-result/v2"))
    source_id = result.get("source_id") if type(result) is dict else None
    if type(source_id) is not str:
        refuse("native_capture_receipt_source_invalid")
    source = lifecycle._source(view, source_id,
        {**(record.get("scope") or {}), **observed["requests"][0]["required_scope"]})
    if record["status"] == "fulfilled" and record.get("source_id") != source_id:
        refuse("historical_fulfillment_conflict")
    view.finish()
    lifecycle._guard(root, observed["workspace_revision"])
    return {"schema_version": "evidence-acquisition-reconciliation/v1", "request_id": request_id,
        "request_basis": "sha256:" + hashlib.sha256(canonical(record)).hexdigest(),
        "acquisition_ref": acquisition_ref, "capture_input_sha256": receipt["input_sha256"],
        "source": source, "status": "ready_for_owner_fulfillment" if source["current_ready"] else "partial",
        "next_operation": "source_requests.fulfill_current" if source["current_ready"] else "recheck_or_authorized_alternative",
        "workspace_revision": observed["workspace_revision"], "recorded_status": record["status"],
        "recorded_source_id": record.get("source_id"), "request_fulfilled_by_reconciliation": False,
        "source_accepted": False, "rights_inferred": False}


def fulfill(root, *, request_id, acquisition_ref, expected_request_basis, expected_material_basis, mutation_request_id):
    """Freeze a current reconciliation before the existing native fulfillment owner."""
    root, context = _host(root, "fulfill")
    lifecycle._ids([request_id])
    lifecycle._ids([acquisition_ref])
    lifecycle._ids([mutation_request_id])
    for value in (expected_request_basis, expected_material_basis):
        if (type(value) is not str or len(value) != 71 or not value.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in value[7:])):
            refuse("expected_basis_invalid")
    outer = {"request_id": request_id, "acquisition_ref": acquisition_ref,
        "expected_request_basis": expected_request_basis, "expected_material_basis": expected_material_basis,
        "mutation_request_id": mutation_request_id}
    retained = _retained_fulfillment(root, context, outer)
    if retained is not None:
        return _fulfillment_result(request_id, mutation_request_id, mutations.submit(root, retained))
    observed = reconcile(root, request_id=request_id, acquisition_ref=acquisition_ref)
    if observed["request_basis"] != expected_request_basis:
        refuse("request_basis_changed")
    if observed["source"].get("material_basis") != expected_material_basis:
        refuse("source_material_changed")
    if observed["source"]["current_ready"] is not True:
        return {"schema_version": "evidence-acquisition-fulfillment/v1", "status": "blocked_external",
            "reason": "current_source_not_ready", "request_id": request_id, "source": observed["source"],
            "request_fulfilled": False, "source_accepted": False, "rights_inferred": False}
    lifecycle._guard(root, observed["workspace_revision"])
    prepared = mutations.prepare_request(root, request_id=mutation_request_id,
        operation="source_requests.fulfill_current", arguments={"request_id": request_id,
            "source_id": observed["source"]["source_id"]})
    if prepared["expected_revision"] != observed["workspace_revision"]:
        refuse("workspace_changed_before_fulfillment")
    _stage_outer(root, context, prepared, outer, observed)
    receipt = mutations.submit(root, prepared)
    return _fulfillment_result(request_id, mutation_request_id, receipt)


def _fulfillment_result(request_id, mutation_request_id, receipt):
    return {"schema_version": "evidence-acquisition-fulfillment/v1", "status": receipt["status"],
        "request_id": request_id, "mutation_request_id": mutation_request_id, "receipt": receipt,
        "historical_fulfillment_recorded": receipt["status"] == "committed" and receipt["result"].get("current_ready") is True,
        "historical": True, "current_evidence_usable": "not_established",
        "source_accepted": False, "rights_inferred": False}


def _retained_fulfillment(root, context, outer):
    # Fulfillment is an authorized write: its first call may create the request
    # namespace before any protected intent exists. Observation reads stay read-only.
    with MutationStore(root, context.mutation_control_directory).locked(outer["mutation_request_id"]) as store:
        value = store._read("intent.json")
        if value is None:
            return None
        intent = checked(INTENT, value)
        if (intent["operation"] != "source_requests.fulfill_current"
                or intent["host_context_sha256"] != mutations._host_selection()
                or intent["workspace_binding"] != store.workspace_binding):
            refuse("retained_fulfillment_identity_conflict")
        descriptor = store.staged(OUTER_SLOT)
        if descriptor is None:
            if store.read() is None and store.receipt() is None:
                return None
            refuse("retained_outer_binding_missing")
        value = json_document(store.read_stage(descriptor))
        if (type(value) is not dict or set(value) != {"schema_version", "inputs", "source_id", "capture_input_sha256"}
                or value.get("schema_version") != "evidence-acquisition-outer/v1" or value["inputs"] != outer
                or type(value["capture_input_sha256"]) is not str or len(value["capture_input_sha256"]) != 71
                or not value["capture_input_sha256"].startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in value["capture_input_sha256"][7:])
                or intent["arguments"]["request_id"] != outer["request_id"]
                or intent["arguments"]["source_id"] != value["source_id"]
                or intent["arguments"]["require_scope"] is not True):
            refuse("retained_outer_binding_conflict")
        request = {key: item for key, item in intent.items() if key not in {"workspace_binding", "input_sha256"}}
        request["schema_version"] = REQUEST
        return checked(REQUEST, request)


def _stage_outer(root, context, request, outer, observed):
    selected_root, control = mutations._scope(root, "submit", write=True)
    if selected_root != root or control != context.mutation_control_directory:
        refuse("current_writer_custody_conflict")
    current_context().require_grant("request_lifecycle_operations.fulfill_current")
    stem = "run_fulfill_current_request" if observed["source"].get("permission_basis") == "native_current_usage" else "run_fulfill_request"
    current_context().require_grant("source_requests." + stem)
    value = {"schema_version": "evidence-acquisition-outer/v1", "inputs": outer,
        "source_id": observed["source"]["source_id"], "capture_input_sha256": observed["capture_input_sha256"]}
    info = root.stat()
    intent = make_intent(request, {"path": str(root), "device": str(info.st_dev), "inode": str(info.st_ino)})
    with MutationStore(root, control, intent).locked() as store:
        lifecycle._guard(root, observed["workspace_revision"])
        if request["host_context_sha256"] != mutations._host_selection():
            refuse("current_host_selection_changed")
        store.prepare(intent)
        if store.read() is not None or store.receipt() is not None:
            refuse("retained_fulfillment_preflight_conflict")
        store.stage_blob(OUTER_SLOT, canonical(value))


def prepare_session(session, *, request_id, alternative_ids=()):
    """Read selected run limits and native alternatives without dispatch or renewal."""
    from .acquisition_budget import remaining
    from .acquisition_contracts import instruction
    from .research_session import ResearchSession

    if type(session) is not ResearchSession or session.execution is None:
        refuse("bound_execution_session_required")
    session._check("prepare_acquisition")
    value=prepare(session.root,request_id=request_id,alternative_ids=alternative_ids,run_id=session.run_id)
    available=remaining(session)
    _observed,view,record=_request(session.root,request_id)
    fetch=owner("fetch_sources")
    configured=owner("source_requests").acquisition_plan_context(view.config)
    provider_limit=configured.get("max_downloads_per_run")
    if type(provider_limit) is not int:
        provider_limit=fetch.max_downloads_per_run(fetch.acquisition_config(view.config))
    from .acquisition_budget import history, original_request
    from .mutation_contracts import digest
    base,_effective=original_request(session)
    in_run=sum(row["run_id"]==session.run_id for row in history(session,digest(base)))
    maximum=min(32,available["downloads"],available["run_attempts"],max(0,provider_limit-in_run))
    configured=value["provider_configuration"]
    if available["bytes"]==0 or available["blockers"] or not configured["enabled"]:
        maximum=0
    refreshed=instruction(record,required_scope=value["required_scope"],alternatives=value["alternatives"],max_attempts=maximum)
    view.finish()
    value.update(refreshed,budget_status="selected_run_observed_reservation_required",
        status="ready_for_owner_check" if maximum else "blocked_external",
        reason="current_scope_permission_budget_reservation_required" if maximum else "frozen_acquisition_budget_exhausted")
    return value
