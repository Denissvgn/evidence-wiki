"""Exact page delivery does not imply semantic or native release approval."""

import json

import pytest

from evidence_wiki import packet_pages as codec
from evidence_wiki.errors import UsageError
from evidence_wiki.review_context import create_review_context
from tests.test_calculation_context import owner_case as owner_case
from tests.test_calculation_context import projected as projected
from tests.test_review_context import inputs


@pytest.fixture
def pages():
    values = inputs(2)
    for source in values["sources"]:
        source["source_revision"] = values["owner_snapshot"]["source_revisions"][source["source_id"]]
        source["content"] *= 25
    context = create_review_context(**values)
    return context, codec.create(context, values["sources"], page_chars=256), values


def test_complete_exact_pages_keep_unknown_semantics_and_blinding(pages):
    _context, packet, _values = pages
    header = packet.header()
    all_pages = [packet.page(row["page_id"]) for row in header["pages"]]
    assert not packet.completeness(all_pages)["complete"]
    assert "mechanical_counter_context_unavailable" in header["limitations"]
    assert not packet.completeness(all_pages)["accepted"]
    assert not header["complete_evidence_delivered"]
    visible = json.dumps([header, *all_pages])
    for hidden in ("private-generator-source", "private-generator-claim", "hidden-key", "order_seed", "aliases", "owner_snapshot_sha256"):
        assert hidden not in visible


def test_one_missing_page_remains_incomplete(pages):
    _context, packet, _values = pages
    all_pages = [packet.page(row["page_id"]) for row in packet.header()["pages"]]
    result = packet.completeness(all_pages[:-1])
    assert not result["complete"] and result["missing_page_ids"]


def test_source_provenance_survives_pagination_without_parent_identity(pages):
    context, packet, _values = pages
    native_sources = {row["source_id"]: row for row in context.document()["sources"]}
    assert packet.header()["sources"]
    for descriptor in packet.header()["sources"]:
        assert descriptor == {key: native_sources[descriptor["source_id"]][key]
            for key in ("source_id", "source_revision", "record_sha256", "metadata")}
        assert "content" not in descriptor
    assert all(row["source_id"].startswith("source-") for row in packet.header()["sources"])


@pytest.mark.parametrize("alter", ["text", "revision", "range", "duplicate"])
def test_changed_and_duplicate_pages_cannot_complete_context(pages, alter):
    _context, packet, _values = pages
    all_pages = [packet.page(row["page_id"]) for row in packet.header()["pages"]]
    if alter == "text":
        all_pages[0]["text"] += " Added words."
    elif alter == "revision":
        all_pages[0]["record_sha256"] = "sha256:" + "0" * 64
    elif alter == "range":
        all_pages[0]["end"] += 1
    else:
        all_pages.append(all_pages[0])
    with pytest.raises(UsageError):
        packet.completeness(all_pages)


def test_whole_source_omission_is_recovered_only_by_exact_owner_bytes():
    values = inputs(4)
    for source in values["sources"]:
        source["source_revision"] = values["owner_snapshot"]["source_revisions"][source["source_id"]]
        source["content"] = "Retained decisive source context. " * 400
    context = create_review_context(**values, max_bytes=8192)
    assert not context.document()["manifest"]["context_complete"]
    packet = codec.create(context, values["sources"], page_chars=4096)
    assert not packet.completeness([packet.page(row["page_id"]) for row in packet.header()["pages"]])["complete"]
    assert not packet.header()["missing_source_ids"]
    values["sources"][0]["content"] += " Changed owner body."
    with pytest.raises(UsageError):
        codec.create(context, values["sources"], page_chars=4096)


def test_native_calculation_v2_is_decisive_required_paged_section(projected):
    _review, context, snapshot, _rubric, sources = projected
    for source in sources:
        source["source_revision"] = snapshot["source_revisions"][source["source_id"]]
    packet = codec.create(context, sources, page_chars=256)
    header = packet.header()
    assert header["computation"]["required"]
    computation_pages = [row for row in header["pages"] if row.get("section_id") == "computation"]
    assert computation_pages and all(row["required"] for row in computation_pages)
    all_pages = [packet.page(row["page_id"]) for row in header["pages"]]
    assert not packet.completeness(all_pages)["complete"]
    assert "mechanical_counter_context_unavailable" in header["limitations"]
    missing = [page for page in all_pages if page.get("section_id") != "computation"]
    assert not packet.completeness(missing)["complete"]
    restored = json.loads("".join(page["text"] for page in all_pages if page.get("section_id") == "computation"))
    assert restored == context.document()["computation"]
