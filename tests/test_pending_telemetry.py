"""Pending control history is correlated without inventing execution telemetry."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta

import pytest

from evidence_wiki import telemetry
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.research_session import ResearchSession
from tests.test_public_telemetry import GRANTS
from tests.test_research_session import host, policy
from tests.test_telemetry import session_root as _session_root


@pytest.fixture
def root(tmp_path):
    return _session_root.__wrapped__(tmp_path)


def settled(root):
    session = ResearchSession(root, policy(paid=True), run_id="run")
    original = session.current()["accounting"]["original_outcomes"][0]["original_text"]
    session.host_job(request_id="job", operation="generate", payload={"question": original})
    session.reconcile_pending(request_id="settle")
    return session


def test_real_pending_control_receipt_is_not_a_step_or_executed_attempt(root, tmp_path):
    with host(root, tmp_path, grants=GRANTS).activate():
        session = settled(root)
        retained = session.pending()
        before = (root / "runs/run/events.jsonl").read_bytes()
        report = telemetry.report(root, run_id="run")
        assert retained["state"] == "unknown" and retained["reservation_occupied"]
        assert report["metrics"] == [] and report["spans"] == [] and report["trace"] == []
        assert report["coverage"]["expected_job_ids"] == ["job"]
        assert report["coverage"]["missing_job_ids"] == ["job"]
        assert report["observed_human_interventions"] is None and not report["coverage"]["whole_execution_authenticated"]
        assert any("control state" in row for row in report["limitations"])
        assert (root / "runs/run/events.jsonl").read_bytes() == before


@pytest.mark.parametrize("field", ["job_sha256", "binding_id", "run_id", "request_id", "issued_utc", "job_deadline_utc", "run_deadline_utc"])
def test_pending_receipt_cannot_rebind_stored_work_or_deadline(root, tmp_path, monkeypatch, field):
    with host(root, tmp_path, grants=GRANTS).activate():
        settled(root)
        original_call = telemetry.call_seam
        snapshot = original_call(telemetry.owner, "run_controller", "run_telemetry_events", root, run_id="run")
        edited = copy.deepcopy(snapshot)
        receipt = next(row["data"]["record"]["receipt"] for row in edited["events"]
                       if row["event_type"] == "custom.research.pending")
        if field in {"job_sha256", "binding_id"}:
            receipt[field] = "sha256:" + "2" * 64
        elif field in {"run_id", "request_id"}:
            receipt[field] = "other"
        elif field == "issued_utc":
            receipt[field] = (datetime.fromisoformat(receipt[field]) - timedelta(seconds=1)).isoformat()
        elif field == "job_deadline_utc":
            receipt[field] = (datetime.fromisoformat(receipt[field]) - timedelta(seconds=1)).isoformat()
            receipt["effective_deadline_utc"] = min(receipt[field], receipt["run_deadline_utc"])
        else:
            receipt[field] = (datetime.fromisoformat(receipt[field]) + timedelta(seconds=1)).isoformat()
        before = (root / "runs/run/events.jsonl").read_bytes()
        monkeypatch.setattr(telemetry, "call_seam", lambda *args, **kwargs: edited)
        with pytest.raises(EvidenceWikiError):
            telemetry.report(root, run_id="run")
        assert (root / "runs/run/events.jsonl").read_bytes() == before


@pytest.mark.parametrize("field", ["research_session", "started_at"])
@pytest.mark.parametrize("form", ["missing", "malformed"])
def test_edited_bounded_state_basis_refuses_with_typed_conflict_and_unchanged_journal(root, tmp_path, field, form):
    with host(root, tmp_path, grants=GRANTS).activate():
        settled(root)
        state_path = root / "runs/run/run-state.json"
        state = json.loads(state_path.read_bytes())
        if form == "missing":
            del state[field]
        else:
            state[field] = {} if field == "research_session" else "unparseable-clock"
        state_path.write_text(json.dumps(state))
        before = (root / "runs/run/events.jsonl").read_bytes()
        with pytest.raises(EvidenceWikiError) as error:
            telemetry.report(root, run_id="run")
        assert error.value.error_code == "TELEMETRY_JOB_CONFLICT"
        assert (root / "runs/run/events.jsonl").read_bytes() == before


def test_changed_state_after_captured_pending_basis_refuses_without_journal_effect(root, tmp_path, monkeypatch):
    with host(root, tmp_path, grants=GRANTS).activate():
        settled(root)
        original = telemetry._pending_state
        observations = 0
        def changing(*args):
            nonlocal observations
            observations += 1
            state = original(*args)
            if observations == 2:
                state["updated_at"] = "different-observation"
            return state
        monkeypatch.setattr(telemetry, "_pending_state", changing)
        before = (root / "runs/run/events.jsonl").read_bytes()
        with pytest.raises(EvidenceWikiError) as error:
            telemetry.report(root, run_id="run")
        assert error.value.error_code == "TELEMETRY_JOB_CONFLICT"
        assert observations == 2 and (root / "runs/run/events.jsonl").read_bytes() == before


def test_retained_terminal_control_history_cannot_rewrite_last_observation(root, tmp_path, monkeypatch):
    with host(root, tmp_path, grants=GRANTS).activate():
        settled(root)
        snapshot = telemetry.call_seam(telemetry.owner, "run_controller", "run_telemetry_events", root, run_id="run")
        previous = next(row for row in snapshot["events"] if row["event_type"] == "custom.research.pending")
        changed = copy.deepcopy(previous)
        changed["event_id"] = "forged-later-event"
        changed["data"]["request_id"] = "later-control"
        receipt = changed["data"]["record"]["receipt"]
        receipt["last_observed_utc"] = (datetime.fromisoformat(receipt["last_observed_utc"]) + timedelta(seconds=1)).isoformat()
        snapshot["events"].append(changed)
        snapshot["count"] += 1
        monkeypatch.setattr(telemetry, "call_seam", lambda *args, **kwargs: snapshot)
        with pytest.raises(EvidenceWikiError) as error:
            telemetry.report(root, run_id="run")
        assert error.value.error_code == "TELEMETRY_JOB_CONFLICT"
