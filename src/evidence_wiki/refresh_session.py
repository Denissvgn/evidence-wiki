"""Superseding bounded research runs preserve authenticated terminal history."""

from __future__ import annotations

from . import execution_session as native
from .errors import EvidenceWikiError
from .history_operations import _subject
from .mutation_contracts import canonical, detached, digest, validate_request_id
from .pack_discovery import owner
from .refresh_contracts import RESULT as SCHEMA
from .refresh_contracts import checked, current_outcome
from .refresh_owner import MARKER, sha
from .research_session_contracts import refuse


def selected(records):
    rows = [row for row in records if row["kind"] == "execution_refresh"]
    if len(rows) > 1:
        refuse("REFRESH_REQUEST_CONFLICT")
    return rows[0]["data"] if rows else None


def _terminal(records):
    return next((row for row in reversed(records) if row["kind"] == "execution_step"
        and row["data"]["step"]["terminal"]), None)


def refresh(session, *, request_id, previous_run_id, max_steps=64, planner_config_ref=None, source_updates=(), operation_control=None):
    from .operation_control import checkpoint
    checkpoint(operation_control, "research_refresh_start")
    session._check("refresh", write=True)
    if session.execution is None:
        refuse("EXECUTION_CONFIG_INVALID")
    request_id, previous_run_id = validate_request_id(request_id), validate_request_id(previous_run_id)
    if previous_run_id == session.run_id or type(max_steps) is not int or not 1 <= max_steps <= 64:
        refuse("REFRESH_ARGUMENT_INVALID")
    updates = _updates(source_updates)
    if planner_config_ref is None:
        refuse("PLANNER_ROLE_REQUIRED")
    from .execution_planning import configured
    configured(session, planner_config_ref)
    for grant in ("research_session.brief", "research_session.resolution", "brief_operations.current", "resolution_operations.current"):
        session.context.require_grant(grant)
    session.context.require_grant("refresh_owner.apply")
    session.context.require_grant("research_session.run_cycle")
    with native.gate(session):
        records = native.retained(session)
        previous = selected(records)
        arguments = {"previous_run_id": previous_run_id, "max_steps": max_steps,
            "planner_config_ref": planner_config_ref, "source_updates": updates}
        if previous is not None:
            if previous["request_id"] != request_id or previous["arguments"] != arguments:
                refuse("REQUEST_CONFLICT")
        else:
            if records:
                refuse("REFRESH_NEW_RUN_REQUIRED")
            subject, parent_records = _subject(session.root, previous_run_id, session.execution)
            parent_doc = owner("run_controller")._session_state(session.root, previous_run_id)
            terminal = _terminal(parent_records)
            closure = next((row["data"]["closure"] for row in reversed(parent_records) if row["kind"] == "execution_close"), None)
            if (parent_doc["state"]["current"] not in owner("run_controller").TERMINAL_STATES or terminal is None
                    or closure is None or closure["terminal_decision_ref"] != digest(terminal["data"]["step"]["decision"])):
                refuse("REFRESH_TERMINAL_PARENT_REQUIRED")
            if subject.binding["original_ids"] != session.binding["original_ids"]:
                refuse("ORIGINALS_CHANGED")
            parent_plan = session.execution.ledger(subject, parent_records)
            accounting = native.accounting.projection(parent_plan, limits=native._limits(session))
            parent_refresh = selected(parent_records)
            prior_prepared = next((row["data"] for row in parent_records
                if row["kind"] == "execution_refresh_prepared"), None)
            marker = owner("_evidence_revision").capture_workspace(session.root).files.get(MARKER)
            expected = (prior_prepared["marker_sha256"] if prior_prepared is not None else
                parent_refresh["expected_marker_sha256"] if parent_refresh is not None else None)
            if sha(marker) != expected:
                refuse("REFRESH_HEAD_CHANGED")
            current = native.current(session)
            before_sources = _source_ids(native._sources(session)[0])
            parent_sources = _parent_sources(parent_records)
            previous = {"request_id": request_id, "arguments": arguments,
                "before_basis_id": current["basis_id"], "expected_marker_sha256": expected,
                "parent_binding_id": digest(subject.binding), "parent_terminal_ref": digest(terminal),
                "parent_outcome": terminal["data"]["step"]["outcome"],
                "parent_usage": accounting["usage"],
                "parent_usage_unknown": accounting["usage_unknown"] or bool(parent_refresh and parent_refresh["parent_usage_unknown"]),
                "reused_source_ids": sorted(identifier for identifier, closure in before_sources.items()
                    if parent_sources.get(identifier) == closure),
                "changed_source_ids": sorted(identifier for identifier, closure in before_sources.items()
                    if parent_sources.get(identifier) != closure),
                "prior_current_eligibility": "not_evaluated"}
            native._begin(session)
            native.append(session, "execution_refresh", request_id, previous)
    cycle = session.run_cycle(request_id=native.child_id(request_id, "cycle"), max_steps=max_steps,
        planner_config_ref=planner_config_ref, operation_control=operation_control)
    from .brief_operations import _held_brief
    try:
        with _held_brief(session) as (brief, resolution):
            permitted = {row["source_id"] for row in resolution.source_packets()}
            report = _report(session, request_id, previous_run_id, previous, cycle, updates,
                brief, None, permitted)
        return report
    except EvidenceWikiError as error:
        return _report(session, request_id, previous_run_id, previous, cycle, updates,
            None, error.error_code, set())


def _report(session, request_id, previous_run_id, previous, cycle, updates, brief, brief_error, permitted):
    outcome, reason = current_outcome(cycle, brief)
    records = session.execution.records(session)
    collected = [row["data"] for row in records if row["kind"] == "execution_refresh_collection"]
    prepared = next((row["data"] for row in records if row["kind"] == "execution_refresh_prepared"), None)
    report = {"schema_version": SCHEMA, "request_id": request_id, "run_id": session.run_id,
        "previous_run_id": previous_run_id, "terminal": cycle["terminal"],
        "outcome": outcome, "reason": reason,
        "historical": cycle["historical"], "brief_error": brief_error,
        "parent_binding_id": previous["parent_binding_id"], "parent_terminal_ref": previous["parent_terminal_ref"],
        "prior_recorded_outcome": previous["parent_outcome"], "prior_current_eligibility": "not_evaluated",
        "refresh_id": prepared["refresh_id"] if prepared else None,
        "reused_source_ids": [identifier for identifier in previous["reused_source_ids"] if identifier in permitted],
        "changed_source_ids": [identifier for identifier in previous["changed_source_ids"] if identifier in permitted],
        "source_updates": [{key: row[key] for key in ("source_request_id", "status", "error_code") if key in row}
            for row in collected], "cycle": cycle, "superseding_brief": brief,
        "unresolved_source_update_request_ids": [row["source_request_id"] for row in updates if not any(
            result["source_request_id"] == row["source_request_id"] and result["status"] == "fulfilled" for result in collected)],
        "current_acceptance": "native_current_brief_only", "approval_reuse": False,
        "usage": cycle["usage"], "previous_usage": previous["parent_usage"],
        "budget_scope": "explicit_new_run_allocation; previous_accounting_retained_not_reset",
        "authorized": False, "evidence_accepted": False}
    if len(canonical(report)) > 1_048_576:
        refuse("LIMIT")
    return checked(report)


def _updates(value):
    from ._pack_io import relative_path
    from .source_capture_inputs import metadata_document
    if type(value) not in {tuple, list} or len(value) > 16:
        refuse("REFRESH_ARGUMENT_INVALID")
    rows, seen, paths = [], set(), set()
    for row in value:
        if type(row) is not dict or set(row) != {"source_request_id", "expected_request_basis", "metadata", "path"}:
            refuse("REFRESH_ARGUMENT_INVALID")
        identifier = validate_request_id(row["source_request_id"])
        supplied = metadata_document(row["metadata"])
        if supplied["request_id"] != identifier or identifier in seen:
            refuse("REFRESH_ARGUMENT_INVALID")
        expected = row["expected_request_basis"]
        import re
        if type(expected) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", expected) is None:
            refuse("REFRESH_ARGUMENT_INVALID")
        relative_path(row["path"])
        if row["path"] in paths:
            refuse("REFRESH_ARGUMENT_INVALID")
        seen.add(identifier)
        paths.add(row["path"])
        rows.append(detached(row))
    return rows


def _source_ids(packets):
    return {row["source_id"]: row["source_binding"]["closure_id"] for row in packets}


def _parent_sources(records):
    result = {}
    for row in records:
        if row["kind"] == "execution_attempt":
            for source in row["data"]["attempt"]["input"].get("sources", []):
                if "source_binding" in source:
                    result[source["source_id"]] = source["source_binding"]["closure_id"]
    return result


def next_action(session, view, records):
    instruction = selected(records)
    if instruction is None:
        return None
    if instruction["parent_usage_unknown"]:
        return native._terminal_action(session, view, "stop_budget", "usage_unknown")
    prepared = next((row for row in records if row["kind"] == "execution_refresh_prepared"), None)
    if prepared is None:
        return {"operation": "prepare", "original_id": session.binding["original_ids"][0],
            "refresh_request_id": instruction["request_id"], "reason": "bounded_refresh_preparation"}
    current_marker = owner("_evidence_revision").capture_workspace(session.root).files.get(MARKER)
    if sha(current_marker) != prepared["data"]["marker_sha256"]:
        return native._terminal_action(session, view, "blocked_external", "untrusted_observation")
    collected = {row["data"]["source_request_id"] for row in records if row["kind"] == "execution_refresh_collection"}
    for update in instruction["arguments"]["source_updates"]:
        if update["source_request_id"] not in collected:
            return {"operation": "refresh_collect", "original_id": session.binding["original_ids"][0],
                "source_update": update, "reason": "permitted_source_update_required"}
    generated = {row["data"]["original_id"] for row in records if row["kind"] == "execution_applied"
        and row["data"].get("operation") == "generate"}
    claimed = {row["data"]["original_id"] for row in records if row["kind"] == "execution_prepared"}
    for identifier in session.binding["original_ids"]:
        if identifier not in claimed:
            return {"operation": "prepare", "original_id": identifier, "reason": "claim_original_questions"}
        if identifier not in generated:
            return {"operation": "generate", "original_id": identifier, "purpose": "generate", "reason": "superseding_draft_required"}
    return None


def prepare(session, original_id):
    with native.gate(session):
        records = native.retained(session)
        instruction = selected(records)
        if instruction is None or any(row["kind"] == "execution_refresh_prepared" for row in records):
            return
        if original_id != session.binding["original_ids"][0]:
            refuse("ORIGINAL_SCOPE_UNSUPPORTED")
        arguments = {"run_id": session.run_id, "previous_run_id": instruction["arguments"]["previous_run_id"],
            "request_id": instruction["request_id"], "expected_basis_id": native.current(session)["basis_id"],
            "expected_marker_sha256": instruction["expected_marker_sha256"], "original_ids": session.binding["original_ids"]}
        # The immutable native effect arguments own retries after partial publication.
        previous = next((row for row in records if row["kind"] == "execution_native"
            and row["data"]["operation"] == "questions.refresh"), None)
        if previous is not None:
            arguments = previous["data"]["arguments"]
    receipt = native._mutation(session, native.child_id(instruction["request_id"], "prepare"), "questions.refresh", arguments,
        retain_owner_result=True)
    if receipt["status"] not in {"committed", "reconciled"}:
        refuse("NATIVE_EFFECT_UNOBSERVED")
    result = receipt.get("result")
    if result is None:
        from .mutation_refresh_recovery import load
        from .mutation_store import MutationStore
        from .workspace import Workspace
        with native.gate(session), Workspace.open(session.root) as workspace:
            inspected = workspace.mutations.inspect(receipt["request_id"])["record"]
            store = MutationStore(session.root, session.context.mutation_control_directory)
            with store.historical_locked(receipt["request_id"], borrow_workspace=True):
                plan = load(session.root, inspected["intent"], inspected["state"]["owner_basis"], store)
                capture = owner("_evidence_revision").capture_workspace(session.root)
                if any(capture.files.get(row["path"]) != row["data"] for row in plan["outputs"]):
                    refuse("NATIVE_EFFECT_UNOBSERVED")
                result = plan["report"]
    with native.gate(session):
        native.append(session, "execution_refresh_prepared", native.child_id(instruction["request_id"], "prepared"), result)


def collect(session, update):
    instruction = selected(session.execution.records(session))
    request_id = native.child_id(instruction["request_id"], "collect." + update["source_request_id"])
    record_id = native.child_id(request_id, "record")
    try:
        outcome = session.acquire_evidence(request_id=request_id, **update)
    except EvidenceWikiError as error:
        with native.gate(session):
            native.append(session, "execution_refresh_collection", record_id, {
                "source_request_id": update["source_request_id"], "status": "refused", "error_code": error.error_code})
        raise
    with native.gate(session):
        native.append(session, "execution_refresh_collection", record_id, {
            "source_request_id": update["source_request_id"], "status": outcome["status"], "outcome": outcome})
    if outcome["status"] != "fulfilled":
        refuse("NATIVE_EFFECT_UNOBSERVED")
