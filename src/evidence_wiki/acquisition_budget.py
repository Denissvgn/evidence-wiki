"""Frozen request scope and monotonic acquisition reservations in native runs."""

from __future__ import annotations

from ._pack_io import json_document, read_file
from .host_capabilities import scope_allows
from .mutation_contracts import digest
from .pack_discovery import owner
from .requirements_operations import REQUIREMENTS, instruction_matches, verifylineage
from .research_journal_contracts import digest_record


def baseline_time(session):
    """Use the authenticated parent baseline clock, never mutable run state."""
    from . import execution_accounting
    from . import execution_session as native
    from .acquisition_dispatch import refuse
    from .research_execution import SCOPE

    initial=next((row["data"]["baseline"] for row in native.retained(session)
        if row["kind"]=="execution_begin"),None)
    if initial is None:
        refuse("acquisition_baseline_missing")
    baseline=execution_accounting.validate_document(execution_accounting.OBSERVATION,initial)
    session.execution._verify(baseline)
    if (baseline["binding"]!={"session_binding_id":digest(session.binding)} or baseline["scope_id"]!=SCOPE
            or baseline["controller_ref"]!="session-observer" or baseline["attempt_id"] is not None
            or baseline["slots"]!=["baseline"] or baseline["sequence"]!=0):
        refuse("acquisition_baseline_mismatched")
    return baseline["observed_at"]


def original_request(session):
    """Verify frozen instruction custody before consuming inherited original limits."""
    from .acquisition_dispatch import refuse
    from .research_observation import ResearchObservation

    session.context.require_path(session.root/REQUIREMENTS)
    raw=read_file(session.root,REQUIREMENTS)
    observed=ResearchObservation(session.root)
    expected=None if observed.policy is None else observed.policy["instructions"].get(REQUIREMENTS)
    if expected is None or not instruction_matches(session.root,raw,expected):
        refuse("original_requirements_instruction_changed")
    try:
        lineage=verifylineage(session.root,json_document(raw))
    except ValueError:
        refuse("original_requirements_custody_invalid")
    observed.finish()
    return lineage["base"]["request"]["payload"],lineage["effective"]["request"]["payload"]


def history(session, original_id):
    """Read bounded authenticated reservations across the original request's runs."""
    from .acquisition_dispatch import refuse
    from .research_execution import JOURNAL

    controller=owner("run_controller")
    paths=sorted((session.root/"runs").glob("*/run-state.json"))
    if len(paths)>64:
        refuse("acquisition_run_history_bound")
    results=[]
    for path in paths:
        session.context.require_path(path)
        if path.is_symlink() or path.parent.is_symlink():
            refuse("acquisition_run_history_unsafe")
        document=controller._session_state(session.root,path.parent.name)
        binding=document.get("research_session")
        if binding is None:
            continue
        if type(binding) is not dict:
            refuse("acquisition_run_history_invalid")
        journal_path=controller.events_path(session.root,path.parent.name)
        session.context.require_path(journal_path)
        records=[]
        for event in controller._bounded_journal(journal_path)["events"]:
            data=event.get("data") or {}
            row=data.get("record",{})
            if row.get("schema_version")!=JOURNAL:
                continue
            previous=records[-1] if records else None
            if (set(row)!={"schema_version","kind","binding_id","request_id","sequence","previous_record_ref","data","authentication"}
                    or row["binding_id"]!=digest(binding) or row["request_id"]!=data.get("request_id")
                    or row["sequence"]!=len(records) or row["previous_record_ref"]!=(digest_record(previous) if previous else None)):
                refuse("acquisition_run_history_invalid")
            if binding.get("observer_id")!=session.binding["observer_id"]:
                refuse("acquisition_history_observer_unknown")
            session.execution._verify(row)
            records.append(row)
            if row["kind"]=="execution_acquisition_reservation":
                value=row["data"]
                if value.get("original_request_sha256") is None:
                    refuse("original_acquisition_history_unknown")
                if value["original_request_sha256"]==original_id:
                    if type(value.get("original_budget_started_utc")) is not str:
                        refuse("original_acquisition_clock_unknown")
                    results.append(value)
    return results


def reserve(session, request_id, record, configured, max_bytes):
    """Reserve declared maximum bytes and one attempt before external effects."""
    from . import execution_session as native
    from .acquisition_dispatch import refuse

    with native.gate(session):
        current=native.current(session)
        original,frozen=original_request(session)
        original_id=digest(original)
        policy=session.policy.to_dict()
        slugs={question["slug"] for original in current["accounting"]["original_outcomes"]
            if original["original_id"] in policy["scope"]["question_ids"] for question in original["questions"]}
        if not record.get("question_slugs") or not set(record["question_slugs"])<=slugs:
            refuse("request_outside_run_scope")
        authority=frozen["authority"]
        url=record["query_or_identifier"]
        if ("acquisition" not in authority["allowed_actions"] or not scope_allows(
                {"scope":[{"kind":"uri_prefix","value":value} for value in authority["source_scope"] if "://" in value]},url)):
            refuse("original_acquisition_scope_ungranted")
        budgets=frozen["budgets"]
        caps={"downloads":budgets["downloads"],"per_run_downloads":min(configured["max_downloads_per_run"],
                policy["continuation_limits"]["acquisition_attempts"]),"bytes":budgets["bytes"],
            "sources":policy["budgets"]["sources"],"source_requests":budgets["source_requests"],"seconds":budgets["seconds"]}
        previous=history(session,original_id)
        stored=next((row for row in previous if row["run_id"]==session.run_id and row["request_id"]==request_id),None)
        binding={"request_id":request_id,"run_id":session.run_id,"original_request_sha256":original_id,"source_request_id":record["request_id"],"request_basis":digest(record),
            "requirements_sha256":digest(frozen),"caps":caps,"url":url,"requested_max_bytes":max_bytes}
        if stored is not None:
            if {key:stored[key] for key in binding}!=binding:
                refuse("acquisition_reservation_conflict")
            return stored
        downloads=len(previous)
        used_bytes=sum(row["max_bytes"] for row in previous)
        run_downloads=sum(row["run_id"]==session.run_id for row in previous)
        selected_requests={row["source_request_id"] for row in previous}
        actual_sources=len(current["execution"]["source_bindings"])
        if (downloads>=caps["downloads"] or run_downloads>=caps["per_run_downloads"] or used_bytes>=caps["bytes"] or actual_sources+downloads>=caps["sources"]
                or len(selected_requests|{record["request_id"]})>caps["source_requests"]):
            refuse("frozen_acquisition_budget_exhausted")
        allowance=min(max_bytes,caps["bytes"]-used_bytes)
        if allowance<=0:
            refuse("frozen_acquisition_budget_exhausted")
        from datetime import datetime, timezone
        started=baseline_time(session)
        if previous:
            started=min([started,*[row["original_budget_started_utc"] for row in previous]],
                key=lambda value:datetime.fromisoformat(value.replace("Z","+00:00")))
        if datetime.now(timezone.utc)<datetime.fromisoformat(started.replace("Z","+00:00")):
            refuse("original_acquisition_clock_regressed")
        binding["original_budget_started_utc"]=started
        binding["max_bytes"]=allowance
        native.append(session,"execution_acquisition_reservation",native.child_id(request_id,"budget"),binding)
        return binding


def remaining(session):
    """Observe original and per-run remaining limits without reserving effects."""
    base,effective=original_request(session)
    rows=history(session,digest(base))
    budgets=effective["budgets"]
    frozen=session.policy.to_dict()
    downloads=budgets["downloads"]-len(rows)
    in_run=sum(row["run_id"]==session.run_id for row in rows)
    from datetime import datetime, timedelta, timezone

    from . import execution_session as native
    blockers=[]
    native_records=native.retained(session)
    if native_records:
        started=baseline_time(session)
        if rows:
            started=min([started,*[row["original_budget_started_utc"] for row in rows]],
                key=lambda value:datetime.fromisoformat(value.replace("Z","+00:00")))
        if datetime.now(timezone.utc)>=datetime.fromisoformat(started.replace("Z","+00:00"))+timedelta(seconds=budgets["seconds"]):
            blockers.append("original_deadline_expired")
        current=native.current(session)
        projection=current["execution"]["accounting"]
        if projection is None or projection["usage_unknown"] or projection["overrun"] or projection["remaining"]["tool_calls"]<1:
            blockers.append("native_run_allowance_unknown_or_exhausted")
        if current["usage"]["sources"]>=frozen["budgets"]["sources"]:
            blockers.append("source_budget_exhausted")
    return {"original_request_sha256":digest(base),"downloads":max(0,downloads),
        "bytes":max(0,budgets["bytes"]-sum(row["max_bytes"] for row in rows)),
        "run_attempts":max(0,frozen["continuation_limits"]["acquisition_attempts"]-in_run),
        "source_requests":max(0,budgets["source_requests"]-len({row["source_request_id"] for row in rows})),
        "blockers":blockers,"execution_authority":"not_established"}
