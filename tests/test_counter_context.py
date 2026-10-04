"""Counterevidence payloads preserve native reviewer blinding and exact aliases."""

import copy
import json

import pytest

from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.review_context import create_review_context
from tests.test_coverage_examination import prepared as prepared


@pytest.fixture
def examination(prepared, monkeypatch):
    from evidence_wiki import counter_context as codec
    instruction = prepared[2]
    claim = instruction["claims"][0]
    source = instruction["source_contexts"][0]
    snapshot = {"claim": claim, "question": {"slug": "q1", "original_id": "q1", "question": "What observation was retained?"},
        "premises": [], "source_revisions": {source["source_id"]: source["record_sha256"]},
        "metadata": {"generator_identity": "private-generator"}, "policy": {"signing_key": "private-key"}}
    rubric = {"id": "native", "revision": "1", "criteria": dict.fromkeys(("support", "source_suitability", "scope", "time", "units", "counterevidence"), "Examine independent current evidence.")}
    context = create_review_context(context_id="counter-context", task_role="critic", basis_id=digest(snapshot),
        owner_snapshot=snapshot, rubric=rubric, sources=[{"source_id": source["source_id"], "record_sha256": source["record_sha256"],
            "content": source["text"], "metadata": {}}], order_seed=b"c" * 32)
    return codec, instruction, context


def test_counter_packet_has_no_parent_control_identity(examination):
    codec, instruction, context = examination
    rendered = json.dumps(codec.packet(instruction, context))
    for field in ("private-generator", "private-key", "owner_snapshot_sha256", "aliases", "order_seed", '"original_id": "q1"'):
        assert field not in rendered
    assert '"facet_id": "facet-1"' in rendered


def test_opposing_aliases_are_resolved_only_in_parent(examination):
    codec, instruction, context = examination
    source = context.document()["sources"][0]
    quote = "Contrary measurement was 0.60."
    start = source["content"].index(quote)
    proposal = {"schema_version": "evidence-counterevidence-proposal/v1", "basis_id": context.sha256,
        "findings": [{"claim_id": "claim-1", "facet_id": "facet-1", "kind": "opposing", "query": "contrary measurements",
            "exclusions": [], "source_ids": [source["source_id"]], "evidence": [{"source_id": source["source_id"],
                "record_sha256": source["record_sha256"], "quote": quote, "start": start, "end": start + len(quote)}], "rationale": "Examine this contrary passage."}]}
    result = codec.translate(instruction, context, proposal)
    assert result["acceptance_gate"] == "blocked" and not result["accepted"]
    assert result["findings"][0]["claim_id"] == "observation"


def test_changed_source_context_invalidates_retained_blinded_packet(examination):
    codec, instruction, context = examination
    changed = copy.deepcopy(instruction)
    changed["source_contexts"][0]["text"] += " Changed body."
    changed["basis_id"] = digest({key: value for key, value in changed.items() if key != "basis_id"})
    with pytest.raises(UsageError) as refused:
        codec.packet(changed, context)
    assert refused.value.details["reason"] == "critic_source_context_changed"
