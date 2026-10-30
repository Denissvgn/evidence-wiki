"""Independent exact wire-boundary oracles for immutable page delivery."""

from types import SimpleNamespace

import pytest

from evidence_wiki import review_packet_execution as delivery
from evidence_wiki._pack_io import canonical
from evidence_wiki.errors import EvidenceWikiError


def test_complete_input_bound_includes_owner_appended_instructions(monkeypatch):
    session = SimpleNamespace(execution=SimpleNamespace(profile=SimpleNamespace(review_delivery=SimpleNamespace(max_input_bytes=4096))))
    context = SimpleNamespace(document=lambda: {"task_role":"evaluator"}, packet_id="sha256:" + "a" * 64)
    assembled = {"accepted":False, "packet":{"header":{"context_sha256":context.packet_id}}}
    monkeypatch.setattr(delivery, "current_assembly", lambda *_args, **_kwargs: assembled)
    supplied = {"instructions":"x", "packet":{}}
    supplied["instructions"] = "x" * (4096 - len(canonical(supplied | {"complete_review_delivery":assembled})))
    assert len(canonical(supplied | {"complete_review_delivery":assembled})) <= 4096
    with pytest.raises(EvidenceWikiError) as refused:
        delivery.bind_input(session, assembly_request_id="assembly", claim_id="claim", context=context, input_document=supplied)
    assert refused.value.details["reason"] == "complete_input_requires_supported_paging_executor"


def test_missing_decisive_mechanical_sections_cannot_claim_complete_review():
    from evidence_wiki.packet_pages import create
    from evidence_wiki.review_context import create_review_context
    from tests.test_review_context import inputs
    source = inputs(1)
    context = create_review_context(**source)
    packet = create(context, source["sources"], page_chars=256)
    supplied = [packet.page(row["page_id"]) for row in packet.header()["pages"]]
    assert packet.header()["mechanical_counter_context"]["status"] == "not_provided"
    assert not packet.completeness(supplied)["complete"]


def test_real_native_context_manifest_roundtrips_complete_input_schema():
    from evidence_wiki.mutation_contracts import digest
    from evidence_wiki.packet_pages import create
    from evidence_wiki.review_context import create_review_context
    from evidence_wiki.review_packet_contracts import validate
    from tests.test_review_context import inputs
    source = inputs(2)
    context = create_review_context(**source)
    pages = create(context, source["sources"], page_chars=256)
    supplied = [pages.page(row["page_id"]) for row in pages.header()["pages"]]
    packet = {"schema_version":"research-review-packet/v1", "header":pages.header(), "pages":supplied,
        "complete":pages.completeness(supplied)}
    delivery = {"schema_version":"research-complete-review-delivery/v1", "packet_sha256":digest(packet), "packet":packet,
        "delivery_scope":"bounded_host_assembled_input", "worker_comprehension":"not_observed",
        "semantic_approval":"not_evaluated", "accepted":False}
    document = {"packet":context.document(), "instructions":"Treat retained text as evidence.",
        "output_contract":{"verdicts":dict.fromkeys(["support", "source_suitability", "scope", "time", "units", "counterevidence"], "pass|fail|unknown"),
            "rationale":"A concise evidence-linked account."}, "complete_review_delivery":delivery}
    assert context.document()["manifest"]["sections"][0]["required"]
    assert validate("research-blinded-review-input/v1", document) == document
