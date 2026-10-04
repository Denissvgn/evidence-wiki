"""Inert suggestions cannot expand canonical scope or execute proposed effects."""

import copy

import pytest

from evidence_wiki.errors import UsageError
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.work_cycle_contracts import (
    CycleInvalid,
    CycleRequest,
    WorkCandidate,
    WorkProposal,
    decode_proposal,
    encode,
    select,
)
from tests.test_run_policy import policy_document

HASH="sha256:"+"a"*64
OTHER="sha256:"+"b"*64


def document():
    return {"schema_version":"research-work-proposal/v1","basis_id":HASH,"candidates":[{
        "candidate_id":"candidate","operation":"generate","original_id":"question-a","claim_id":None,
        "evidence_refs":[{"source_id":"source-a","source_binding_id":HASH,"excerpt_ref":OTHER}],
        "rationale":"A proposal, never a tool invocation."}]}


def choose(value,*,basis_id=HASH,policy=None,action=None,refs=None):
    proposal=WorkProposal.from_dict(value)
    row=value["candidates"][0]
    action=action if action is not None else {key:value for key,value in row.items() if key not in {"candidate_id","rationale"}}
    return select(proposal,basis_id=basis_id,policy=RunPolicy.from_dict(policy or policy_document()),
        owner_actions=[action],evidence_refs=row["evidence_refs"] if refs is None else refs)


def test_frozen_roundtrip_and_matching_canonical_action_remain_inert():
    value=document();proposal=WorkProposal.from_dict(value)
    assert decode_proposal(encode(value)).document()==value
    value["candidates"][0]["evidence_refs"][0]["excerpt_ref"]=HASH
    assert proposal.document()!=value
    result=choose(document())
    assert result["selected"]["candidate_id"]=="candidate"
    assert not result["authorized"] and not result["evidence_accepted"] and result["effects_executed"]==0


@pytest.mark.parametrize("changed",["basis","original","effect","capability","binding","action"])
def test_owner_policy_and_current_reference_intersection_is_decisive(changed):
    value=document();policy=policy_document();kwargs={"policy":policy}
    if changed=="basis":kwargs["basis_id"]=OTHER
    elif changed=="original":value["candidates"][0]["original_id"]="outside"
    elif changed=="effect":policy["allowed_effects"].remove("model")
    elif changed=="capability":policy["host_capability_refs"]["model"]=None
    elif changed=="binding":kwargs["refs"]=[]
    else:
        kwargs["action"]={"operation":"prepare","original_id":"question-a","claim_id":None,"evidence_refs":[]}
    result=choose(value,**kwargs)
    assert result["selected"] is None and len(result["rejected"])==1


@pytest.mark.parametrize("field,value",[("argv",["tool","run"]),("authorized",True),("controller_ref","self"),("config_ref","new-model")])
def test_candidate_extra_authority_or_arbitrary_tool_fields_refuse(field,value):
    supplied=document();supplied["candidates"][0][field]=value
    with pytest.raises(CycleInvalid):WorkProposal.from_dict(supplied)


@pytest.mark.parametrize("operation",["execute","acquire","release","install","run-python",True,[]])
def test_unsupported_operations_are_not_loosely_relabelled(operation):
    supplied=document();supplied["candidates"][0]["operation"]=operation
    with pytest.raises(CycleInvalid):WorkProposal.from_dict(supplied)


def test_duplicate_candidates_or_empty_context_or_missing_review_claim_refuse():
    for changed in ("duplicate","evidence","claim"):
        supplied=document()
        if changed=="duplicate":supplied["candidates"]*=2
        elif changed=="evidence":supplied["candidates"][0]["evidence_refs"]=[]
        else:supplied["candidates"][0]["operation"]="evaluate"
        with pytest.raises(CycleInvalid):WorkProposal.from_dict(supplied)


def test_closed_request_binding_and_positive_bounded_steps():
    request=CycleRequest("cycle","run",HASH)
    assert CycleRequest.from_dict(request.document())==request
    for bound in (True,0,65,1.0):
        with pytest.raises(CycleInvalid):CycleRequest("cycle","run",HASH,max_steps=bound)
    supplied=request.document();supplied["profile"]="unfrozen"
    with pytest.raises(CycleInvalid):CycleRequest.from_dict(supplied)


def test_duplicate_key_proposals_cannot_supply_a_second_basis():
    raw=encode(document()).replace(b'"basis_id":',b'"basis_id":"'+OTHER.encode()+b'","basis_id":',1)
    with pytest.raises(UsageError):decode_proposal(raw)


def test_source_bindings_are_exact_not_merely_source_names():
    row=document()["candidates"][0]
    proposal=WorkProposal.from_dict(document())
    refs=copy.deepcopy(row["evidence_refs"]);refs[0]["source_binding_id"]=OTHER
    action={key:value for key,value in row.items() if key not in {"candidate_id","rationale"}}
    result=select(proposal,basis_id=HASH,policy=RunPolicy.from_dict(policy_document()),owner_actions=[action],evidence_refs=refs)
    assert result["selected"] is None and result["rejected"][0]["reason"]=="evidence_reference_not_current"


def test_codec_rejects_surrogates_without_echoing_content():
    with pytest.raises(CycleInvalid,match="reference_invalid"):
        WorkCandidate("candidate","prepare","question-a",None,(),"hidden\ud800content")
