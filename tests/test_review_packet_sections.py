"""Pure association and blinding checks are not owner authentication proof."""

import copy
import json

import pytest

from evidence_wiki import review_packet_sections as sections
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.review_context import create_review_context
from tests.test_coverage_examination import prepared as counter_prepared
from tests.test_review_context import inputs


@pytest.fixture
def case():
    values = inputs(1)
    context = create_review_context(**values)
    claim = values["owner_snapshot"]["claim"]
    source = values["sources"][0]
    hashed = "sha256:" + "a" * 64
    evidence = {"evidence_index": 0, "source_id": source["source_id"], "record_sha256": source["record_sha256"],
        "location_hint": "page 1", "anchor_pointer": None, "form": "quote", "result": "verified", "reason": None,
        "mechanical": {"result": "verified", "form": "quote", "match_type": "exact", "occurrence_count": 1,
            "global_occurrence_count": 1, "policy": "exact"},
        "current_usage": {"eligible": True, "source_revision": hashed, "complete": True}}
    diagnosed = {"schema_version": "evidence-claim-diagnostics/v1", "proposal_sha256": hashed,
        "basis_id": hashed, "observed_at": "2026-10-01T00:00:00Z", "original_ids": ["private-original"],
        "structural_result": "valid", "semantic_approval": "not_evaluated", "accepted": False,
        "published": False, "bounds": {"claims": 100, "output_bytes": 1048576},
        "claims": [{"claim_id": claim["id"], "question_slug": claim["question_slug"], "original_id": "private-original",
            "qualification": "supported", **{key: claim[key] for key in ("scope", "time", "units", "premises")},
            "evidence": [evidence], "mechanical_result": "verified", "semantic_approval": "not_evaluated", "accepted": False}]}
    counter = {"source_id": source["source_id"], "record_sha256": source["record_sha256"],
        "start": 0, "end": 24, "quote": source["content"][:24], "truncated": False, "search_ref": hashed}
    return context, values["sources"], diagnosed, counter


def test_exact_associations_preserve_blinding_and_unknown_counter_completeness(case):
    context, sources, diagnostics, counter = case
    visible, binding = sections.create(context, sources, diagnostics, [counter])
    assert visible["mechanical"][0]["claim_id"] == "claim-1"
    assert visible["counter_material"][0]["source_id"] == "source-1"
    assert visible["counter_search_completeness"] == "not_established"
    assert not visible["accepted"]
    encoded = json.dumps(visible)
    for secret in ("private-original", "private-question", "private-generator", "search_ref", "prior_verdict", "hidden-key"):
        assert secret not in encoded
    assert binding["search_refs"] == [counter["search_ref"]]


def test_mechanical_and_counter_material_are_required_exact_pages(case):
    from evidence_wiki.packet_pages import create as paged
    context, sources, diagnostics, counter = case
    visible, _binding = sections.create(context, sources, diagnostics, [counter])
    handle = paged(context, sources, page_chars=256, sections=visible)
    descriptors = handle.header()["pages"]
    assert any(row.get("section_id") == "mechanical-counter" for row in descriptors)
    pages = [handle.page(row["page_id"]) for row in descriptors]
    assert not handle.completeness(pages)["complete"]
    assert "counter_context_unavailable" in handle.header()["limitations"]
    assert not handle.completeness([page for page in pages if page.get("section_id") != "mechanical-counter"])["complete"]
    restored = json.loads("".join(page["text"] for page in pages if page.get("section_id") == "mechanical-counter"))
    assert restored == visible


@pytest.mark.parametrize("change", ["hash", "range", "quote", "foreign", "authority"])
def test_counter_labels_cannot_replace_exact_current_material(case, change):
    context, sources, diagnostics, counter = case
    altered = copy.deepcopy(counter)
    if change == "hash":
        altered["record_sha256"] = "sha256:" + "0" * 64
    elif change == "range":
        altered["end"] += 1
    elif change == "quote":
        altered["quote"] = "An invented opposing quotation."
    elif change == "foreign":
        altered["source_id"] = "foreign-source"
    else:
        altered["authenticated"] = True
    with pytest.raises(UsageError):
        sections.create(context, sources, diagnostics, [altered])


@pytest.mark.parametrize("change", ["missing", "duplicate", "scope", "association"])
def test_native_diagnostic_associations_must_cover_exact_subject(case, change):
    context, sources, diagnostics, _counter = case
    altered = copy.deepcopy(diagnostics)
    if change == "missing":
        altered["claims"] = []
    elif change == "duplicate":
        altered["claims"] *= 2
    elif change == "scope":
        altered["claims"][0]["scope"] = "Different context"
    else:
        altered["claims"][0]["evidence"][0]["source_id"] = "foreign-source"
    with pytest.raises(UsageError):
        sections.create(context, sources, altered)


@pytest.fixture
def counter_case(case, tmp_path):
    from evidence_wiki.coverage_examination import prepare
    context, sources, diagnostics, _counter = case
    frozen, args, _instruction = counter_prepared.__wrapped__(tmp_path)
    values = inputs(1)
    claim = {key: value for key, value in values["owner_snapshot"]["claim"].items()
        if key not in {"desired_outcome", "generator"}}
    slug = claim["question_slug"]
    question = frozen["request"]["payload"]["questions"][0]
    inventory = {"schema_version": "evidence-strict-claims/v1", "claims": [claim],
        "questions": [{"slug": slug, "original_id": question["id"], "question": question["text"].strip()}]}
    alternative = {"source_id": "private-other-study", "record_sha256": "sha256:" + "d" * 64,
        "content": "A complete independent alternative source.", "metadata": {"title": "Alternative retained study", "prior_verdict": "pass"}}
    sources = [*sources, alternative]
    instruction = prepare(frozen, question_id=question["id"], question_slug=slug, admitted_inventory=inventory,
        anchor_observations=[{"claim_id": claim["id"], "evidence_index": 0, "record_sha256": sources[0]["record_sha256"],
            "mechanical_result": "verified", "eligible": True}],
        source_contexts=[{"source_id": row["source_id"], "record_sha256": row["record_sha256"], "text": row["content"],
            "scope": {"jurisdiction": "Spain"}, "eligible": True} for row in sources])
    return context, sources, diagnostics, instruction


def test_native_counter_scope_and_alternatives_are_blinded_without_prior_findings(counter_case):
    context, sources, diagnostics, instruction = counter_case
    visible, retained = sections.create(context, sources, diagnostics, counter_instruction=instruction)
    counter = visible["counter_context"]
    assert counter["status"] == "current_native_instruction"
    assert counter["scope"] == instruction["scope"]
    assert counter["criteria"][0]["facet_id"] == "facet-1"
    assert counter["alternative_sources"][0]["source_id"] == "alternative-1"
    assert counter["alternative_sources"][0]["content"] == sources[1]["content"]
    assert retained["counter_instruction_ref"] == digest(instruction)
    assert "private-other-study" not in json.dumps(visible)
    assert "prior_verdict" not in json.dumps(visible)
    assert visible["counter_search_completeness"] == "not_established"


@pytest.mark.parametrize("change", ["claim", "alternative_body", "primary_body"])
def test_rehashed_counter_instruction_cannot_relabel_changed_current_material(counter_case, change):
    context, sources, diagnostics, instruction = copy.deepcopy(counter_case)
    if change == "claim":
        instruction["claims"][0]["text"] = "A different substantive claim."
    else:
        instruction["source_contexts"][1 if change == "alternative_body" else 0]["text"] += " Changed decisive content."
    instruction["basis_id"] = digest({key: value for key, value in instruction.items() if key != "basis_id"})
    with pytest.raises(UsageError):
        sections.create(context, sources, diagnostics, counter_instruction=instruction)
