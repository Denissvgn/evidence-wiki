"""Closed cycle outcome observations; native release remains authoritative."""

from evidence_wiki.mutation_contracts import MutationError, _shape, canonical, detached
from evidence_wiki.research_session_contracts import STEP_V2, refuse
from evidence_wiki.research_session_contracts import checked as checked_step
from evidence_wiki.research_session_contracts import schemas as session_schemas

RESULT = "research-work-cycle-result/v1"
MAX_BYTES = 1_048_576


def schema_document():
    def obj(**fields):
        return {"type":"object","properties":fields,"required":list(fields),"additionalProperties":False}
    def text(maximum=256):
        return {"type":"string","minLength":1,"maxLength":maximum}
    identifier={**text(96),"pattern":r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$(?![\s\S])"}
    flag={"type":"boolean"}
    sha={"type":"string","pattern":r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    opaque={"type":"object","maxProperties":128,"additionalProperties":True}
    original=obj(original_id=text(),original_text=text(4096),attempted=flag,
        disposition={"enum":["accepted_current","rejected_native","abstained_native","not_attempted","blocked_current","pending"]},
        native_accepted=flag,questions={"type":"array","items":opaque,"maxItems":100},
        missing_question_ids={"type":"array","items":text(),"maxItems":100})
    return {"$schema":"https://json-schema.org/draft/2020-12/schema",**obj(schema_version={"const":RESULT},
        request_id=identifier,run_id=identifier,binding_id=sha,initial_basis_id=sha,current_basis_id=sha,
        terminal=flag,canonical_run_state=text(64),outcome=text(),reason=text(),step=session_schemas()[STEP_V2],
        original_outcomes={"type":"array","items":original,"minItems":1,"maxItems":100},
        accounting_complete=flag,current_release=opaque,usage=opaque,pending={"anyOf":[opaque,{"type":"null"}]},
        historical=flag,authorized={"const":False},evidence_accepted={"const":False})}


def checked(value):
    value=detached(value)
    try:
        _shape(value,schema_document())
    except MutationError:
        refuse("INVALID")
    if len(canonical(value))>MAX_BYTES:
        refuse("LIMIT")
    step=checked_step(STEP_V2,value["step"])
    for name in ("run_id","binding_id","terminal","canonical_run_state","outcome","reason","historical"):
        if value[name]!=step[name]:
            refuse("INVALID","/"+name)
    rows=value["original_outcomes"]
    ids=[row["original_id"] for row in rows]
    if len(set(ids))!=len(ids) or ids!=step["accounting"]["original_ids"]:
        refuse("INVALID","/original_outcomes")
    if value["terminal"] and value["canonical_run_state"] not in {"complete","blocked_on_sources","no_ship","failed"}:
        refuse("INVALID","/terminal")
    for row in rows:
        if row["native_accepted"]!=(row["disposition"]=="accepted_current"):
            refuse("INVALID","/original_outcomes")
        if row["native_accepted"] and row["missing_question_ids"]:
            refuse("INVALID","/original_outcomes")
        if row["disposition"]=="not_attempted" and row["attempted"]:
            refuse("INVALID","/original_outcomes")
        if row["disposition"] in {"blocked_current","pending"} and not row["attempted"]:
            refuse("INVALID","/original_outcomes")
        if value["terminal"] and row["disposition"]=="pending":
            refuse("INVALID","/original_outcomes")
    return value
