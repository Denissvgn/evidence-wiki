"""Owned scan bytes preserve independent canonical and authentication checks."""
from __future__ import annotations

import copy
import hmac
from types import SimpleNamespace

import pytest

from evidence_wiki import research_journal_contracts as codec
from evidence_wiki import research_session
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.mutation_contracts import _serialized
from tests.test_research_journal_bounds import observer
from tests.test_review_native_journal_wire import _owned_data


@pytest.mark.parametrize("large", [False, True])
def test_scan_rechecks_owned_records_and_authentication(tmp_path, monkeypatch, large):
    execution = observer()
    session = SimpleNamespace(root=tmp_path, run_id="scan", binding={"run_id": "scan"})
    original = _owned_data() if large else {"nested": ["original"]}
    if large:
        original["padding"].append("x" * 60000)
    first = execution.seal_record(session, "execution_native", "one", original, records=[])
    second = execution.seal_record(session, "execution_native", "two", {}, records=[first])
    first = codec.storage_record(first)
    events = [{"event_id": str(i), "data": {"request_id": row["request_id"], "record": row}}
              for i, row in enumerate((first, second))]
    state = tmp_path / "state"
    state.write_text("{}")
    proxy = SimpleNamespace(run_state_path=lambda *_: state, events_path=lambda *_: state,
        _session_state=lambda *_: {"research_session": session.binding},
        _bounded_journal=lambda *_: {"events": events})
    monkeypatch.setattr(research_session, "_load", lambda *_: proxy)
    original_hmac = hmac.new
    hmac_calls = []
    def fresh_hmac(*args, **kwargs):
        hmac_calls.append(args[0])
        return original_hmac(*args, **kwargs)
    monkeypatch.setattr(hmac, "new", fresh_hmac)
    original_verify = execution._verify
    calls = []
    def verify(record):
        calls.append(record["request_id"])
        return original_verify(record)
    monkeypatch.setattr(execution, "_verify", verify)
    first_read = execution.records(session)
    assert calls == ["one", "two"]
    first_read[0]["data"]["padding" if large else "nested"][0] = "caller mutation"
    assert execution.records(session)[0]["data"] == original
    assert calls == ["one", "two", "one", "two"]
    assert hmac_calls == [execution._observer_key] * 4
    for index, field, value in ((1, "previous_record_ref", "sha256:" + "0" * 64),
            (0, "authentication", "0" * 64), (0, "data", {"changed": True})):
        saved = copy.deepcopy(events)
        events[index]["data"]["record"][field] = value
        for _ in range(2):
            with pytest.raises(EvidenceWikiError):
                execution.records(session)
        events[:] = saved
    execution._observer_key = b"different-observer"
    with pytest.raises(EvidenceWikiError):
        execution.records(session)


@pytest.mark.parametrize("size", [262143, 262144, 262145])
@pytest.mark.parametrize("authentication", [None, "a" * 64])
def test_each_header_threshold_and_owned_logical_independence(size, authentication):
    record = {"schema_version": codec.JOURNAL, "kind": "execution_native", "binding_id": "binding",
        "request_id": "record", "sequence": 0, "previous_record_ref": None,
        "data": _owned_data(), "authentication": authentication}
    record["data"]["padding"][-1] = "x" * (size - len(_serialized(record)))
    assert len(_serialized(record)) == size
    raw, logical = codec._canonical_owned_record(record)
    assert raw == codec.canonical_record(record)
    assert logical == codec.detached_record(record)
    assert len(raw) <= 262144
    logical["data"]["padding"][0] = "caller mutation"
    assert codec._canonical_owned_record(record)[0] == raw
    for header in ({"authentication": None}, {"previous_record_ref": "sha256:" + "f" * 64}):
        variant = {**record, **header}
        assert codec._canonical_owned_record(variant) == (codec.canonical_record(variant), codec.detached_record(variant))
