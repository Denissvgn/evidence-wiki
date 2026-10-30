"""Stable serialized legacy vectors do not acquire optional service declarations."""

import hashlib
import json

from evidence_wiki import (
    DisagreementPlan,
    ExecutionProfile,
    ExecutionRole,
    ExecutionRoleLimit,
    ExecutionRoleRegistry,
    ExecutionRoleScope,
    ExecutionSchedule,
    ReviewDelivery,
    RunPolicy,
)
from evidence_wiki.run_policy import POLICY_V2
from tests.test_run_policy import policy_document


def checksum(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",", ":")).encode()).hexdigest()


def test_legacy_policy_bytes_remain_fixed():
    one = policy_document()
    two = policy_document()
    two["schema_version"] = POLICY_V2
    two["scope"]["source_scope_kind"] = "identity"
    assert hashlib.sha256(RunPolicy(one).to_json()).hexdigest() == "a2e4e571061689b9154adeb6f63755260f749551534435e2980f5d7fd8a22d76"
    assert hashlib.sha256(RunPolicy(two).to_json()).hexdigest() == "2d604fd7b85e1147bb9c0a12f2278b711ab923921aa5afd017c4873edbd9e068"


def test_legacy_profile_documents_remain_fixed():
    worker = ExecutionRole("worker","host_worker","owner","offline/model","prompt")
    first = ExecutionProfile("legacy",4,"EUR","fixed",(worker,),1,1,100,"0")
    second = ExecutionProfile("legacy",4,"EUR","fixed",(worker,),1,1,100,"0",schedule=ExecutionSchedule((
        ExecutionRoleLimit("host_worker","owner",2,2,200,"0",60),)))
    assert checksum(first.document()) == "4b7802ba8fecf9be3863320d32e70c15a5c1737db574c0693aace554599ab444"
    assert checksum(second.document()) == "1f01fbbc06974ee15d23af0adba2cbd03e2446edb7bc5b3d2492ea5d4eefca25"
    roles = (worker,ExecutionRole("review","evaluator","review-owner","local/model","prompt"))
    registry = ExecutionRoleRegistry((ExecutionRoleScope("worker","researcher",("model",)),
        ExecutionRoleScope("review","evaluator",("model",))))
    cases = [({},"456a53ec5f12c4fac9cfb75b886456c8431626d8835e65af3118f961deae0d0f"),
        ({"disagreement":DisagreementPlan()},"3ece16a16ecd454275885a5d54e600fa7a33e00b9a1a3b1a842b224e5567c3fc"),
        ({"review_delivery":ReviewDelivery()},"c6e9c6baa996a33fc94bf536a2420f33a84eda51bdd8828ea8a476a7bb5e4f4c")]
    for fields, expected in cases:
        assert checksum(ExecutionProfile("legacy",4,"EUR","fixed",roles,1,1,100,"0",role_scopes=registry,**fields).document()) == expected
    compute = ExecutionRole("compute","host_worker","owner","offline/model","prompt")
    scopes = ExecutionRoleRegistry((*registry.scopes,ExecutionRoleScope("compute","calculation",("model","calculation.propose"))))
    assert checksum(ExecutionProfile("legacy",4,"EUR","fixed",(*roles,compute),1,1,100,"0",role_scopes=scopes).document()) == "567efa3e716f6ee961d16442c4200efbf64eecfbe40404cb73b46e015989f2a2"
