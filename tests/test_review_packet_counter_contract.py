"""Actual codec-shaped counter input roundtrip, no owner authentication claim."""

from evidence_wiki.counter_context import packet as counter_packet
from evidence_wiki.packet_pages import create
from evidence_wiki.review_context import create_review_context
from evidence_wiki.review_packet_contracts import validate
from evidence_wiki.review_packet_sections import create as sections
from tests import test_review_packet_sections as cases
from tests.test_review_context import inputs


def test_closed_counter_input_roundtrips_actual_raw_context_sha(tmp_path):
    base = cases.case.__wrapped__()
    _context, sources, diagnostics, instruction = cases.counter_case.__wrapped__(base, tmp_path)
    values = inputs(1)
    values["task_role"] = "critic"
    context = create_review_context(**values)
    visible, _binding = sections(context, sources, diagnostics, counter_instruction=instruction)
    pages = create(context, sources[:1], sections=visible)
    header = pages.header()
    delivered = [pages.page(row["page_id"]) for row in header["pages"]]
    packet = {"schema_version": "research-review-packet/v1", "header": header, "pages": delivered,
        "complete": pages.completeness(delivered)}
    from evidence_wiki.mutation_contracts import digest
    value = {**counter_packet(instruction, context), "purpose": "counterevidence", "instructions": "Read exact material.",
        "output_contract": {"schema_version": "evidence-counterevidence-proposal/v1", "basis_id": context.sha256,
            "findings": "Closed cited findings only"},
        "complete_review_delivery": {"schema_version": "research-complete-review-delivery/v1", "packet_sha256": digest(packet),
            "packet": packet, "delivery_scope": "bounded_host_assembled_input", "worker_comprehension": "not_observed",
            "semantic_approval": "not_evaluated", "accepted": False}}
    assert validate("research-blinded-review-input/v1", value) == value
