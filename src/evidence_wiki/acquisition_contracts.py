"""Closed inert acquisition choices cannot create provider or source authority."""

from __future__ import annotations

import base64
import copy
import hashlib

from evidence_wiki._pack_io import bounded, canonical
from evidence_wiki.errors import UsageError
from evidence_wiki.pack_discovery import owner

INSTRUCTION = "evidence-acquisition-instruction/v1"
PROPOSAL = "evidence-acquisition-choice/v1"


def refuse(field):
    raise UsageError("ACQUISITION_CHOICE_INVALID", "Acquisition choice refused.", details={"field": field})


def _text(value, field, maximum=256):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        refuse(field)
    return value


def _scope(value):
    if type(value) is not dict or len(value) > 16:
        refuse("scope")
    result = {_text(key, "scope_key", 128): _text(item, "scope_value", 1024) for key, item in value.items()}
    if owner("_request_scope").normalize_scope(result) != result:
        refuse("scope")
    return result


def instruction(record, *, required_scope, alternatives, max_attempts=1, retained_attempts=0):
    """Bind candidate labels to retained bookkeeping, never to execution rights."""
    if type(record) is not dict or record.get("status") not in {"open", "fulfilled"}:
        refuse("request")
    identifier = _text(record.get("request_id"), "request_id", 128)
    query = _text(record.get("query_or_identifier"), "query", 4096)
    scope, required = _scope(record.get("scope") or {}), _scope(required_scope)
    if any(key in scope and scope[key] != value for key, value in required.items()):
        refuse("frozen_scope_conflict")
    if type(alternatives) is not list or len(alternatives) > 32:
        refuse("alternatives")
    if (type(max_attempts) is not int or not 0 <= max_attempts <= 32
            or type(retained_attempts) is not int or not 0 <= retained_attempts <= 4096):
        refuse("attempt_bounds")
    detached, seen = [], set()
    for candidate in alternatives:
        if type(candidate) is not dict or set(candidate) != {"alternative_id", "provider_id", "resource_ref", "scope"}:
            refuse("alternative_shape")
        row = {"alternative_id": _text(candidate["alternative_id"], "alternative_id", 128),
            "provider_id": _text(candidate["provider_id"], "provider_id", 128),
            "resource_ref": _text(candidate["resource_ref"], "resource_ref", 4096), "scope": _scope(candidate["scope"])}
        if row["alternative_id"] in seen:
            refuse("alternative_duplicate")
        seen.add(row["alternative_id"])
        detached.append(row)
    basis = "sha256:" + hashlib.sha256(canonical(record)).hexdigest()
    value = {"schema_version": INSTRUCTION, "request_id": identifier, "request_basis": basis,
        "recorded_status": record["status"], "query_or_identifier": query,
        "required_scope": {**scope, **required}, "alternatives": detached,
        "attempts": {"maximum": max_attempts, "retained": retained_attempts,
            "remaining": max(0, max_attempts - retained_attempts)},
        "execution_authority": "not_established", "source_accepted": False, "rights_inferred": False}
    bounded(value)
    return value


def choose(prepared, proposal):
    """Reduce an agent suggestion to an inert owner-check instruction."""
    if (type(prepared) is not dict or prepared.get("schema_version") != INSTRUCTION
            or prepared.get("execution_authority") != "not_established"):
        refuse("instruction")
    if type(proposal) is not dict or set(proposal) != {"schema_version", "request_id", "request_basis", "action", "alternative_id", "reason"}:
        refuse("proposal_shape")
    if (proposal["schema_version"] != PROPOSAL or proposal["request_id"] != prepared["request_id"]
            or proposal["request_basis"] != prepared["request_basis"]):
        refuse("stale_request")
    action = proposal["action"]
    if type(action) is not str or action not in {"retry", "alternate", "narrow", "abstain"}:
        refuse("action")
    _text(proposal["reason"], "reason", 1024)
    alternatives = {row["alternative_id"]: row for row in prepared["alternatives"]}
    selected = proposal["alternative_id"]
    if selected is not None:
        _text(selected, "alternative_id", 128)
    if action != "abstain" and (selected is None or selected not in alternatives):
        refuse("unselected_alternative")
    if action == "abstain":
        if selected is not None:
            refuse("abstention_target")
        status, reason = "abstained", "agent_abstained"
    elif prepared["attempts"]["remaining"] == 0:
        status, reason = "budget_exhausted", "canonical_budget_recheck_required"
    else:
        if type(selected) is not str or selected not in alternatives:
            refuse("unselected_alternative")
        scope = alternatives[selected]["scope"]
        if action == "narrow" and any(prepared["required_scope"].get(key) != value for key, value in scope.items()):
            status, reason = "blocked_external", "narrow_scope_not_preauthorized"
        elif any(scope.get(key) != value for key, value in prepared["required_scope"].items()):
            status, reason = "blocked_external", "frozen_required_scope_not_covered"
        else:
            status, reason = "ready_for_owner_check", "current_native_provider_scope_permission_budget_recheck_required"
    return {"schema_version": "evidence-acquisition-choice-result/v1", "request_id": prepared["request_id"],
        "request_basis": prepared["request_basis"], "action": action, "alternative_id": selected,
        "status": status, "reason": reason, "proposal": copy.deepcopy(proposal),
        "execution_authority": "not_established", "source_accepted": False, "rights_inferred": False}


def schemas():
    """Publish bounded observation shapes without granting connector authority."""
    from .source_contracts import array, obj, string
    sha={"type":"string","pattern":r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    nullable=lambda shape:{"anyOf":[shape,{"type":"null"}]}
    flags={"source_accepted":{"const":False},"rights_inferred":{"const":False}}
    facts={"type":"object","maxProperties":128,"additionalProperties":True}
    scope={"type":"object","maxProperties":16,"additionalProperties":string(1024)}
    alternative=obj(alternative_id=string(128),provider_id=string(128),resource_ref=string(4096),scope=scope)
    instruction_shape=obj(schema_version={"const":INSTRUCTION},request_id=string(128),request_basis=sha,
        recorded_status={"enum":["open","fulfilled"]},query_or_identifier=string(4096),required_scope=scope,
        alternatives=array(alternative,32),attempts=obj(maximum={"type":"integer","minimum":0,"maximum":32},
            retained={"type":"integer","minimum":0,"maximum":4096},remaining={"type":"integer","minimum":0,"maximum":32}),
        execution_authority={"const":"not_established"},**flags)
    prepared=obj(**instruction_shape["properties"],provider_configuration=obj(enabled={"type":"boolean"},providers=array(string(128),32)),
        run_id=nullable(string(128)),budget_status=string(128),status=string(128),reason=string(128),workspace_revision=sha)
    source=obj(source_id=string(256),current_ready={"type":"boolean"},gaps=array(string(256),128),inspection=facts,usage=facts,
        permission_basis={"enum":["native_current_usage","legacy_compatibility"]},source_revision=nullable(sha),material_basis=sha,
        semantic_adequacy={"const":"not_evaluated"},evidence_accepted={"const":False})
    source={"anyOf":[source,obj(source_id=string(512),current_ready={"const":False},
        gaps=array(string(256),128,1))]}
    reconciliation=obj(schema_version={"const":"evidence-acquisition-reconciliation/v1"},request_id=string(128),request_basis=sha,
        acquisition_ref=string(128),capture_input_sha256=sha,source=source,
        status={"enum":["ready_for_owner_fulfillment","partial"]},next_operation=string(128),workspace_revision=sha,
        recorded_status={"enum":["open","fulfilled"]},recorded_source_id=nullable(string(256)),request_fulfilled_by_reconciliation={"const":False},**flags)
    blocked=obj(schema_version={"const":"evidence-acquisition-fulfillment/v1"},status={"const":"blocked_external"},
        reason={"const":"current_source_not_ready"},request_id=string(128),source=source,request_fulfilled={"const":False},**flags)
    completed=obj(schema_version={"const":"evidence-acquisition-fulfillment/v1"},status=string(128),request_id=string(128),
        mutation_request_id=string(128),receipt=facts,historical_fulfillment_recorded={"type":"boolean"},historical={"const":True},
        current_evidence_usable={"const":"not_established"},**flags)
    transport=obj(schema_version={"const":"evidence-acquisition-transport-result/v1"},source_request_id=string(128),
        request_basis=sha,provider={"const":"web"},byte_count={"type":"integer","minimum":1,"maximum":49152},
        content_sha256=sha,content_base64={"type":"string","minLength":4,"maxLength":65536},
        final_url=string(4096),content_type={"enum":["text/plain","text/html","application/json"]},tls_verified={"const":True},
        reserved_provider_calls={"type":"integer","minimum":1,"maximum":4096},source_captured={"const":False},**flags)
    workflow=obj(schema_version={"const":"evidence-acquisition-workflow-result/v1"},
        status={"enum":["fulfilled","partial","capture_unknown","fulfillment_unknown"]},source_request_id=string(128),
        owner_result=facts,source_accepted={"const":False},rights_inferred={"const":False},semantic_adequacy={"const":"not_evaluated"})
    agent=obj(schema_version={"const":"evidence-acquisition-agent-result/v1"},status={"enum":["blocked_external","ready_for_owner_check","abstained","budget_exhausted"]},
        request_id=string(128),reason=string(128),proposal=nullable(facts),model_invoked={"type":"boolean"},**flags)
    agent_workflow=obj(schema_version={"const":"evidence-acquisition-agent-workflow-result/v1"},
        status={"enum":["fulfilled","partial","capture_unknown","fulfillment_unknown","blocked_external","abstained","budget_exhausted","ready_for_owner_check"]},
        choice=agent,evidence=nullable(workflow),**flags)
    return {name:{"$schema":"https://json-schema.org/draft/2020-12/schema",**shape} for name,shape in
        ((INSTRUCTION,instruction_shape),("evidence-acquisition-agent-workflow-result/v1",agent_workflow),
         ("evidence-acquisition-agent-result/v1",agent),
         ("evidence-acquisition-workflow-result/v1",workflow),
         ("evidence-acquisition-transport-result/v1",transport),
         ("evidence-acquisition-preparation/v1",prepared),
         ("evidence-acquisition-reconciliation/v1",reconciliation),("evidence-acquisition-fulfillment/v1",{"anyOf":[blocked,completed]}))}


def schema_document(name):
    values=schemas()
    if type(name) is not str or name not in values:
        refuse("schema_unknown")
    return values[name]


def checked_transport(value):
    """Check bounded exact transport bytes; this creates no capture or permission."""
    from .onboarding_contract import _matches
    bounded(value)
    _matches(value,schema_document("evidence-acquisition-transport-result/v1"))
    try:
        raw=base64.b64decode(value["content_base64"],validate=True)
    except (ValueError,UnicodeError):
        refuse("transport_encoding_invalid")
    if (base64.b64encode(raw).decode("ascii")!=value["content_base64"] or len(raw)!=value["byte_count"]
            or "sha256:"+hashlib.sha256(raw).hexdigest()!=value["content_sha256"]):
        refuse("transport_bytes_changed")
    return copy.deepcopy(value)


def checked_instruction(value):
    """Validate exact inert choices without trusting supplied provider labels."""
    from .onboarding_contract import _matches
    bounded(value)
    _matches(value,schema_document(INSTRUCTION))
    attempts=value["attempts"]
    if attempts["remaining"]!=max(0,attempts["maximum"]-attempts["retained"]):
        refuse("instruction_attempts_inconsistent")
    identifiers=[row["alternative_id"] for row in value["alternatives"]]
    if len(identifiers)!=len(set(identifiers)):
        refuse("alternative_duplicate")
    _scope(value["required_scope"])
    for alternative in value["alternatives"]:
        _scope(alternative["scope"])
    return copy.deepcopy(value)
