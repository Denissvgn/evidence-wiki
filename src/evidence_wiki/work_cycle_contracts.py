"""Inert bounded work candidates; canonical owners retain all effect authority."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from evidence_wiki.mutation_contracts import canonical, detached, validate_request_id
from evidence_wiki.run_policy import RunPolicy

REQUEST = "research-work-cycle-request/v1"
PROPOSAL = "research-work-proposal/v1"
SELECTION = "research-work-selection/v1"
_HASH = re.compile(r"sha256:[a-f0-9]{64}\Z")
OPERATIONS = ("prepare", "generate", "evaluate", "repair", "adjudicate", "request_evidence", "abstain")
EFFECTS = {"prepare":"offline_check", "generate":"model", "evaluate":"model", "repair":"revise",
           "adjudicate":"adjudicate", "request_evidence":"acquire", "abstain":"offline_check"}


class CycleInvalid(ValueError):
    """Content-free refusal; input text and candidate reasons are not rendered."""


def refuse(reason):
    raise CycleInvalid("work_cycle_" + reason) from None


def text(value, maximum=256):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(char)<32 or 0xD800<=ord(char)<=0xDFFF for char in value)):
        refuse("reference_invalid")
    return value


def digest(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        refuse("binding_invalid")
    return value


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        refuse("document_fields_invalid")
    return value


@dataclass(frozen=True)
class CycleRequest:
    request_id: str
    run_id: str
    expected_basis_id: str
    max_steps: int = 64
    planner_config_ref: str | None = None

    def __post_init__(self):
        validate_request_id(self.request_id)
        validate_request_id(self.run_id)
        digest(self.expected_basis_id)
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 64:
            refuse("step_bound_invalid")
        if self.planner_config_ref is not None:
            text(self.planner_config_ref)

    def document(self):
        return {"schema_version":REQUEST, "request_id":self.request_id, "run_id":self.run_id,
            "expected_basis_id":self.expected_basis_id, "max_steps":self.max_steps,
            "planner_config_ref":self.planner_config_ref}

    @classmethod
    def from_dict(cls,value):
        exact(value,{"schema_version","request_id","run_id","expected_basis_id","max_steps","planner_config_ref"})
        if value["schema_version"] != REQUEST:
            refuse("schema_unsupported")
        return cls(**{key:part for key,part in value.items() if key != "schema_version"})


@dataclass(frozen=True)
class WorkCandidate:
    candidate_id: str
    operation: str
    original_id: str
    claim_id: str | None
    evidence_refs: tuple[tuple[str,str,str], ...]
    rationale: str

    def __post_init__(self):
        validate_request_id(self.candidate_id)
        if type(self.operation) is not str or self.operation not in OPERATIONS:
            refuse("operation_unsupported")
        text(self.original_id)
        if self.claim_id is not None:
            text(self.claim_id)
        if type(self.evidence_refs) is not tuple or len(self.evidence_refs)>32:
            refuse("evidence_bound_invalid")
        for row in self.evidence_refs:
            if type(row) is not tuple or len(row)!=3:
                refuse("evidence_reference_invalid")
            text(row[0]);digest(row[1]);digest(row[2])
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            refuse("evidence_reference_duplicate")
        text(self.rationale,4096)
        if self.operation in {"evaluate","repair","adjudicate"} and self.claim_id is None:
            refuse("claim_reference_required")
        if self.operation in {"generate","evaluate","repair","adjudicate"} and not self.evidence_refs:
            refuse("evidence_reference_required")

    def document(self):
        return {"candidate_id":self.candidate_id, "operation":self.operation, "original_id":self.original_id,
            "claim_id":self.claim_id, "evidence_refs":[{"source_id":row[0],"source_binding_id":row[1],"excerpt_ref":row[2]}
                for row in self.evidence_refs], "rationale":self.rationale}

    @classmethod
    def from_dict(cls,value):
        exact(value,{"candidate_id","operation","original_id","claim_id","evidence_refs","rationale"})
        rows=value["evidence_refs"]
        if type(rows) is not list or len(rows)>32:
            refuse("evidence_bound_invalid")
        bindings=[]
        for row in rows:
            exact(row,{"source_id","source_binding_id","excerpt_ref"})
            bindings.append((row["source_id"],row["source_binding_id"],row["excerpt_ref"]))
        return cls(value["candidate_id"],value["operation"],value["original_id"],value["claim_id"],tuple(bindings),value["rationale"])


@dataclass(frozen=True)
class WorkProposal:
    basis_id: str
    candidates: tuple[WorkCandidate, ...]

    def __post_init__(self):
        digest(self.basis_id)
        if (type(self.candidates) is not tuple or not 1 <= len(self.candidates)<=16
                or any(type(row) is not WorkCandidate for row in self.candidates)):
            refuse("candidate_bound_invalid")
        if len({row.candidate_id for row in self.candidates}) != len(self.candidates):
            refuse("candidate_duplicate")

    def document(self):
        return {"schema_version":PROPOSAL,"basis_id":self.basis_id,"candidates":[row.document() for row in self.candidates]}

    @classmethod
    def from_dict(cls,value):
        exact(value,{"schema_version","basis_id","candidates"})
        if value["schema_version"] != PROPOSAL:
            refuse("schema_unsupported")
        if type(value["candidates"]) is not list or not 1 <= len(value["candidates"])<=16:
            refuse("candidate_bound_invalid")
        copied=detached(value)
        if len(canonical(copied))>262144:
            refuse("document_bound_invalid")
        return cls(copied["basis_id"],tuple(WorkCandidate.from_dict(row) for row in copied["candidates"]))


def select(proposal, *, basis_id, policy, owner_actions, evidence_refs):
    """Intersect inert candidates with exact canonical actions, policy and bindings.

    The calling execution owner authenticates the proposal, current observation,
    capability mapping and owner actions. This pure reducer grants no authority.
    """
    if type(proposal) is not WorkProposal or type(policy) is not RunPolicy:
        refuse("selection_inputs_invalid")
    digest(basis_id)
    if type(owner_actions) is not list or len(owner_actions)>32 or type(evidence_refs) is not list or len(evidence_refs)>256:
        refuse("selection_bound_invalid")
    actions=[]
    for action in owner_actions:
        exact(action,{"operation","original_id","claim_id","evidence_refs"})
        # Reuse the strict candidate codec, excluding caller-supplied authority.
        checked=WorkCandidate.from_dict({"candidate_id":"owner-action","rationale":"Canonical owner action",**action})
        actions.append({key:part for key,part in checked.document().items() if key not in {"candidate_id","rationale"}})
    references=set()
    for row in evidence_refs:
        exact(row,{"source_id","source_binding_id","excerpt_ref"})
        references.add((text(row["source_id"]),digest(row["source_binding_id"]),digest(row["excerpt_ref"])))
    frozen=policy.to_dict()
    rejected=[]
    selected=None
    for candidate in proposal.candidates:
        reason=None
        effect=EFFECTS[candidate.operation]
        comparable={key:part for key,part in candidate.document().items() if key not in {"candidate_id","rationale"}}
        if proposal.basis_id != basis_id:
            reason="basis_changed"
        elif candidate.original_id not in frozen["scope"]["question_ids"]:
            reason="original_outside_scope"
        elif effect not in frozen["allowed_effects"] or effect!="offline_check" and frozen["host_capability_refs"][effect] is None:
            reason="effect_ungranted"
        elif not set(candidate.evidence_refs)<=references:
            reason="evidence_reference_not_current"
        elif comparable not in actions:
            reason="canonical_action_not_permitted"
        if reason:
            rejected.append({"candidate_id":candidate.candidate_id,"reason":reason})
        elif selected is None:
            selected=candidate.document()
    return {"schema_version":SELECTION,"basis_id":basis_id,"selected":selected,"rejected":rejected,
        "authorized":False,"evidence_accepted":False,"effects_executed":0}


def decode_proposal(raw):
    if type(raw) is not bytes or not 0 < len(raw)<=262144:
        refuse("document_bound_invalid")
    from evidence_wiki._pack_io import json_document
    return WorkProposal.from_dict(json_document(raw))


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
