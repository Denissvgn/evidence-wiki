"""Pure authenticated journal binding, with controlled current-proof owners."""

import copy
import hashlib
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import research_session
from evidence_wiki import resolution_operations as resolution
from evidence_wiki.agent_execution import ExecutionAttempt
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import MAX_BYTES, MutationError, canonical, detached, digest
from evidence_wiki.research_execution import ResearchExecution
from evidence_wiki.semantic_contracts import FINDINGS, schema_document
from tests.test_agent_execution import profile
from tests.test_workflow_contracts import job


@pytest.fixture
def binding(monkeypatch, tmp_path):
    state = {"config": {"selection": "original"}, "controls": {"controller": "original"},
        "packets": [{"source_id": "owned-source", "text": "Original source."}],
        "proofs": {"original": {"eligible": False}, "derived": {"eligible": False}},
        "constraints": [], "qualification": {"state": "current"}, "budget_overrun": False}
    class Session:
        root = tmp_path
        run_id = "owned-run"
        binding = {"run_id": run_id}
        context = SimpleNamespace(require_grant=lambda *_: None)
        policy = SimpleNamespace(to_dict=lambda: {"policy": "original"}, decide=lambda _value:
            {"outcome": "abstain", "reason": "unresolved", "selected_outcome_refs": []})
        def _check(self, *_args, **_kwargs):
            pass
    session = Session()
    execution = object.__new__(ResearchExecution)
    execution._observer_key = b"local-resolution-journal-fixture!"
    session.execution = execution
    attempt = ExecutionAttempt.create(attempt_id="owned-attempt", nonce="owned-nonce", run_id=session.run_id,
        binding_id=digest(session.binding), profile=profile(), config_ref="researcher", job=job(),
        input_document={"output_contract": schema_document(FINDINGS)})
    records, events = [], []
    for index, (kind, data) in enumerate([
        ("execution_attempt", {"attempt": {"job": attempt.job, "input": attempt.input_document}}),
        ("execution_observation", {"attempt_id": attempt.attempt_id, "state": "succeeded"}),
        ("execution_step", {"step": {"decision": {"terminal": True, "outcome": "abstained"}}}),
    ]):
        record = execution.seal_record(session, kind, "request-" + str(index), data, records=records)
        records.append(record | {"event_ref": "event-" + str(index)})
        events.append({"event_id": records[-1]["event_ref"], "data": {"request_id": record["request_id"], "record": record}})
    controller = SimpleNamespace(run_state_path=lambda *_: SimpleNamespace(exists=lambda: True),
        _session_state=lambda *_: {"research_session": session.binding},
        events_path=lambda *_: "controlled-journal", _bounded_journal=lambda *_: {"events": copy.deepcopy(events)})
    monkeypatch.setattr(research_session, "_load", lambda *_: controller)
    usage = SimpleNamespace(configured=lambda *_: False, current_view=lambda *_: nullcontext(None))
    def owner(name):
        if name == "verify_quotes":
            return SimpleNamespace(load_config=lambda *_: copy.deepcopy(state["config"]))
        if name == "_caller_context":
            return SimpleNamespace(capture=lambda *_: copy.deepcopy(state["controls"]))
        if name == "_evidence_usage":
            return usage
        pytest.fail("Unexpected owner: " + name)
    monkeypatch.setattr(native, "owner", owner)
    monkeypatch.setattr(resolution, "owner", owner)
    def observe(current):
        view = {"basis_id": digest({"controls": state["controls"]}), "qualification": copy.deepcopy(state["qualification"]),
            "run": {"terminal": True}}
        observed = SimpleNamespace(config={}, controls={"result": copy.deepcopy(state["controls"])}, finish=lambda: None)
        return view, observed, copy.deepcopy(state["packets"]), native.retained(current)
    monkeypatch.setattr(native, "_observe_current", observe)
    monkeypatch.setattr(native, "policy_observation", lambda *_: {"selected_outcomes": []})
    monkeypatch.setattr(native, "execution_budget_overrun", lambda *_: state["budget_overrun"])
    monkeypatch.setattr(resolution, "ResearchSession", Session)
    monkeypatch.setattr(resolution, "_Pass", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(resolution, "questions", lambda *_args, **_kwargs: copy.deepcopy(state["proofs"]))
    monkeypatch.setattr(resolution, "blockers", lambda *_: copy.deepcopy(state["constraints"]))
    monkeypatch.setattr(resolution, "_project", lambda **_kwargs:
        {"verdict": "abstain", "authorized": False, "external_action_authorized": False})
    return session, records, events, state


def test_owned_overdepth_contract_binds_exact_full_journal_without_outward_authority(binding):
    session, records, _events, state = binding
    # The input is a real validated attempt carrying the semantic output schema.
    for record in records:
        assert canonical(record)
    with pytest.raises(MutationError):
        digest({"records": records})
    expected_raw = b"[" + b",".join(canonical(record) for record in records) + b"]"
    with native._current_facts(session) as facts:
        proof = resolution._journal_proof(session, facts)
        assert proof["canonical_sha256"] == "sha256:" + hashlib.sha256(expected_raw).hexdigest()
        assert proof["canonical_bytes"] == len(expected_raw)
        assert proof["record_count"] == len(records)
        assert proof["ordered_records"] == [{"sequence": record["sequence"], "event_ref": record["event_ref"],
            "previous_record_ref": record["previous_record_ref"], "record_sha256": digest(record),
            "chain_record_ref": digest({key: value for key, value in record.items() if key != "event_ref"}),
            "canonical_bytes": len(canonical(record))} for record in records]
        assert proof["closing_chain_record_ref"] == proof["ordered_records"][-1]["chain_record_ref"]
        assert detached(proof) == proof
        assert resolution._journal_proof(session, facts) == proof
    with pytest.raises(UsageError):
        resolution._journal_proof(session, facts)
    with resolution._held_current(session) as held:
        assert held.document() == {"verdict": "abstain", "authorized": False, "external_action_authorized": False}
        expected = digest({"basis_id": digest({"controls": state["controls"]}), "journal": proof,
            "qualification": state["qualification"], "observation": {"selected_outcomes": []},
            "decision": session.policy.decide(None), "proofs": state["proofs"], "constraints": [], "config": state["config"]})
        assert held._proof_sha256 == expected
        assert held.source_packets() == state["packets"]
    with pytest.raises(UsageError):
        held.document()


@pytest.mark.parametrize("index", [0, 1, 2])
def test_tampering_any_original_record_refuses_native_opening(binding, index):
    session, _records, events, _state = binding
    events[index]["data"]["record"]["data"]["changed"] = True
    with pytest.raises(UsageError, match="refused"):
        with resolution._held_current(session):
            pytest.fail("Unauthenticated record reached owned custody.")


@pytest.mark.parametrize("change", ["input_schema", "output_schema", "chain", "order", "authentication", "binding", "sequence"])
def test_schema_chain_and_order_tampering_never_becomes_a_commitment(binding, change):
    session, _records, events, _state = binding
    record = events[0]["data"]["record"]
    if change == "input_schema":
        record["data"]["attempt"]["input"]["unexpected"] = {"type": "integer"}
    elif change == "output_schema":
        record["data"]["attempt"]["input"]["output_contract"]["properties"]["checks"]["type"] = "string"
    elif change == "chain":
        events[1]["data"]["record"]["previous_record_ref"] = "sha256:" + "0" * 64
    elif change == "order":
        events.reverse()
    elif change == "authentication":
        record["authentication"] = "0" * 64
    elif change == "binding":
        record["binding_id"] = "sha256:" + "0" * 64
    else:
        record["sequence"] = 99
    with pytest.raises(UsageError):
        with resolution._held_current(session):
            pytest.fail("Changed original facts reached owned custody.")


@pytest.mark.parametrize("change", ["record", "source", "config", "controls", "qualification", "derived_proof", "constraints", "budget"])
def test_owned_closure_rechecks_every_current_proof_without_caching(binding, change):
    session, _records, events, state = binding
    with pytest.raises(UsageError):
        with resolution._held_current(session) as held:
            assert held.document()["verdict"] == "abstain"
            if change == "record":
                events[1]["data"]["record"]["data"]["state"] = "failed"
            elif change == "source":
                state["packets"][0]["text"] = "Changed source."
            elif change == "config":
                state["config"]["selection"] = "changed"
            elif change == "controls":
                state["controls"]["controller"] = "changed"
            elif change == "qualification":
                state["qualification"]["state"] = "expired"
            elif change == "derived_proof":
                state["proofs"]["derived"]["eligible"] = True
            elif change == "constraints":
                state["constraints"].append("unsupported")
            else:
                state["budget_overrun"] = True


@pytest.mark.parametrize("change", ["input_schema", "output_schema", "event_ref", "append"])
def test_authenticated_changed_journal_is_still_refused_at_closing(binding, change):
    session, records, events, _state = binding
    with pytest.raises(UsageError) as caught:
        with resolution._held_current(session):
            if change == "event_ref":
                events[-1]["event_id"] = "different-owned-event"
            else:
                data = [copy.deepcopy(record["data"]) for record in records]
                if change == "input_schema":
                    data[0]["attempt"]["input"]["additional_contract"] = {"type": "string"}
                elif change == "output_schema":
                    data[0]["attempt"]["input"]["output_contract"]["properties"]["checks"]["type"] = "string"
                else:
                    data.append({"closing_fact": "new"})
                rebuilt = []
                events.clear()
                for index, value in enumerate(data):
                    kind = records[index]["kind"] if index < len(records) else "closing_record"
                    record = session.execution.seal_record(session, kind, "request-" + str(index), value, records=rebuilt)
                    rebuilt.append(record | {"event_ref": "event-" + str(index)})
                    events.append({"event_id": rebuilt[-1]["event_ref"],
                        "data": {"request_id": record["request_id"], "record": record}})
                assert native.retained(session) == rebuilt
    assert caught.value.details["reason"] == "resolution_current_inputs_changed"


def test_caller_commitments_cannot_substitute_for_held_native_facts(binding):
    session, _records, _events, _state = binding
    with pytest.raises(UsageError):
        resolution._journal_proof(session, SimpleNamespace(records=[], require=lambda *_: None))
    with native._current_facts(session) as facts:
        proof = resolution._journal_proof(session, facts)
        proof["ordered_records"].clear()
        assert resolution._journal_proof(session, facts)["record_count"] == 3


@pytest.mark.parametrize("limit", ["bytes", "nodes"])
def test_complete_journal_aggregate_limits_remain_bounded(binding, limit):
    session, _records, events, _state = binding
    records = []
    events.clear()
    # Each record is valid separately; the complete journal is still bounded.
    payload = {"text": "x" * 60000} if limit == "bytes" else {"rows": [None] * 4000}
    count = 18
    for index in range(count):
        record = session.execution.seal_record(session, "bounded_record", "large-" + str(index), payload, records=records)
        records.append(record | {"event_ref": "large-event-" + str(index)})
        events.append({"event_id": records[-1]["event_ref"], "data": {"request_id": record["request_id"], "record": record}})
    assert all(len(canonical(record)) < MAX_BYTES for record in records)
    with pytest.raises(UsageError) as caught:
        with native._current_facts(session) as facts:
            resolution._journal_proof(session, facts)
    assert caught.value.details["reason"] == "resolution_native_journal_limit"
