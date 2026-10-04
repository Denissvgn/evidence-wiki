"""Retain canonical run reservations before one bounded native web transport."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from evidence_wiki import execution_session as native
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest, validate_request_id
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_session import ResearchSession

from . import acquisition_operations as observations


def refuse(reason):
    raise UsageError("ACQUISITION_DISPATCH_REFUSED", "Acquisition dispatch refused.", details={"reason": reason})


def dispatch(session, *, request_id, source_request_id, expected_request_basis):
    """Spend the existing authenticated run/tool and native provider budgets once."""
    if type(session) is not ResearchSession or session.execution is None:
        refuse("bound_execution_session_required")
    request_id = validate_request_id(request_id)
    source_request_id = validate_request_id(source_request_id)
    if (type(expected_request_basis) is not str or len(expected_request_basis) != 71
            or not expected_request_basis.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in expected_request_basis[7:])):
        refuse("request_basis_invalid")
    session._check("acquire", write=True)
    session.context.require_grant("acquisition_operations.dispatch.web")
    frozen = session.policy.to_dict()
    if "acquire" not in frozen["allowed_effects"] or frozen["host_capability_refs"]["acquire"] is None:
        refuse("acquisition_ungranted")
    arguments = {"source_request_id": source_request_id, "expected_request_basis": expected_request_basis,
        "session_binding_id": digest(session.binding), "provider": "web"}
    identifier = native.child_id(request_id, "web-transport")
    from .acquisition_budget import baseline_time, history, original_request
    with native.gate(session):
        native._begin(session)
        original,_effective=original_request(session)
        original_id=digest(original)
        original=original["budgets"]
        started=datetime.fromisoformat(baseline_time(session).replace("Z","+00:00"))
        previous=history(session,original_id)
        if previous:
            started=min(started,*[datetime.fromisoformat(row["original_budget_started_utc"].replace("Z","+00:00")) for row in previous])
        try:
            original_deadline=started+timedelta(seconds=original["seconds"])
        except (OverflowError,TypeError):
            refuse("original_deadline_invalid")
        end=min(native.deadline(session),original_deadline)

    def invoke(control):
        # _native_call has left every native coordination gate before this
        # callback. Short reservation locks end before transport is invoked.
        session._check("acquire", write=True)
        observed, view, record = observations._request(session.root, source_request_id)
        if digest(record) != expected_request_basis or record["status"] != "open":
            refuse("request_basis_changed")
        fetch, transport = owner("fetch_sources"), owner("_acquisition_transport")
        configured = fetch.acquisition_context(session.root, view.config, "web", 1, run_id=session.run_id)
        web = fetch.web_config(configured["acquisition"])
        source_url = record["query_or_identifier"]
        transport.validate_https_url(source_url, allowed_domains=web["allowed_domains"], resolve_hostnames=False)
        records = native.retained(session)
        attempts = [row for row in records if row["kind"] == "execution_native"
            and row["data"].get("operation") == "acquisition.web_transport"]
        if len(attempts) > frozen["continuation_limits"]["acquisition_attempts"]:
            refuse("acquisition_attempt_limit")
        max_bytes = min(web["max_download_bytes"], 49_152)
        view.finish()
        observations.lifecycle._guard(session.root, observed["workspace_revision"])
        run = configured["run_budget"]
        if run is None or run["run_id"] != session.run_id:
            refuse("current_canonical_run_required")
        from .acquisition_budget import reserve
        budget=reserve(session,identifier,record,configured,max_bytes)
        max_bytes=budget["max_bytes"]
        accounting=owner("_provider_accounting")
        reservation=accounting.reserve(run["path"].parent,"web",1,
            ledger_filename=fetch.REGISTERED_LEDGER_FILENAME,lock_filename=fetch.REGISTERED_LEDGER_LOCK_FILENAME,
            schema_version=fetch.ACCOUNTING_SCHEMA_VERSION,per_run_max=budget["caps"]["per_run_downloads"],
            extra_fields={"event_type":"acquisition_transport","source_request_id":source_request_id,
                "session_binding_id":digest(session.binding),"attempt_id":identifier,"reserved_max_bytes":max_bytes})
        control.checkpoint("before_acquisition_transport")
        remaining = (end - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            refuse("deadline_expired")
        from .host_capabilities import scope_allows
        _base,effective=original_request(session)
        declared=effective["authority"]["source_scope"]
        scopes={"scope":[{"kind":"uri_prefix","value":value} for value in declared if "://" in value]}
        def guard_url(url):
            if not scope_allows(scopes,url):
                refuse("redirect_outside_original_scope")
        result = transport.execute_planned_request(SimpleNamespace(url=source_url, method="GET", headers=(), body=None, timeout_hint=None),
            allowed_domains=web["allowed_domains"], credentials=[], env={}, timeout=min(30, remaining),
            max_bytes=max_bytes, expected_content_types=["text/plain", "text/html", "application/json"], _url_guard=guard_url)
        if type(result) is not transport.DownloadResult:
            refuse("native_download_result_required")
        result = transport.validate_download_result(result, source_url=source_url,
            allowed_domains=web["allowed_domains"], max_bytes=max_bytes,
            expected_content_types=["text/plain", "text/html", "application/json"], resolve_hostnames=False)
        guard_url(result.final_url)
        for redirected in result.redirect_chain:
            guard_url(redirected)
        control.checkpoint("after_acquisition_transport")
        # This is a retained transport result, not captured or accepted evidence.
        from .acquisition_contracts import checked_transport
        return checked_transport({"schema_version": "evidence-acquisition-transport-result/v1", "source_request_id": source_request_id,
            "request_basis": expected_request_basis, "provider": "web", "byte_count": result.byte_count,
            "content_sha256": result.checksum, "content_base64": base64.b64encode(result.content).decode("ascii"),
            "final_url": result.final_url, "content_type": result.content_type, "tls_verified": result.tls_verified,
            "reserved_provider_calls": reservation.per_run_used, "source_captured": False,
            "source_accepted": False, "rights_inferred": False})

    return native._native_call(session, identifier, "acquisition.web_transport", arguments, invoke,
        recover=None, effect_deadline=end.isoformat(), effect_precondition=lambda: session._check("acquire", write=True))
