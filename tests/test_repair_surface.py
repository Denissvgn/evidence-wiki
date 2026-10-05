"""Inert scoped merge constraints do not confer current source rights."""

import copy
import json

import pytest

from evidence_wiki.agent_execution import ExecutionError
from evidence_wiki.repair_surface import merge_inventory, require_current_dependencies, worker_inventory
from tests.test_repair_progress import inventory


def before_case():
    before = inventory()
    peer = copy.deepcopy(before['claims'][0])
    peer.update(id='unrelated-peer', text='PRIVATE-PEER-DETAIL', premises=[], qualification='supported', derivation=None)
    peer['evidence'][0].update(source_id='private-source', record_sha256='sha256:'+'d'*64, quote='PRIVATE-PEER-DETAIL')
    before['claims'].append(peer)
    target = before['claims'][0]
    decisive = copy.deepcopy(target)
    decisive.update(id='decisive-premise', premises=[], qualification='supported', derivation=None)
    before['claims'].insert(0, decisive)
    target.update(qualification='inference', premises=['decisive-premise'], derivation='Retain the explicit premise.')
    return before, target['id'], target['premises']


def test_worker_inventory_omits_unrelated_peer_but_parent_retains_it():
    before, target, premises = before_case()
    surface = worker_inventory(before, target_claim_id=target, premise_ids=premises)
    assert 'PRIVATE-PEER-DETAIL' not in json.dumps(surface)
    assert before['claims'][-1]['text'] == 'PRIVATE-PEER-DETAIL'
    assert target in {claim['id'] for claim in surface['claims']}


def test_missing_peer_current_packet_refuses_before_any_worker_surface_permission():
    before, target, premises = before_case()
    packets = [{'source_id': e['source_id'], 'record_sha256': e['record_sha256']} for claim in before['claims'][:-1] for e in claim['evidence']]
    with pytest.raises(ExecutionError, match='dependency_not_current_permitted'):
        require_current_dependencies(before, packets)


def test_target_only_proposal_merges_exact_parent_peers():
    before, target, premises = before_case()
    surface = worker_inventory(before, target_claim_id=target, premise_ids=premises)
    proposed = copy.deepcopy(surface)
    next(claim for claim in proposed['claims'] if claim['id'] == target)['scope'] += ' Corrected.'
    result = merge_inventory(before, before, proposed, target_claim_id=target, worker_before=surface)
    assert [c for c in result['claims'] if c['id'] != target] == [c for c in before['claims'] if c['id'] != target]
    assert next(c['scope'] for c in result['claims'] if c['id']==target) != next(c['scope'] for c in before['claims'] if c['id']==target)


@pytest.mark.parametrize('change', ['peer','extra','question','format','current'])
def test_untrusted_repair_cannot_expand_or_rewrite_parent_surface(change):
    before, target, premises = before_case()
    surface = worker_inventory(before, target_claim_id=target, premise_ids=premises)
    proposed, current = copy.deepcopy(surface), copy.deepcopy(before)
    if change == 'peer':
        next(c for c in proposed['claims'] if c['id']!=target)['text'] += ' Unauthorized.'
    elif change == 'extra':
        proposed['claims'].append(copy.deepcopy(before['claims'][-1]))
    elif change == 'question':
        proposed['questions'][0]['question'] += ' Easier.'
    elif change == 'format':
        proposed['schema_version'] = 'evidence-strict-claims/v2'
    else:
        current['claims'][-1]['units'] += ' Changed.'
    with pytest.raises((ExecutionError, ValueError)):
        merge_inventory(before, current, proposed, target_claim_id=target, worker_before=surface)


def test_deterministic_proposal_projection_retains_actual_raw_commitment():
    from evidence_wiki.mutation_contracts import digest
    from evidence_wiki.repair_surface import project_proposal
    before, target, premises = before_case()
    surface = worker_inventory(before, target_claim_id=target, premise_ids=premises)
    proposal = {"schema_version": "evidence-draft-proposal/v1", "inventory": copy.deepcopy(surface),
        "originals": [{"original_id": question["original_id"], "text": question["question"],
            "question_slugs": [question["slug"]]} for question in surface["questions"]]}
    next(claim for claim in proposal["inventory"]["claims"] if claim["id"] == target)["scope"] += " Corrected."
    saved = copy.deepcopy(proposal)
    projected, proof = project_proposal(before, before, proposal, target_claim_id=target, worker_before=surface)
    assert proposal == saved
    assert proof["raw_proposal_sha256"] == digest(proposal)
    assert proof["merged_proposal_sha256"] == digest(projected)
    assert proof["raw_proposal_sha256"] != proof["merged_proposal_sha256"]
    assert next(c for c in projected["inventory"]["claims"] if c["id"] == "unrelated-peer") == before["claims"][-1]
    tampered = copy.deepcopy(proposal)
    tampered["inventory"]["claims"][0]["units"] += " Forged peer."
    with pytest.raises(ExecutionError, match="peer_changed"):
        project_proposal(before, before, tampered, target_claim_id=target, worker_before=surface)


def test_scoped_questions_are_consistent_and_parent_keeps_unrelated_question():
    before, target, premises = before_case()
    question = copy.deepcopy(before["questions"][0])
    question.update(slug="unrelated-question", original_id="unrelated", question="An unrelated original question.")
    before["questions"].append(question)
    before["claims"][-1]["question_slug"] = question["slug"]
    surface = worker_inventory(before, target_claim_id=target, premise_ids=premises)
    assert question not in surface["questions"]
    proposed = copy.deepcopy(surface)
    next(c for c in proposed["claims"] if c["id"] == target)["scope"] += " Corrected."
    merged = merge_inventory(before, before, proposed, target_claim_id=target, worker_before=surface)
    assert merged["questions"] == before["questions"]
    assert merged["claims"][-1] == before["claims"][-1]
