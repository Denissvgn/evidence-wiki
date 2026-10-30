"""Result codecs keep native disposition separate from attempt and acceptance."""

import copy

import pytest

from evidence_wiki import cycle_result_contracts as contract
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests.test_run_policy import observation, policy_document


def document():
    policy=RunPolicy(policy_document())
    observed=observation(policy,0)
    observed["spent"]["seconds"]=301
    decision=policy.decide(observed)
    sha="sha256:"+"a"*64
    originals=[{"original_id":identifier,"original_text":"Exact original", "attempted":False,
        "disposition":"not_attempted","native_accepted":False,"questions":[],"missing_question_ids":[]}
        for identifier in policy.to_dict()["scope"]["question_ids"]]
    accounting={"original_ids":[r["original_id"] for r in originals],"original_outcomes":[{"original_id":r["original_id"]} for r in originals],
        "accounting_complete":True,"missing_mapping":False,"accepted_count":0,"unresolved_count":2}
    step={"schema_version":"research-session-step/v2","request_id":"step","run_id":"run","binding_id":sha,
        "before_basis_id":sha,"outcome":decision["outcome"],"reason":decision["reason"],"terminal":True,
        "decision":decision,"job":None,"event_ref":"event","canonical_run_state":"no_ship","accounting":accounting,
        "historical":False,"authorized":False,"evidence_accepted":False,"action":"terminal","attempt_id":None}
    return {"schema_version":contract.RESULT,"request_id":"drive","run_id":"run","binding_id":sha,
        "initial_basis_id":sha,"current_basis_id":sha,"terminal":True,"canonical_run_state":"no_ship",
        "outcome":step["outcome"],"reason":step["reason"],"step":step,"original_outcomes":originals,
        "accounting_complete":True,"current_release":{},"usage":{},"pending":None,"historical":False,
        "authorized":False,"evidence_accepted":False}


def test_result_is_detached_and_keeps_unattempted_terminal_accounting():
    value=document()
    checked=contract.checked(value)
    assert checked==value
    checked["original_outcomes"][0]["original_text"]="Changed"
    assert value["original_outcomes"][0]["original_text"]=="Exact original"


@pytest.mark.parametrize("attack",["outcome","run","terminal_state","duplicate","accepted_missing","false_disposition","attempted","pending_terminal","unknown_field","request_id"])
def test_result_cross_field_attack_refused(attack):
    value=copy.deepcopy(document())
    row=value["original_outcomes"][0]
    if attack=="outcome":value["outcome"]="accepted"
    elif attack=="run":value["run_id"]="foreign"
    elif attack=="terminal_state":value["canonical_run_state"]=value["step"]["canonical_run_state"]="active"
    elif attack=="duplicate":value["original_outcomes"][1]=copy.deepcopy(row)
    elif attack=="accepted_missing":row.update(native_accepted=True,disposition="accepted_current",missing_question_ids=["gap"])
    elif attack=="false_disposition":row["native_accepted"]=True
    elif attack=="attempted":row["attempted"]=True
    elif attack=="pending_terminal":row.update(attempted=True,disposition="pending")
    elif attack=="request_id":value["request_id"]="../foreign"
    else:value["approved"]=True
    with pytest.raises(WorkflowContractError):contract.checked(value)


def test_current_release_accounting_is_separate_from_historical_step():
    value=document()
    value["historical"]=value["step"]["historical"]=True
    value["accounting_complete"]=False
    row=value["original_outcomes"][0]
    row.update(native_accepted=True,disposition="accepted_current")
    assert contract.checked(value)["original_outcomes"][0]["native_accepted"]
