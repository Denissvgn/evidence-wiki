"""Closed inert research-session observations and bounded step results."""

from __future__ import annotations

import json

from .mutation_contracts import _shape, canonical, detached
from .workflow_contracts import WorkflowContractError

CURRENT = "research-session-current/v1"
NEXT = "research-session-next/v1"
BINDING = "research-session-binding/v1"
STEP = "research-session-step/v1"
CURRENT_V2 = "research-session-current/v2"
NEXT_V2 = "research-session-next/v2"
BINDING_V2 = "research-session-binding/v2"
STEP_V2 = "research-session-step/v2"
MAX_BYTES = 262_144


def refuse(reason, field="/"):
    raise WorkflowContractError("RESEARCH_" + reason, "Research session request refused.", recoverable=False,
        details={"field": field}, remediation="Inspect canonical state, original scope and the immutable policy before advancing.") from None


def _obj(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def _text(size=256):
    return {"type": "string", "minLength": 1, "maxLength": size}


def _nullable(shape):
    return {"anyOf": [shape, {"type": "null"}]}


def schemas():
    opaque = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    identifier = {**_text(96), "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$(?![\s\S])"}
    digest = {**_text(71), "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    flags = dict(authorized={"const": False}, evidence_accepted={"const": False})
    accounting = _obj(original_ids={"type": "array", "items": _text(), "maxItems": 100},
        original_outcomes={"type": "array", "items": opaque, "maxItems": 100},
        accounting_complete={"type": "boolean"}, missing_mapping={"type": "boolean"},
        accepted_count={"type": "integer", "minimum": 0, "maximum": 100},
        unresolved_count={"type": "integer", "minimum": 0, "maximum": 100})
    usage = _obj(**{key: _nullable({"type": "string"} if key == "cost_limit" else {"type": "integer", "minimum": 0})
        for key in ("seconds", "model_calls", "tool_calls", "tokens", "cost_limit", "claims", "sources",
                    "repair_cycles", "adjudication_cycles", "retry_attempts", "acquisition_attempts")})
    qualification = _obj(state={"const": "unknown"}, reason={"const": "no_authenticated_evaluator_profile_owner"})
    run = _obj(run_id=identifier, actor_ref=_text(), state=_text(64),
        allowed_next_states={"type": "array", "items": _text(64), "maxItems": 8},
        started_utc=_nullable(_text(64)), updated_utc=_nullable(_text(64)), last_heartbeat_utc=_nullable(_text(64)),
        caller_context_id=_nullable(_text()), session_binding_id=_nullable(digest), terminal={"type": "boolean"})
    current = _obj(schema_version={"const": CURRENT}, target=_text(4096), run_id=_nullable(identifier),
        basis_id=digest, policy_id=digest, observed_utc=_text(64), accounting=accounting,
        canonical_release=opaque, run=_nullable(run), sources=opaque, qualification=qualification,
        usage=usage, policy_compatible={"type": "boolean"}, limitations={"type": "array", "items": _text(4096), "maxItems": 16},
        authority=opaque,
        **flags)
    action = _obj(operation={"enum": ["offline_check", "terminal", "host_job"]},
        owner=_text(), arguments=opaque, preconditions={"type": "array", "items": _text(), "maxItems": 16},
        effects={"type": "array", "items": _text(), "maxItems": 16},
        required_grants={"type": "array", "items": _text(), "maxItems": 32},
        reason=_text(), **flags)
    next_ = _obj(schema_version={"const": NEXT}, current=current,
        actions={"type": "array", "items": action, "maxItems": 8}, decision=opaque, **flags)
    binding = _obj(schema_version={"const": BINDING}, run_id=identifier, actor_ref=_text(),
        target=_text(4096), workspace_binding=_obj(path=_text(4096), device=_text(64), inode=_text(64)),
        policy=opaque, policy_id=digest, request_binding=digest, host_context_sha256=digest, owner_generation=opaque,
        original_ids={"type": "array", "items": _text(), "minItems": 1, "maxItems": 100}, **flags)
    step = _obj(schema_version={"const": STEP}, request_id=identifier, run_id=identifier, binding_id=digest,
        before_basis_id=digest, outcome=_text(), reason=_text(), terminal={"type": "boolean"},
        decision=opaque, job=_nullable(opaque), event_ref=_nullable(_text()), canonical_run_state=_text(),
        accounting=accounting, historical={"type": "boolean"}, **flags)
    current_v2 = json.loads(json.dumps(current))
    current_v2["properties"]["schema_version"] = {"const": CURRENT_V2}
    current_v2["properties"]["qualification"] = _obj(state={"enum": ["qualified", "unknown", "unqualified"]},
        reason=_text(), profiles={"type": "array", "items": opaque, "maxItems": 8})
    current_v2["properties"]["usage"]["properties"]["seconds"] = _nullable({"type": "number", "minimum": 0})
    current_v2["properties"]["execution"] = opaque
    current_v2["required"].append("execution")
    next_v2 = json.loads(json.dumps(next_))
    next_v2["properties"]["schema_version"] = {"const": NEXT_V2}
    next_v2["properties"]["current"] = current_v2
    next_v2["properties"]["decision"] = _nullable(opaque)
    next_v2["properties"]["actions"]["items"]["properties"]["operation"] = {
        "enum": ["terminal", "normalize", "prepare", "generate", "evaluate", "repair", "reconcile", "admit", "resolve", "release"]}
    binding_v2 = json.loads(json.dumps(binding))
    binding_v2["properties"]["schema_version"] = {"const": BINDING_V2}
    for key in ("execution_profile_id", "qualification_id", "signer_id", "observer_id"):
        binding_v2["properties"][key] = digest
        binding_v2["required"].append(key)
    step_v2 = json.loads(json.dumps(step))
    step_v2["properties"]["schema_version"] = {"const": STEP_V2}
    step_v2["properties"]["decision"] = _nullable(opaque)
    step_v2["properties"]["action"] = _text(64)
    step_v2["properties"]["attempt_id"] = _nullable(identifier)
    step_v2["required"].extend(["action", "attempt_id"])
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
        for name, shape in ((CURRENT, current), (NEXT, next_), (BINDING, binding), (STEP, step),
                            (CURRENT_V2, current_v2), (NEXT_V2, next_v2), (BINDING_V2, binding_v2), (STEP_V2, step_v2))}


def schema_document(name):
    if type(name) is not str or len(name) > 64 or name not in schemas():
        refuse("VERSION_UNSUPPORTED", "/schema_version")
    return schemas()[name]


def checked(name, value):
    try:
        value = detached(value)
        _shape(value, schema_document(name))
        if len(canonical(value)) > MAX_BYTES:
            refuse("LIMIT")
        if name in {BINDING, BINDING_V2} and len(set(value["original_ids"])) != len(value["original_ids"]):
            refuse("INVALID", "/original_ids")
        if name in {BINDING, BINDING_V2}:
            from .run_policy import POLICY, RunPolicy
            if name == BINDING and value["policy"].get("schema_version") != POLICY:
                refuse("VERSION_UNSUPPORTED", "/policy/schema_version")
            policy = RunPolicy(value["policy"])
            if policy.policy_id != value["policy_id"] or set(value["original_ids"]) != set(policy.to_dict()["scope"]["question_ids"]):
                refuse("INVALID", "/policy_id")
        if name in {CURRENT, STEP, CURRENT_V2, STEP_V2}:
            accounting = value["accounting"]
            ids = accounting["original_ids"]
            if (len(ids) != len(set(ids)) or accounting["accepted_count"] + accounting["unresolved_count"] != len(ids)
                    or [row.get("original_id") for row in accounting["original_outcomes"]] != ids):
                refuse("INVALID", "/accounting")
        if name == NEXT_V2:
            checked(CURRENT_V2, value["current"])
        if name in {NEXT, STEP, NEXT_V2, STEP_V2} and value["decision"] is not None:
            from .run_policy import DECISION, validate_document
            validate_document(DECISION, value["decision"])
        if name in {STEP, STEP_V2} and value["job"] is not None:
            from .workflow_contracts import JOB, validate_document
            validate_document(JOB, value["job"])
        if name == STEP_V2:
            decision = value["decision"]
            if ((decision is None and (value["terminal"] or value["outcome"] not in {"advanced", "pending"}))
                    or decision is not None and (value["terminal"] != decision["terminal"] or value["outcome"] != decision["outcome"])):
                refuse("INVALID", "/terminal")
    except WorkflowContractError:
        raise
    except Exception:
        refuse("INVALID")
    return value


def encode(name, value):
    return canonical(checked(name, value))


def decode(name, raw):
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        refuse("LIMIT")
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    refuse("INVALID")
                result[key] = value
            return result
        return checked(name, json.loads(raw.decode(), object_pairs_hook=unique, parse_constant=lambda _: refuse("INVALID")))
    except WorkflowContractError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        refuse("INVALID")
