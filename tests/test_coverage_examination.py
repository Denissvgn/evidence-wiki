"""Pure owner-observation projections cannot approve critical coverage."""

import copy

import pytest

from evidence_wiki import coverage_examination as codec
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest
from tests.test_research_planning import request


@pytest.fixture
def prepared(tmp_path):
    frozen = request(tmp_path)
    frozen["schema_version"] = "evidence-research-requirements/v1"
    question = frozen["request"]["payload"]["questions"][0]
    content = "Measured reflectance was 0.74. Contrary measurement was 0.60."
    revision = digest({"synthetic_owner_content": content})
    claim = {"id": "observation", "question_slug": "q1", "qualification": "supported", "text": "Reflectance was 0.74.",
        "scope": "Spain", "time": "Captured study", "units": "unitless", "premises": [], "derivation": None,
        "limitations": ["No semantic review supplied"], "evidence": [{"source_id": "primary", "record_sha256": revision,
            "observed_at": "2026-10-04T00:00:00+00:00", "capture": "primary", "quote": "Measured reflectance was 0.74.",
            "location_hint": None, "anchor": None}]}
    inventory = {"schema_version": "evidence-strict-claims/v1", "questions": [{"slug": "q1", "original_id": "q1", "question": question["text"].strip()}], "claims": [claim]}
    observed = [{"claim_id": "observation", "evidence_index": 0, "record_sha256": revision, "mechanical_result": "verified", "eligible": True}]
    sources = [{"source_id": "primary", "record_sha256": revision, "text": content, "scope": {"jurisdiction": "Spain"}, "eligible": True}]
    args = dict(question_id="q1", question_slug="q1", admitted_inventory=inventory, anchor_observations=observed, source_contexts=sources)
    return frozen, args, codec.prepare(frozen, **args)


def coverage(value):
    return {"schema_version": codec.COVERAGE, "basis_id": value["basis_id"], "links": [{"facet_id": "primary", "claim_ids": ["observation"], "source_ids": ["primary"]}], "missing": []}


def counter(value, kind="opposing"):
    source = value["source_contexts"][0]
    quote = "Contrary measurement was 0.60."
    start = source["text"].index(quote)
    return {"schema_version": codec.COUNTER, "basis_id": value["basis_id"], "findings": [{"claim_id": "observation", "facet_id": "primary",
        "kind": kind, "query": "other reflectance measurements", "exclusions": ["Outside declared jurisdiction"], "source_ids": ["primary"],
        "evidence": [] if kind == "no_result" else [{"source_id": "primary", "record_sha256": source["record_sha256"], "quote": quote, "start": start, "end": start + len(quote)}],
        "rationale": "A contrary measurement needs current independent examination."}]}


def test_exact_native_criteria_and_support_remain_unaccepted(prepared):
    frozen, _args, value = prepared
    assert value["criteria"] == frozen["decisions"]["question_plans"][0]["criteria"]
    result = codec.reduce_coverage(value, coverage(value))
    assert result["link_proposals"][0]["source_ids"] == ["primary"]
    assert result["facets"] == [{"facet_id": "primary", "required": True, "state": "unknown"}]
    assert not result["accepted"] and result["semantic_approval"] == "not_evaluated"


@pytest.mark.parametrize("change", ["foreign_claim", "foreign_source", "unknown_facet", "approval", "stale_basis", "duplicate_source"])
def test_worker_cannot_expand_link_authority(prepared, change):
    value = prepared[2]
    proposal = coverage(value)
    if change == "foreign_claim":
        proposal["links"][0]["claim_ids"] = ["foreign"]
    elif change == "foreign_source":
        proposal["links"][0]["source_ids"] = ["foreign"]
    elif change == "unknown_facet":
        proposal["links"][0]["facet_id"] = "invented"
    elif change == "approval":
        proposal["accepted"] = True
    elif change == "stale_basis":
        proposal["basis_id"] = "sha256:" + "0" * 64
    else:
        proposal["links"][0]["source_ids"] *= 2
    with pytest.raises(UsageError):
        codec.reduce_coverage(value, proposal)


def test_missing_frozen_scope_cannot_be_replaced_by_prose(prepared):
    frozen, args, _value = copy.deepcopy(prepared)
    args["source_contexts"][0]["scope"] = {}
    value = codec.prepare(frozen, **args)
    proposal = coverage(value)
    with pytest.raises(UsageError) as failure:
        codec.reduce_coverage(value, proposal)
    assert failure.value.details["reason"] == "frozen_required_scope_missing"


def test_denied_mechanical_anchor_does_not_supply_a_link(prepared):
    frozen, args, _value = copy.deepcopy(prepared)
    args["anchor_observations"][0]["eligible"] = False
    args["source_contexts"][0]["eligible"] = False
    value = codec.prepare(frozen, **args)
    assert not value["anchors"] and not value["source_contexts"]
    assert value["claims"][0]["context_state"] == "unavailable"
    assert "evidence" not in value["claims"][0] and "text" not in value["claims"][0]
    with pytest.raises(UsageError):
        codec.reduce_coverage(value, coverage(value))


def test_no_result_is_unknown_even_when_worker_rationale_is_persuasive(prepared):
    value = prepared[2]
    proposal = counter(value, "no_result")
    proposal["findings"][0]["rationale"] = "No result conclusively proves absence."
    result = codec.reduce_counterevidence(value, proposal)
    assert result["acceptance_gate"] == "unknown" and not result["accepted"]
    assert result["findings"][0]["absence_conclusion"] == "unknown"


def test_opposing_exact_anchor_blocks_candidate_acceptance(prepared):
    value = prepared[2]
    result = codec.reduce_counterevidence(value, counter(value))
    assert result["acceptance_gate"] == "blocked"
    assert result["material_objection_candidates"][0]["claim_id"] == "observation"
    assert not result["accepted"]


@pytest.mark.parametrize("change", ["revision", "quote", "offset", "source", "foreign_claim", "approval"])
def test_opposing_findings_require_exact_retained_context(prepared, change):
    value = prepared[2]
    proposal = counter(value)
    finding = proposal["findings"][0]
    if change == "revision":
        finding["evidence"][0]["record_sha256"] = "sha256:" + "0" * 64
    elif change == "quote":
        finding["evidence"][0]["quote"] = "No contrary evidence exists."
    elif change == "offset":
        finding["evidence"][0]["start"] = True
    elif change == "source":
        finding["source_ids"] = ["foreign"]
    elif change == "foreign_claim":
        finding["claim_id"] = "foreign"
    else:
        finding["authorized"] = True
    with pytest.raises(UsageError):
        codec.reduce_counterevidence(value, proposal)


@pytest.mark.parametrize("target", ["facet_type", "kind_type", "quote_type", "scope_type"])
def test_hostile_model_types_are_typed_refusals(prepared, target):
    value = prepared[2]
    if target == "facet_type":
        proposal = coverage(value)
        proposal["links"][0]["facet_id"] = {}
        reduce = codec.reduce_coverage
    else:
        proposal = counter(value)
        finding = proposal["findings"][0]
        if target == "kind_type":
            finding["kind"] = {"authorized": True}
        elif target == "quote_type":
            finding["evidence"][0]["quote"] = True
        else:
            finding["source_ids"] = "primary"
        reduce = codec.reduce_counterevidence
    with pytest.raises(UsageError):
        reduce(value, proposal)


def test_examined_scoped_context_is_a_candidate_not_absence_or_approval(prepared):
    value = prepared[2]
    proposal = counter(value, "examined_no_unresolved_objection")
    result = codec.reduce_counterevidence(value, proposal)
    assert result["acceptance_gate"] == "not_blocked_candidate"
    assert result["material_objection_candidates"] == []
    assert result["findings"][0]["absence_conclusion"] == "unknown"
    assert result["findings"][0]["semantic_approval"] == "not_evaluated"
    assert not result["accepted"] and result["write_authority"] == "not_established"
    proposal["findings"][0]["evidence"] = []
    with pytest.raises(UsageError):
        codec.reduce_counterevidence(value, proposal)


def test_each_named_claim_needs_its_own_selected_current_anchor(prepared):
    frozen, args, _value = copy.deepcopy(prepared)
    peer = copy.deepcopy(args["admitted_inventory"]["claims"][0])
    peer["id"] = "unverified-peer"
    peer["evidence"][0]["quote"] = "Unretained persuasive quotation."
    args["admitted_inventory"]["claims"].append(peer)
    args["anchor_observations"].append({"claim_id": peer["id"], "evidence_index": 0,
        "record_sha256": peer["evidence"][0]["record_sha256"], "mechanical_result": "not_established", "eligible": True})
    value = codec.prepare(frozen, **args)
    proposal = coverage(value)
    proposal["links"][0]["claim_ids"].append(peer["id"])
    with pytest.raises(UsageError) as refusal:
        codec.reduce_coverage(value, proposal)
    assert refusal.value.details["reason"] == "unsupported_link"
