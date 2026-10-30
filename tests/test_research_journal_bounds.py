"""Native header and collection overhead preserve complete bounded payloads."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_reporting, research_session, resolution_operations
from evidence_wiki import mutation_contracts as generic
from evidence_wiki import research_journal_contracts as journal
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.research_execution import ResearchExecution
from evidence_wiki.run_policy import RunPolicy
from tests.test_execution_reporting import PROFILE, native_intent
from tests.test_resolution_proof_binding import binding as binding
from tests.test_run_policy import policy_document


def nested(depth):
    result = "complete original observation"
    for _ in range(depth - 1):
        result = {"nested": result}
    return result


def native_data():
    value = native_intent()
    value["arguments"] = {"envelope": {"payload": {
        "action": "register-strict-review", "observation": nested(12)}}}
    return generic.detached(value)


def observer():
    execution = object.__new__(ResearchExecution)
    execution._observer_key = b"controlled-native-journal-observer"
    return execution


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def test_legacy_record_and_collection_canonical_bytes_hmac_and_refs_are_identical():
    execution = observer()
    session = SimpleNamespace(binding={"run_id": "journal"})
    first = execution.seal_record(session, "execution_native", "original", native_intent(), records=[])
    unsigned = {**first, "authentication": None}
    assert first["authentication"] == hmac.new(execution._observer_key, generic.canonical(unsigned), hashlib.sha256).hexdigest()
    assert journal.canonical_record(first) == generic.canonical(first)
    assert journal.digest_record(first) == generic.digest(first)
    second = execution.seal_record(session, "execution_native_observation", "next", {"result": "complete"}, records=[first])
    assert second["previous_record_ref"] == generic.digest(first)
    records = [first | {"event_ref": "event-1"}, second | {"event_ref": "event-2"}]
    assert journal.canonical_records(records) == generic.canonical(records)
    assert journal.digest_records(records) == generic.digest(records)


def test_boundary_sign_chain_actual_event_reader_and_public_report(tmp_path, monkeypatch):
    execution = observer()
    session = SimpleNamespace(root=tmp_path, run_id="journal", binding={"run_id": "journal"})
    data = native_data()
    original = copy.deepcopy(data)
    first = execution.seal_record(session, "execution_native", "boundary", data, records=[])
    assert first["data"] == original
    with pytest.raises(generic.MutationError):
        generic.detached(first)
    execution._verify(first)
    assert journal.canonical_record(first) == encoded(first)
    assert first["authentication"] == hmac.new(execution._observer_key, encoded({**first, "authentication": None}), hashlib.sha256).hexdigest()
    second = execution.seal_record(session, "execution_native_observation", "next", {"result": "complete"}, records=[first])
    assert second["previous_record_ref"] == "sha256:" + hashlib.sha256(encoded(first)).hexdigest()
    folder = tmp_path / "runs/journal"
    folder.mkdir(parents=True)
    state_path, events_path = folder / "run-state.json", folder / "events.jsonl"
    state_path.write_text("{}")
    events = [{"event_id": "event-" + str(index), "run_id": "journal", "agent_id": "host",
               "event_type": "custom.research.decision", "data": {"request_id": record["request_id"], "record": record}}
              for index, record in enumerate((first, second))]
    events_path.write_bytes(b"\n".join(encoded(event) for event in events) + b"\n")
    controller = owner("run_controller")
    proxy = SimpleNamespace(run_state_path=lambda *_: state_path, events_path=lambda *_: events_path,
                            _session_state=lambda *_: {"research_session": session.binding},
                            _bounded_journal=controller._bounded_journal)
    monkeypatch.setattr(research_session, "_load", lambda *_: proxy)
    records = execution.records(session)
    assert [row["data"] for row in records] == [original, second["data"]]
    assert journal.canonical_records(records) == encoded(records)
    report = execution_reporting.report(RunPolicy(policy_document()), PROFILE, generic.digest(session.binding),
                                        session.run_id, records, None)
    assert report["chain_complete"] and report["record_count"] == 2
    assert report["trace"][0]["record_ref"] == journal.digest_record(first)
    assert b"complete original observation" not in generic.canonical(report)
    third = execution.seal_record(session, "execution_native", "later", {}, records=None)
    assert third["sequence"] == 2 and third["previous_record_ref"] == journal.digest_record(second)
    records[0]["data"].clear()
    assert execution.records(session)[0]["data"] == original


def test_complete_resolution_proof_uses_the_same_owned_record_codec(binding):
    from evidence_wiki import execution_session as native

    session, records, events, _state = binding
    record = session.execution.seal_record(session, "execution_native", "boundary", native_data(), records=records)
    records.append(record | {"event_ref": "boundary-event"})
    events.append({"event_id": "boundary-event", "data": {"request_id": "boundary", "record": record}})
    with native._current_facts(session) as facts:
        proof = resolution_operations._journal_proof(session, facts)
    raw = journal.canonical_records(records)
    assert proof["canonical_sha256"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert proof["canonical_bytes"] == len(raw)
    assert proof["ordered_records"][-1]["chain_record_ref"] == journal.digest_record(record)


def test_native_qualification_reader_preserves_two_boundary_records_and_source_pins(tmp_path):
    from tools._terminal_qualification import native_journal_records

    execution = observer()
    session = SimpleNamespace(binding={"run_id": "journal"})
    first = execution.seal_record(session, "execution_native", "first", native_data(), records=[])
    second = execution.seal_record(session, "execution_native", "second", native_data(), records=[first])
    state = {"run_id": "journal", "agent_id": "host", "research_session": session.binding}
    events = [{"event_id": "event-" + str(index), "run_id": "journal", "agent_id": "host",
               "event_type": "custom.research.decision", "data": {"request_id": record["request_id"], "record": record}}
              for index, record in enumerate((first, second))]
    raw = b"".join(encoded(event) + b"\n" for event in events)
    state_raw = encoded(state)
    folder = tmp_path / "runs/journal"
    snapshot = {"run_id": "journal", "source_path": str(folder / "events.jsonl"),
                "state_path": str(folder / "run-state.json"), "state": state,
                "original_state_json": state_raw.decode(), "state_sha256": hashlib.sha256(state_raw).hexdigest(),
                "events": events, "source_sha256": hashlib.sha256(raw).hexdigest()}
    assert native_journal_records(snapshot, str(tmp_path)) == [first, second]
    assert b"".join(encoded(event) + b"\n" for event in snapshot["events"]) == raw
    snapshot["events"][1]["data"]["record"]["previous_record_ref"] = "sha256:" + "0" * 64
    snapshot["source_sha256"] = hashlib.sha256(b"".join(encoded(event) + b"\n" for event in events)).hexdigest()
    with pytest.raises(ValueError, match="qualification_native_journal_chain"):
        native_journal_records(snapshot, str(tmp_path))


@pytest.mark.parametrize("kind", ["depth", "bytes", "nodes", "cycle", "extra", "header_depth", "wrong_schema"])
def test_wrapper_codec_cannot_expand_untrusted_payload_or_header_limits(kind):
    execution = observer()
    record = execution.seal_record(SimpleNamespace(binding={}), "execution_native", "boundary", native_data(), records=[])
    if kind == "depth":
        record["data"]["arguments"]["envelope"]["payload"]["observation"] = nested(13)
    elif kind == "bytes":
        record["data"] = {"many": ["x" * 65536] * 17}
    elif kind == "nodes":
        record["data"] = {"many": [["x"] * 4096 for _ in range(17)]}
    elif kind == "cycle":
        record["data"]["cycle"] = record
    elif kind == "extra":
        record["unexpected"] = "untrusted header"
    elif kind == "header_depth":
        record["kind"] = nested(17)
    else:
        record["schema_version"] = "future/v9"
    with pytest.raises(generic.MutationError):
        journal.canonical_record(record)


@pytest.mark.parametrize("kind", ["bytes", "nodes", "items", "record_items"])
def test_collection_keeps_original_aggregate_limits(kind):
    execution = observer()
    payload = {"text": "x" * 60000} if kind == "bytes" else {"rows": [None] * 4000}
    record = execution.seal_record(SimpleNamespace(binding={}), "bounded_record", "original", payload, records=[])
    if kind == "record_items":
        record["data"] = [None] * 4097
        records = [record]
    else:
        records = [record] * (4097 if kind == "items" else 18)
    with pytest.raises(generic.MutationError):
        journal.canonical_records(records)


@pytest.mark.parametrize("target", ["data", "authentication", "previous_record_ref", "sequence", "binding_id"])
def test_complete_boundary_record_authentication_still_detects_tampering(target):
    execution = observer()
    record = execution.seal_record(SimpleNamespace(binding={}), "execution_native", "boundary", native_data(), records=[])
    if target == "data":
        record["data"]["arguments"]["envelope"]["payload"]["observation"] = "substituted"
    else:
        record[target] = 1 if target == "sequence" else "0" * 64
    with pytest.raises(EvidenceWikiError):
        execution._verify(record)
