"""Exact bounded codec/HMAC mechanics; no native run or provider qualification."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from evidence_wiki import readability_execution as input_owner
from evidence_wiki.agent_execution import ExecutionAttempt
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import canonical, detached, digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.readability_context import create
from evidence_wiki.readability_contracts import (
    PREPARATION,
    SEALED_INPUT,
    _persist,
    _restore,
    check_persisted_record,
    restored_preparation,
    stored_attempt,
    stored_preparation,
)
from evidence_wiki.research_execution import ResearchExecution
from tests.test_readability_context import context
from tests.test_readability_profile import attempt
from tests.test_readability_profile import profile as profile


@pytest.fixture
def execution(profile):
    value = object.__new__(ResearchExecution)
    value._executor = SimpleNamespace(profile=profile[1])
    value._profile = profile[1]
    value._observer_key = b"h" * 32
    return value


@pytest.mark.parametrize("anchor", [False, True])
def test_real_seal_record_quote_and_anchored_input_restore_exactly_with_unchanged_raw_chain(profile, execution, anchor, monkeypatch):
    base = attempt(profile)
    brief, _packet, _binding = context()
    claim = brief["original_outcomes"][0]["eligible_questions"][0]["claims"][0]
    claim["evidence"] = [{"source_id": "lab:sample", "record_sha256": "sha256:" + "a" * 64,
        "quote": None if anchor else "Exact observation.", "anchor": {"pointer": "/section/observed", "expected": "Exact observation."} if anchor else None,
        "location_hint": None, "observed_at": "2026-10-01T00:00:00Z", "capture": "primary"}]
    packet, binding = create(brief, context_id="bounded", order_seed=b"b" * 32)
    supplied = {"purpose": "readability", "packet": packet}
    job = base.job | {"id": base.attempt_id, "context_id": packet["context_id"], "packet_sha256": digest(supplied).removeprefix("sha256:")}
    current = ExecutionAttempt.create(attempt_id=base.attempt_id, nonce=base.nonce, run_id=base.run_id,
        binding_id=base.binding_id, profile=profile[1], config_ref="readability", job=job, input_document=supplied)
    wire = stored_attempt(current, profile[1])
    assert wire["input"]["schema_version"] == SEALED_INPUT
    assert execution.attempt(wire).document() == current.document()
    row = {"attempt": wire, "reservation": {"attempt_id": current.attempt_id, "job_id": current.job["id"],
        "controller_ref": "session-observer", "issued_at": current.job["issued_utc"],
        "reservation": {**current.job["budgets"], "seconds": str(current.job["timeout_seconds"])},
        "cost_unit": {"currency": profile[1].currency, "pricing_ref": profile[1].pricing_ref}, "required_slots": ["dispatch", "result", "usage"]},
        "purpose": "readability", "original_id": current.job["scope"][0], "claim_id": None,
        "readability_parent": {"basis_id": current.job["basis_id"], "substance_sha256": binding["substance_sha256"],
            "binding": binding, "order_seed": (b"b" * 32).hex(), "plan_sha256": profile[1].readability.sha256, "sources": []}}
    probe = {"schema_version": "research-readability-probe/v1", "profile_sha256": profile[1].sha256,
        "registry_sha256": "b" * 64, "semantic_basis_id": "sha256:" + "b" * 64, "expires_utc": "2026-10-01T00:00:30Z",
        "observed_utc": "2026-10-01T00:00:00Z", "configuration": None, "reasons": []}
    preparation = {"attempt_record": row, "probe_record": {"probe": probe, "domain_ref": "sha256:" + "c" * 64},
        "predecessor_records_ref": digest([])}
    encoded = stored_preparation(preparation, execution)
    assert restored_preparation(encoded, execution) == preparation
    for defect in ("alias", "target", "source", "configuration", "reason", "timestamp", "digest", "reservation", "reservation_type", "attempt"):
        malformed = copy.deepcopy(preparation)
        if defect == "alias":
            next(iter(malformed["attempt_record"]["readability_parent"]["binding"]["aliases"].values()))["kind"] = []
        elif defect == "target":
            malformed["attempt_record"]["readability_parent"]["binding"]["field_targets"]["/missing"] = []
        elif defect == "source":
            malformed["attempt_record"]["readability_parent"]["sources"] = [{"source_id": []}]
        elif defect == "reservation":
            malformed["attempt_record"]["reservation"]["attempt_id"] = "substituted"
        elif defect == "reservation_type":
            malformed["attempt_record"]["reservation"]["reservation"]["model_calls"] = True
        elif defect == "attempt":
            malformed["attempt_record"]["attempt"].pop("nonce")
        else:
            field = {"configuration": "configuration", "reason": "reasons", "timestamp": "expires_utc", "digest": "registry_sha256"}[defect]
            malformed["probe_record"]["probe"][field] = {"unapproved": True} if defect == "configuration" else [None] if defect == "reason" else "invalid"
        with pytest.raises(UsageError) as invalid:
            restored_preparation(_persist(malformed, PREPARATION), execution)
        assert invalid.value.error_code == "READABILITY_REFUSED"
    session = SimpleNamespace(binding={}, execution=execution)
    first = execution.seal_record(session, "execution_advance", input_owner.native.child_id(current.attempt_id, "advance_intent"),
        {"advance_id": current.attempt_id, "readability_preparation": encoded}, records=[])
    probe_row = execution.seal_record(session, "execution_readability_probe", input_owner.native.child_id(current.attempt_id, "readability_probe"),
        preparation["probe_record"], records=[first])
    second = execution.seal_record(session, "execution_attempt", input_owner.native.child_id(current.attempt_id, "intent"), row, records=[first, probe_row])
    for record in (first, probe_row, second):
        check_persisted_record(record)
        assert len(owner("run_controller").compact_json(record).encode()) <= 262144
        execution._verify(record)
    assert second["previous_record_ref"] == digest(probe_row) and probe_row["previous_record_ref"] == digest(first)
    assert first["data"]["readability_preparation"] == encoded
    monkeypatch.setattr(input_owner.native, "retained", lambda *_: [first, probe_row, second])
    token = input_owner._INPUT_DISPATCH.set(None)
    try:
        input_owner._admit_input_dispatch(session, current.attempt_id, current)
        admitted = input_owner.take_input_dispatch(session, current)
        assert admitted.state == "transferred"
        with pytest.raises(UsageError):
            input_owner.take_input_dispatch(session, current)
        input_owner._INPUT_DISPATCH.set(None)
        monkeypatch.setattr(input_owner.native, "retained", lambda *_: [first, probe_row])
        with pytest.raises(UsageError):
            input_owner._admit_input_dispatch(session, current.attempt_id, current)
    finally:
        input_owner._INPUT_DISPATCH.reset(token)
    altered = copy.deepcopy(second)
    altered["data"]["attempt"]["nonce"] = "another-nonce"
    with pytest.raises(UsageError):
        execution._verify(altered)


@pytest.mark.parametrize("mutation", ["hash", "duplicate", "noncanonical", "bad_type", "depth", "keys", "integer", "surrogate"])
def test_strict_storage_rejects_tamper_and_noncanonical_parse_before_use(mutation):
    value = _persist({"purpose": "readability", "packet": {}}, SEALED_INPUT)
    if mutation == "hash":
        value["sha256"] = "0" * 64
    elif mutation == "bad_type":
        value["canonical_json"] = []
    elif mutation == "surrogate":
        value["canonical_json"] = "\ud800"
    else:
        text = {"duplicate": '{"purpose":"readability","purpose":"readability","packet":{}}',
            "noncanonical": '{ "packet": {}, "purpose": "readability" }',
            "keys": json.dumps({str(index): index for index in range(129)}, separators=(",", ":"), sort_keys=True),
            "integer": '{"integer":' + str(1 << 257) + '}',
            "depth": '{"deep":' + '[' * 17 + '0' + ']' * 17 + '}'}[mutation]
        value["canonical_json"] = text
        value["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    with pytest.raises(UsageError) as refused:
        _restore(value, SEALED_INPUT)
    assert refused.value.error_code == "READABILITY_REFUSED"


def test_full_escaped_native_record_bound_and_storage_string_bound_are_not_raised():
    with pytest.raises(UsageError):
        _persist({"raw": "x" * 65536}, PREPARATION)
    value = {"text": "😀" * 30000}
    assert len(canonical(value)) < 262144
    assert len(owner("run_controller").compact_json(value).encode()) > 262144
    with pytest.raises(UsageError):
        check_persisted_record(value)
    with pytest.raises(UsageError):
        detached({"text": "x" * 1_048_576})


def test_unselected_legacy_attempt_wire_and_hash_are_unchanged(profile, execution):
    value = attempt(profile).document()
    # Old ordinary inputs remain raw and follow the same reconstruction/equality.
    value["input"] = {"legacy": "unchanged"}
    legacy = SimpleNamespace(document=lambda: detached(value), input_document={"legacy": "unchanged"})
    before = canonical(value)
    assert canonical(stored_attempt(legacy, profile[0])) == before
