"""Serialized reservations and finite pending outcomes over real canonical runs."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event

import pytest

from evidence_wiki import Workspace
from evidence_wiki.mutation_contracts import detached, digest
from evidence_wiki.mutation_store import MutationStore
from evidence_wiki.research_session import ResearchSession
from tests.test_research_session import GRANTS, files, host, policy
from tests.test_research_session import root as root


def job(session):
    return session.host_job(request_id="job", operation="generate", payload={"question": "What does the evidence show?"})


def rows(root):
    return [json.loads(row) for row in (root / "runs/session/events.jsonl").read_text().splitlines()]


def clock(monkeypatch, value):
    import evidence_wiki.research_session as module
    class SelectedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)
    monkeypatch.setattr(module, "datetime", SelectedClock)


def test_retained_pending_is_inert_and_unavailable_executor_terminates(root, tmp_path):
    with host(root, tmp_path).activate(), Workspace.open(root) as workspace:
        session = workspace.research.session(policy(paid=True), run_id="session")
        assert session.pending() is None
        work = job(session)
        before = files(root)
        receipt = session.pending()
        assert receipt["state"] == "reserved" and receipt["job_sha256"] == digest(work)
        assert files(root) == before
        result = session.reconcile_pending(request_id="settle")
        assert result["state"] == "unknown" and result["fallback_reason"] == "executor_unavailable"
        assert result["terminal"] and result["reservation_occupied"] and result["completion_ref"] is None
        assert session.current()["run"]["terminal"]
        before = files(root)
        assert session.reconcile_pending(request_id="settle") == result
        assert files(root) == before
        assert not result["authorized"] and not result["evidence_accepted"]
        with pytest.raises(Exception) as error:
            session.host_job(request_id="new", operation="generate", payload=work["payload"])
        assert error.value.error_code == "RESEARCH_JOB_RESERVATION_PENDING"
        assert files(root) == before


def test_cancellation_is_unconfirmed_and_never_refunds_unknown_usage(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        result = session.cancel_pending(request_id="cancel")
        assert result["state"] == "unknown" and result["fallback_reason"] == "cancellation_unconfirmed"
        assert result["cancellation_requested_utc"] is not None
        assert result["completion_ref"] is None and result["reservation_occupied"]
        assert session.pending() == result
        assert session.cancel_pending(request_id="cancel") == result
        before = files(root)
        with pytest.raises(Exception) as error:
            session.reconcile_pending(request_id="cancel")
        assert error.value.error_code == "RESEARCH_REQUEST_CONFLICT"
        assert files(root) == before


def test_expiry_is_durable_after_restart_and_keeps_original_lease(root, tmp_path, monkeypatch):
    context = host(root, tmp_path)
    with context.activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        work = job(session)
        clock(monkeypatch, datetime.fromisoformat(work["deadline_utc"]) + timedelta(seconds=1))
        result = session.reconcile_pending(request_id="expire")
        assert result["state"] == "expired" and result["fallback_reason"] == "deadline_expired"
        assert result["job_deadline_utc"] == work["deadline_utc"] and result["reservation_occupied"]
        session.close()
        resumed = ResearchSession(root, policy(paid=True), run_id="session")
        assert resumed.pending() == result
        assert resumed.reconcile_pending(request_id="expire") == result
        assert resumed.host_job(request_id="job", operation="generate", payload=work["payload"]) == work


def test_clock_regression_preserves_retained_state(root, tmp_path, monkeypatch):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        work = job(session)
        clock(monkeypatch, datetime.fromisoformat(work["issued_utc"]) - timedelta(seconds=1))
        before = files(root)
        with pytest.raises(Exception) as error:
            session.reconcile_pending(request_id="past")
        assert "CLOCK" in error.value.error_code
        assert files(root) == before


@pytest.mark.parametrize("field", ["started_at", "updated_at"])
@pytest.mark.parametrize("value", ["bad", "2026-10-03T01:00:00", 0, None])
def test_malformed_canonical_clock_refuses_without_effect(root, tmp_path, field, value):
    import evidence_wiki.research_session as module
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        state_path = root / "runs/session/run-state.json"
        state = json.loads(state_path.read_bytes())
        state[field] = value
        state_path.write_text(json.dumps(state))
        before = files(root)
        with pytest.raises(Exception) as error:
            module._load(root, "run_controller")._session_state(root, "session")
        assert error.value.error_code == "RUN_STATE_INVALID"
        with pytest.raises(Exception) as error:
            session.pending()
        assert error.value.error_code in {"RUN_STATE_INVALID", "RESEARCH_INVALID"}
        with pytest.raises(Exception) as error:
            session.host_job(request_id="new", operation="generate", payload={"question": "What does the evidence show?"})
        assert error.value.error_code in {"RUN_STATE_INVALID", "RESEARCH_INVALID"}
        assert files(root) == before


def test_retained_clock_at_calendar_bound_refuses_without_raw_overflow(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        state_path = root / "runs/session/run-state.json"
        state = json.loads(state_path.read_bytes())
        state["started_at"] = "9999-12-31T23:59:59+00:00"
        state_path.write_text(json.dumps(state))
        before = files(root)
        with pytest.raises(Exception) as error:
            session.pending()
        assert error.value.error_code == "RESEARCH_CLOCK_INVALID"
        assert files(root) == before


def test_lost_terminal_return_replays_saved_pending_without_duplicate(root, tmp_path, monkeypatch):
    import evidence_wiki.research_session as module
    owner = module._owner
    def interrupted(project, seam, **kwargs):
        if seam == "run_session_transition":
            raise KeyboardInterrupt
        return owner(project, seam, **kwargs)
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        monkeypatch.setattr(module, "_owner", interrupted)
        with pytest.raises(KeyboardInterrupt):
            session.reconcile_pending(request_id="settle")
        assert session.pending()["terminal"]
        monkeypatch.setattr(module, "_owner", owner)
        result = session.reconcile_pending(request_id="settle")
        assert result["state"] == "unknown" and session.current()["run"]["terminal"]
        assert len([row for row in rows(root) if (row.get("data") or {}).get("request_id") == "settle"]) == 1


def test_advance_interruption_after_settlement_replays_original_precondition(root, tmp_path, monkeypatch):
    original = ResearchSession._settle_pending
    def interrupted(self, *args):
        original(self, *args)
        raise KeyboardInterrupt
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        basis = session.current()["basis_id"]
        monkeypatch.setattr(ResearchSession, "_settle_pending", interrupted)
        with pytest.raises(KeyboardInterrupt):
            session.advance(request_id="step", expected_basis_id=basis)
        monkeypatch.setattr(ResearchSession, "_settle_pending", original)
        result = session.advance(request_id="step", expected_basis_id=basis)
        assert result["historical"] and result["terminal"]
        assert len([row for row in rows(root) if row["event_type"] == "custom.research.decision"
                    and (row.get("data") or {}).get("request_id") == "step"]) == 1


def test_closed_writer_does_not_create_private_custody(root, tmp_path):
    context = host(root, tmp_path)
    with context.activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        session.close()
        before = {p.relative_to(context.mutation_control_directory): p.read_bytes()
                  for p in context.mutation_control_directory.rglob("*") if p.is_file()}
        with pytest.raises(Exception) as error:
            job(session)
        assert error.value.error_code == "RESEARCH_SESSION_CLOSED"
        assert {p.relative_to(context.mutation_control_directory): p.read_bytes()
                for p in context.mutation_control_directory.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("grant", ["run_controller.run_session_transition", "run_controller.run_session_finish"])
def test_job_preflights_terminal_permissions_before_start(root, tmp_path, grant):
    with host(root, tmp_path, grants=GRANTS - {grant}).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        before = files(root)
        with pytest.raises(Exception) as error:
            job(session)
        assert error.value.error_code == "HOST_CONTEXT_REFUSED"
        assert files(root) == before and not (root / "runs/session").exists()


@pytest.mark.parametrize("gate", ["workspace", "request"])
def test_gate_preserves_yielded_owner_io_error(root, tmp_path, gate):
    context = host(root, tmp_path)
    expected = OSError("canonical owner flush failure")
    with context.activate(), pytest.raises(OSError) as error:
        store = MutationStore(root, context.mutation_control_directory)
        with store.workspace_locked() if gate == "workspace" else store.locked("request"):
            raise expected
    assert error.value is expected


def test_job_clock_cannot_move_before_canonical_start(root, tmp_path, monkeypatch):
    context = host(root, tmp_path)
    with context.activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        with MutationStore(root, context.mutation_control_directory).workspace_locked():
            document = session._run()
        clock(monkeypatch, datetime.fromisoformat(document["started_at"].replace("Z", "+00:00")) - timedelta(seconds=1))
        before = files(root)
        with pytest.raises(Exception) as error:
            job(session)
        assert error.value.error_code == "RESEARCH_CLOCK_REGRESSED"
        assert files(root) == before


@pytest.mark.parametrize("change", ["deadline", "model_calls", "tokens", "cost", "controller", "attempts", "zero_calls"])
def test_native_first_reservation_checks_bound_policy(root, tmp_path, monkeypatch, change):
    import evidence_wiki.research_session as module
    original = module._owner
    def staged(project, seam, **kwargs):
        return {"event_id": "unpublished"} if seam == "run_session_record" else original(project, seam, **kwargs)
    context = host(root, tmp_path)
    with context.activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        monkeypatch.setattr(module, "_owner", staged)
        work = job(session)
        monkeypatch.setattr(module, "_owner", original)
        if change == "deadline":
            work["deadline_utc"] = (datetime.fromisoformat(work["issued_utc"]) + timedelta(seconds=600)).isoformat()
        elif change == "controller":
            work["controller_ref"] = "other-controller"
        elif change == "attempts":
            work["max_attempts"] = 2
        elif change == "zero_calls":
            work["budgets"]["model_calls"] = 0
        else:
            work["budgets"][change] = "2" if change == "cost" else work["budgets"][change] + 1
        before = files(root)
        with MutationStore(root, context.mutation_control_directory).workspace_locked(), pytest.raises(Exception) as error:
            original(root, "run_session_record", run_id="session", agent_id=context.actor_ref,
                request_id="job", binding=session.binding,
                record={"kind": "host_job", "job": work, "authorized": False, "evidence_accepted": False})
        assert error.value.error_code == "RUN_JOB_RESERVATION_INVALID"
        assert files(root) == before


def test_advance_automatically_settles_reserved_work(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        basis = session.current()["basis_id"]
        result = session.run_until_terminal(request_id="step", expected_basis_id=basis, max_steps=2)
        assert result["terminal"] and session.pending()["fallback_reason"] == "executor_unavailable"
        assert session.advance(request_id="step", expected_basis_id=basis)["historical"]


def test_valid_caller_id_cannot_collide_with_automatic_settlement(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        reserved_id = "settle-" + digest("step")[7:39]
        session.host_job(request_id=reserved_id, operation="generate", payload={"question": "What does the evidence show?"})
        basis = session.current()["basis_id"]
        result = session.advance(request_id="step", expected_basis_id=basis)
        assert result["terminal"] and session.advance(request_id="step", expected_basis_id=basis)["historical"]
        assert session.pending()["job_id"] == reserved_id


def test_terminal_cancellation_id_cannot_trap_a_new_advance(root, tmp_path):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        session.cancel_pending(request_id="step")
        basis = session.current()["basis_id"]
        before = files(root)
        with pytest.raises(Exception) as error:
            session.advance(request_id="step", expected_basis_id=basis)
        assert error.value.error_code == "RESEARCH_RUN_TERMINAL" and files(root) == before


@pytest.mark.parametrize("field", ["state", "run_id", "binding_id", "run_deadline_utc", "last_observed_utc"])
def test_public_pending_reader_refuses_edited_receipt_lineage(root, tmp_path, field):
    with host(root, tmp_path).activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        job(session)
        session.reconcile_pending(request_id="settle")
        retained = rows(root)
        receipt = next(row["data"]["record"]["receipt"] for row in retained if row["event_type"] == "custom.research.pending")
        if field == "state":
            receipt.update(state="completed", reservation_occupied=False, fallback_reason=None, completion_ref="sha256:" + "2" * 64)
        elif field == "binding_id":
            receipt[field] = "sha256:" + "2" * 64
        elif field == "run_id":
            receipt[field] = "foreign"
        elif field == "run_deadline_utc":
            receipt[field] = (datetime.fromisoformat(receipt[field]) + timedelta(seconds=1)).isoformat()
        else:
            receipt[field] = (datetime.fromisoformat(receipt["issued_utc"]) - timedelta(seconds=1)).isoformat()
        journal = root / "runs/session/events.jsonl"
        journal.write_text("".join(json.dumps(row) + "\n" for row in retained))
        before = files(root)
        with pytest.raises(Exception) as error:
            session.pending()
        assert error.value.error_code.startswith("PENDING_WORK_")
        assert files(root) == before


def test_native_owner_rejects_a_second_budget_reservation(root, tmp_path):
    import evidence_wiki.research_session as module
    context = host(root, tmp_path)
    with context.activate():
        session = ResearchSession(root, policy(paid=True), run_id="session")
        work = job(session)
        second = detached(work)
        second["id"] = "second"
        before = files(root)
        with MutationStore(root, context.mutation_control_directory).workspace_locked(), pytest.raises(Exception) as error:
            module._owner(root, "run_session_record", run_id="session", agent_id=context.actor_ref,
                request_id="second", binding=session.binding,
                record={"kind": "host_job", "job": second, "authorized": False, "evidence_accepted": False})
        assert error.value.error_code == "RUN_JOB_RESERVATION_PENDING"
        assert files(root) == before


def test_concurrent_clients_cannot_reserve_the_same_run_budget(root, tmp_path, monkeypatch):
    import evidence_wiki.research_session as module
    entered, release = Event(), Event()
    original = module._job_original
    def paused(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    context = host(root, tmp_path)
    with context.activate():
        first = ResearchSession(root, policy(paid=True), run_id="session")
        second = ResearchSession(root, policy(paid=True), run_id="session")
        with MutationStore(root, context.mutation_control_directory).workspace_locked():
            first._run()
    monkeypatch.setattr(module, "_job_original", paused)
    def reserve(session, request):
        with context.activate():
            return session.host_job(request_id=request, operation="generate", payload={"question": "What does the evidence show?"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(reserve, first, "first")
        try:
            assert entered.wait(10)
            with pytest.raises(Exception) as error:
                pool.submit(reserve, second, "second").result(timeout=10)
            assert error.value.error_code == "MUTATION_LOCK_BUSY"
        finally:
            release.set()
        assert pending.result(timeout=10)["id"] == "first"
    reservations = [row for row in rows(root) if (row.get("data") or {}).get("record", {}).get("kind") == "host_job"]
    assert len(reservations) == 1
