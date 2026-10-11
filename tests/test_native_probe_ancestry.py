"""Fresh authenticated witnesses extend a business journal without rewriting work."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_wiki.research_execution import ResearchExecution
from tools._terminal_qualification import native_journal_records, native_read_epoch, probe_only_extension


def probe():
    return {"schema_version": "research-qualification-probe/v1", "profile_sha256": "a" * 64,
        "registry_sha256": "b" * 64, "semantic_basis_id": "sha256:" + "c" * 64,
        "observed_utc": "2026-10-11T03:00:00+00:00", "expires_utc": "2026-10-11T03:00:30+00:00",
        "configurations": {name: {"config_ref": name, "active": True, "execution_kind": "model"}
            for name in ("evidence", "scope", "critic", "adjudicator")}, "reasons": [], "configuration_reasons": {}}


def append(snapshot, kind, data, request_id):
    execution = object.__new__(ResearchExecution)
    execution._observer_key = b"native-journal-controlled-observer!"
    session = SimpleNamespace(binding=snapshot["state"]["research_session"])
    records = [{**event["data"]["record"], "event_ref": event["event_id"]} for event in snapshot["events"]]
    record = execution.seal_record(session, kind, request_id, data, records=records)
    execution._verify(record)
    timestamp = "2026-10-11T03:00:10+00:00"
    snapshot["events"].append({"schema_version": "1.0", "event_id": f"evt-{len(records)+1:04d}",
        "run_id": "business", "agent_id": "host", "event_type": "custom.research.decision",
        "from_state": None, "to_state": None, "message": "Inert research decision recorded.",
        "occurred_at": timestamp, "data": {"request_id": request_id, "record": record}})
    snapshot["state"]["updated_at"] = timestamp
    refresh_hash(snapshot)
    return execution


def refresh_hash(snapshot):
    raw = b"".join((json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for item in snapshot["events"])
    snapshot["source_sha256"] = hashlib.sha256(raw).hexdigest()
    snapshot["original_state_json"] = json.dumps(snapshot["state"],sort_keys=True)
    snapshot["state_sha256"] = hashlib.sha256(snapshot["original_state_json"].encode()).hexdigest()


def baseline():
    snapshot = {"run_id": "business", "source_path": "/owned/runs/business/events.jsonl",
        "state_path": "/owned/runs/business/run-state.json", "source_sha256": "", "state_sha256": "d" * 64,
        "state": {"run_id": "business", "agent_id": "host", "research_session": {"run_id": "business"},
            "updated_at": "2026-10-11T03:00:00+00:00", "original_question": "Unchanged original."}, "events": []}
    append(snapshot, "execution_attempt", {"attempt_id": "original", "input": "Original input."}, "original-work")
    append(snapshot, "execution_qualification_probe", {"probe": probe(), "domain_ref": "sha256:" + "e" * 64,
        "completed_utc": "2026-10-11T03:00:05+00:00"}, "original-probe")
    return snapshot


def extended():
    original = baseline()
    current = deepcopy(original)
    fresh = probe()
    fresh.update(observed_utc="2026-10-11T03:00:05+00:00", expires_utc="2026-10-11T03:00:35+00:00")
    append(current, "execution_qualification_probe", {"probe": fresh, "domain_ref": "sha256:" + "e" * 64,
        "completed_utc": "2026-10-11T03:00:10+00:00"}, "fresh-probe")
    return original, current


def test_real_native_owner_signed_probe_only_extension_preserves_all_business_bytes():
    original, current = extended()
    probe_only_extension(original, current, "/owned")
    assert native_journal_records(current, "/owned")[:2] == native_journal_records(original, "/owned")


@pytest.mark.parametrize("change", ["prefix", "business_state", "nonprobe", "gap", "event_scope", "binding", "duplicate_request"])
def test_business_rewrite_nonprobe_scope_and_chain_gap_refuse(change):
    original, current = extended()
    last = current["events"][-1]
    if change == "prefix":
        current["events"][0]["data"]["record"]["data"]["input"] = "Replaced."
    elif change == "business_state":
        current["state"]["original_question"] = "Replaced."
    elif change == "nonprobe":
        last["data"]["record"]["kind"] = "execution_application"
    elif change == "gap":
        last["data"]["record"]["sequence"] += 1
    elif change == "event_scope":
        last["run_id"] = "other"
    elif change == "binding":
        last["data"]["record"]["binding_id"] = "sha256:" + "f" * 64
    else:
        last["data"]["record"]["request_id"] = "original-probe"
        last["data"]["request_id"] = "original-probe"
    refresh_hash(current)
    with pytest.raises(ValueError):
        probe_only_extension(original, current, "/owned")


@pytest.mark.parametrize("change", ["expired", "longer", "withdrawn", "config", "domain", "profile", "registry"])
def test_original_witness_window_and_exact_current_configuration_remain_required(change):
    original, current = extended()
    data = current["events"][-1]["data"]["record"]["data"]
    if change == "expired":
        data["completed_utc"] = data["probe"]["expires_utc"]
    elif change == "longer":
        data["probe"]["expires_utc"] = "2026-10-11T03:01:05+00:00"
    elif change == "withdrawn":
        data["probe"]["configurations"]["critic"]["active"] = False
    elif change == "config":
        data["probe"]["configurations"]["critic"]["config_ref"] = "another"
    else:
        field = {"domain": "semantic_basis_id", "profile": "profile_sha256", "registry": "registry_sha256"}[change]
        data["probe"][field] = "changed"
    refresh_hash(current)
    with pytest.raises(ValueError, match="witness_extension_invalid"):
        probe_only_extension(original, current, "/owned")


def test_forged_tail_signature_cannot_become_original_producer_authenticated_journal():
    _original, current = extended()
    forged = current["events"][-1]["data"]["record"]
    forged["authentication"] = "f" * 64
    execution = object.__new__(ResearchExecution)
    execution._observer_key = b"native-journal-controlled-observer!"
    with pytest.raises(Exception) as error:
        execution._verify(forged)
    assert error.value.error_code == "RESEARCH_EXECUTION_OBSERVATION_UNAUTHENTICATED"


def test_current_read_epoch_must_finish_inside_real_receipt_and_unexpanded_witness():
    _original, current = extended()
    result = {"workspace": "/owned", "journal": current, "current": {"qualification": {"state": "qualified"}},
        "read_started_utc": "2026-10-11T03:00:11+00:00", "read_finished_utc": "2026-10-11T03:00:34+00:00"}
    receipt = {"started_at": "2026-10-11T03:00:00+00:00", "finished_at": "2026-10-11T03:00:40+00:00"}
    native_read_epoch(result, receipt)
    result["read_finished_utc"] = "2026-10-11T03:00:35+00:00"
    with pytest.raises(ValueError, match="read_epoch_expired"):
        native_read_epoch(result, receipt)


@pytest.mark.parametrize("change", ["none", "expired", "configuration", "source", "accounting", "review_age", "late_expiry"])
def test_closing_recomputes_actual_eligibility_without_comparing_volatile_publication_clock(change):
    from types import SimpleNamespace

    from tools.replay_qualification_native import closing_qualification

    original = {"state": "qualified", "profiles": [{"qualified": True, "config_ref": "original"}]}
    current = {"basis_id": "owned-input", "policy_id": "owned-policy", "accounting": {"accepted_count": 1},
        "qualification": original,
        "canonical_release": {"research_complete": True,
            "publication": {"evaluated_at": "2026-10-11T03:00:10+00:00", "accepted": True}}}
    closing = deepcopy(current)
    closing["canonical_release"]["publication"]["evaluated_at"] = "2026-10-11T03:00:11+00:00"
    if change == "accounting":
        closing["accounting"]["accepted_count"] = 0
    if change == "review_age":
        closing["canonical_release"]["publication"]["accepted"] = False
    if change == "late_expiry":
        closing["qualification"]["state"] = "unknown"
    fresh = {"state": "unknown", "profiles": []} if change == "expired" else deepcopy(original)
    if change == "configuration":
        fresh["profiles"][0]["config_ref"] = "changed"
    calls = []
    def read(session):
        calls.append(session)
        return fresh
    owner = SimpleNamespace(current_qualifications=read)
    session = SimpleNamespace(current=lambda: closing)
    before, after = {"original-input": "a" * 64}, {"original-input": "a" * 64}
    if change == "source":
        after["original-input"] = "b" * 64
    if change == "none":
        assert closing_qualification(owner, session, current, before, after) == closing
    else:
        match = "stable source hashes" if change == "source" else "closing current" if change in {
            "accounting", "review_age", "late_expiry"} else "current qualification"
        with pytest.raises(ValueError, match=match):
            closing_qualification(owner, session, current, before, after)
    assert calls == ([] if change == "source" else [session])
