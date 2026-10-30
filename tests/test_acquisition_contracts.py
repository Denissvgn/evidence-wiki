"""Agent choices retain scope and never establish connector authority."""

import copy

import pytest

from evidence_wiki import acquisition_contracts as codec
from evidence_wiki.errors import UsageError


def prepared(**values):
    record = {"request_id": "request", "status": "open", "query_or_identifier": "Original scoped measurements",
        "scope": {"facet_id": "primary"}, "question_slugs": ["question"]}
    candidates = [{"alternative_id": "candidate", "provider_id": "web", "resource_ref": "https://example.org/source",
        "scope": {"facet_id": "primary", "jurisdiction": "Spain"}}]
    return codec.instruction(record, required_scope={"jurisdiction": "Spain"}, alternatives=candidates, **values)


def proposal(value, **changes):
    return {"schema_version": codec.PROPOSAL, "request_id": value["request_id"], "request_basis": value["request_basis"],
        "action": "alternate", "alternative_id": "candidate", "reason": "Try the retained scoped alternative", **changes}


def test_choice_requires_actual_owner_check_and_cannot_infer_grants():
    value = prepared()
    chosen = codec.choose(value, proposal(value))
    assert chosen["status"] == "ready_for_owner_check"
    assert chosen["execution_authority"] == "not_established"
    assert not chosen["rights_inferred"] and not chosen["source_accepted"]


@pytest.mark.parametrize("action", ["retry", "alternate", "narrow"])
def test_retained_attempt_exhaustion_is_finite_with_no_hidden_retry(action):
    value = prepared(max_attempts=1, retained_attempts=1)
    chosen = codec.choose(value, proposal(value, action=action))
    assert chosen["status"] == "budget_exhausted"
    assert not chosen["source_accepted"]


@pytest.mark.parametrize("field", ["grant", "credentials", "callback", "command", "receipt", "source_accepted"])
def test_hostile_connector_fields_are_not_model_authority(field):
    value = prepared()
    with pytest.raises(UsageError):
        codec.choose(value, proposal(value, **{field: "execute"}))


def test_stale_request_or_foreign_alternative_refuses():
    value = prepared()
    for changes in ({"request_basis": "sha256:" + "0" * 64}, {"request_id": "foreign"}, {"alternative_id": "foreign"}):
        with pytest.raises(UsageError):
            codec.choose(value, proposal(value, **changes))


def test_narrow_choice_cannot_change_frozen_success_scope_or_add_ungranted_scope():
    value = prepared()
    for scope in ({"facet_id": "primary"}, {"facet_id": "primary", "jurisdiction": "Portugal"},
            {"facet_id": "primary", "jurisdiction": "Spain", "new_context": "outside"}):
        changed = copy.deepcopy(value)
        changed["alternatives"][0]["scope"] = scope
        result = codec.choose(changed, proposal(changed, action="narrow"))
        assert result["status"] == "blocked_external"


def test_abstention_has_no_execution_target():
    value = prepared()
    assert codec.choose(value, proposal(value, action="abstain", alternative_id=None))["status"] == "abstained"
    with pytest.raises(UsageError):
        codec.choose(value, proposal(value, action="abstain"))


def transport_document():
    import base64
    import hashlib
    raw=b"Exact scoped original"
    return {"schema_version":"evidence-acquisition-transport-result/v1","source_request_id":"request",
        "request_basis":"sha256:"+"a"*64,"provider":"web","byte_count":len(raw),
        "content_sha256":"sha256:"+hashlib.sha256(raw).hexdigest(),"content_base64":base64.b64encode(raw).decode(),
        "final_url":"https://example.org/source","content_type":"text/plain","tls_verified":True,
        "reserved_provider_calls":1,"source_captured":False,"source_accepted":False,"rights_inferred":False}


def test_transport_codec_preserves_exact_bytes_without_source_acceptance():
    value=transport_document()
    result=codec.checked_transport(value)
    assert result==value and result is not value


@pytest.mark.parametrize("field,value",[("byte_count",1),("content_sha256","sha256:"+"0"*64),
    ("content_base64","%%%="),("tls_verified",False),("source_accepted",True)])
def test_transport_codec_refuses_inconsistent_or_authoritative_fields(field,value):
    document=transport_document()
    document[field]=value
    with pytest.raises(UsageError):codec.checked_transport(document)
