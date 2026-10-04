"""Existing native journal records host page delivery, not worker understanding."""

import hashlib
import hmac

from evidence_wiki import execution_session as native
from evidence_wiki._pack_io import canonical
from evidence_wiki.mutation_contracts import digest, validate_request_id
from evidence_wiki.research_session import ResearchSession

from .packet_pages import refuse
from .review_page_operations import prepare


def assembled(session, *, request_id, claim_id, task_role="evaluator", page_chars=4096):
    """Assemble exact current pages under a finite native read reservation.

    The parent may dispatch this complete bounded packet only after pinning its
    SHA into an existing authenticated attempt. A delivery record alone is not
    proof that a worker consumed or understood any source.
    """
    if type(session) is not ResearchSession or session.execution is None:
        refuse("bound_execution_session_required")
    request_id = validate_request_id(request_id)
    session._check("advance", write=True)
    session.context.require_grant("review_page_session.assembled")
    with native.gate(session):
        document = native._begin(session)
        if document["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
            completed = any(row["kind"] == "execution_native_observation"
                and row["data"]["observation"]["attempt_id"] == request_id
                and row["data"]["error_code"] is None for row in native.retained(session))
            if not completed:
                refuse("packet_run_terminal")
        source_ids = [row["source_id"] for row in native.current(session)["execution"]["source_bindings"]]
    seed = hmac.new(session.execution._observer_key, b"review-page-order/v1\0" +
        canonical({"binding": digest(session.binding), "request_id": request_id}), hashlib.sha256).digest()
    arguments = {"claim_id": claim_id, "task_role": task_role, "page_chars": page_chars, "source_ids": source_ids}
    def invoke(_control):
        _control.checkpoint("before_packet_assembly")
        handle = prepare(session.root, claim_id=claim_id, context_id="context-" + seed.hex()[:32],
            task_role=task_role, page_chars=page_chars, source_ids=source_ids, _order_seed=seed)
        header = handle.header()
        delivered = []
        for item in header["pages"]:
            _control.checkpoint("before_required_page")
            page = handle.page(item["page_id"])
            _control.checkpoint("after_required_page")
            delivered.append(page)
            with native.gate(session):
                if native._begin(session)["state"]["current"] in native.owner("run_controller").TERMINAL_STATES:
                    refuse("packet_run_terminal")
                native.append(session, "execution_review_page_delivery", native.child_id(request_id, "page." + item["page_id"]),
                    {"claim_id": claim_id, "header_sha256": digest(header), "page_id": item["page_id"],
                        "page_sha256": digest(page), "delivery_scope": "host_assembly", "worker_consumption": "not_observed"})
        complete = handle.complete(delivered)
        _control.checkpoint("after_packet_assembly")
        if not complete["complete"]:
            refuse("decisive_context_incomplete")
        packet = {"schema_version": "research-review-packet/v1", "header": header, "pages": delivered, "complete": complete}
        delivery = getattr(session.execution.profile, "review_delivery", None)
        if len(canonical(packet)) > (196608 if delivery is None else delivery.max_packet_bytes):
            refuse("complete_packet_requires_supported_paging_executor")
        value = {"schema_version": "research-host-assembled-review/v1", "claim_id": claim_id,
            "packet_sha256": digest(packet), "packet": packet, "worker_consumption": "not_observed",
            "semantic_approval": "not_evaluated", "accepted": False}
        from .review_packet_contracts import validate
        return validate("research-host-assembled-review/v1", value)
    value = native._native_call(session, request_id, "review_pages.host_assembly", arguments, invoke, recover=None)
    # A historical body is private evidence, never a fresh source-access grant.
    handle = prepare(session.root, claim_id=claim_id, context_id="context-" + seed.hex()[:32],
        task_role=task_role, page_chars=page_chars, source_ids=source_ids, _order_seed=seed)
    if handle.header() != value["packet"]["header"]:
        refuse("historical_packet_not_current")
    if handle.complete(value["packet"]["pages"]) != value["packet"]["complete"]:
        refuse("historical_decisive_pages_not_current")
    from .review_packet_contracts import validate
    return validate("research-host-assembled-review/v1", value)
