"""Host application windows preserve the original bounded worker lifetime."""

from contextvars import copy_context
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from threading import Thread
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import review_application_lease as lease
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionError, _digest
from evidence_wiki.errors import ConfigError, UsageError
from evidence_wiki.runtime_configuration import _profile
from evidence_wiki.workflow_contracts import (
    JOB_V3,
    JOB_V5,
    JOB_V6,
    WorkflowContractError,
    validate_document,
    validate_job,
)
from tests.test_application_validation_epoch import application as application
from tests.test_disagreement_configuration import simple_profile
from tests.test_workflow_contracts import job


def selected(origin, schema_version=lease.LEASE_V1):
    base = simple_profile()
    roles = tuple(replace(role, controller_ref="reviewer") if role.config_ref == "profile-a" else role for role in base.roles)
    return replace(base, roles=roles, review_application_lease=lease.ReviewApplicationLease(1800 if schema_version == lease.LEASE_V1 else 3600,
        (origin + timedelta(hours=2)).isoformat(), schema_version=schema_version))


def pin(application, monkeypatch, *, extended=True, schema_version=lease.LEASE_V1):
    session, state, row, previous, observation, records = application
    origin = state.now
    profile = selected(origin, schema_version)
    if not extended:
        profile = replace(profile, review_application_lease=None)
    scope = profile.role_scopes.scope("profile-a")
    document = job("evaluate") | previous.job | {
        "schema_version": (JOB_V5 if schema_version == lease.LEASE_V1 else JOB_V6) if extended else JOB_V3,
        "issued_utc": origin.isoformat(), "deadline_utc": (origin + timedelta(seconds=600)).isoformat(),
        "timeout_seconds": 600, "expected_result_schema": "research-execution-observation/v1",
        "payload": {"claim_id": row["claim_id"], "packet_id": previous.job["payload"]["packet_id"]},
        "context_id": previous.input_document["packet"]["context_id"], "role_scope_sha256": scope.sha256,
        "packet_sha256": _digest(previous.input_document), "required_capabilities": ["model"]}
    if extended:
        document.update(application_started_utc=(origin - timedelta(seconds=60)).isoformat(),
            application_deadline_utc=(origin + timedelta(seconds=profile.review_application_lease.seconds - 60)).isoformat())
    attempt = ExecutionAttempt.create(attempt_id="review", nonce="nonce", run_id="run", binding_id="binding",
        profile=profile, config_ref="profile-a", job=document, input_document=previous.input_document)
    session.execution.profile = profile
    session.execution.attempt = lambda _: attempt
    row["attempt"] = attempt.document()
    observation["completed_utc"] = (origin + timedelta(seconds=300)).isoformat()
    monkeypatch.setattr(native, "deadline", lambda _: origin + timedelta(hours=2))
    return attempt


@pytest.mark.parametrize("schema_version", [lease.LEASE_V1, lease.LEASE_V2])
def test_timely_worker_applies_after_worker_deadline_with_current_checks(application, monkeypatch, schema_version):
    attempt = pin(application, monkeypatch, schema_version=schema_version)
    session, state, _, _, _, records = application
    state.now += timedelta(seconds=790)
    native._apply_owned(session, attempt.attempt_id)
    assert state.effects == ["strict.register_review"]
    assert state.strict_reads == 1 and state.prepares == 2 and state.verified
    assert records[-1]["kind"] == "execution_applied"
    assert attempt.job["timeout_seconds"] == 600
    assert "application_deadline_utc" not in attempt.worker_document()["job"]


@pytest.mark.parametrize("change", ["expired", "late_worker", "source", "grant", "qualification", "profile", "retry", "run_bound"])
@pytest.mark.parametrize("schema_version", [lease.LEASE_V1, lease.LEASE_V2])
def test_extended_application_never_bypasses_original_or_current_authority(application, monkeypatch, change, schema_version):
    attempt = pin(application, monkeypatch, schema_version=schema_version)
    session, state, row, _, observed, records = application
    state.now += timedelta(seconds=790)
    if change == "expired":
        state.now += timedelta(seconds=3600)
    elif change == "late_worker":
        observed["completed_utc"] = (datetime.fromisoformat(attempt.job["deadline_utc"]) + timedelta(seconds=1)).isoformat()
    elif change == "source":
        state.source_changed = True
    elif change == "grant":
        state.denied = True
    elif change == "qualification":
        state.qualified = False
    elif change == "profile":
        session.execution.profile = replace(session.execution.profile, profile_ref="different")
    elif change == "retry":
        row["retry_purpose"] = "evaluate"
    else:
        monkeypatch.setattr(native, "deadline", lambda _: state.now + timedelta(seconds=1))
    expected = (UsageError if change in {"source", "grant"} else
        WorkflowContractError if change in {"expired", "qualification"} else ExecutionError)
    with pytest.raises(expected):
        native._apply_owned(session, attempt.attempt_id)
    assert not state.effects
    assert not any(record["kind"] == "execution_application" for record in records)


def test_default_attempt_still_expires_at_original_worker_deadline(application, monkeypatch):
    attempt = pin(application, monkeypatch, extended=False)
    session, state, *_ = application
    state.now += timedelta(seconds=790)
    with pytest.raises(WorkflowContractError):
        native._apply_owned(session, attempt.attempt_id)
    assert not state.effects


def test_profile_roundtrip_is_immutable_and_legacy_bytes_unchanged():
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    legacy = simple_profile()
    opted = selected(origin)
    assert opted.document()["schema_version"] == "research-execution-profile/v16"
    assert replace(opted, roles=legacy.roles, review_application_lease=None).document() == legacy.document()
    assert _profile(opted.document()).document() == opted.document()
    detached = opted.document()
    detached["review_application_lease"]["seconds"] = 900
    assert opted.document()["review_application_lease"]["seconds"] == 1800
    with pytest.raises(FrozenInstanceError):
        opted.review_application_lease.seconds = 900
    for seconds in (0, 1801, True):
        with pytest.raises(ExecutionError):
            lease.ReviewApplicationLease(seconds, origin.isoformat())


def test_original_window_is_bound_before_assembly_and_cannot_be_renewed(monkeypatch):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state = SimpleNamespace(now=origin)
    class Clock:
        @staticmethod
        def now(_):
            return state.now
    monkeypatch.setattr(lease, "datetime", Clock)
    monkeypatch.setattr(native, "deadline", lambda _: origin + timedelta(seconds=1700))
    session = SimpleNamespace(execution=SimpleNamespace(profile=selected(origin)))
    with pytest.raises(ExecutionError, match="window_required"):
        lease.issuance(session)
    with lease.review_application_window(session):
        state.now += timedelta(seconds=400)
        original = lease.issuance(session)
        assert original == {"application_started_utc": origin.isoformat(),
            "application_deadline_utc": (origin + timedelta(seconds=1700)).isoformat()}
        with pytest.raises(ExecutionError, match="window_invalid"):
            with lease.review_application_window(session):
                pass
        foreign = SimpleNamespace(execution=session.execution)
        with pytest.raises(ExecutionError, match="window_required"):
            lease.issuance(foreign)
        errors = []
        context = copy_context()
        def copied_thread():
            try:
                context.run(lease.issuance, session)
            except ExecutionError as error:
                errors.append(str(error))
        thread = Thread(target=copied_thread)
        thread.start()
        thread.join()
        assert errors == ["review_application_window_required"]
        state.now += timedelta(seconds=1300)
        with pytest.raises(ExecutionError, match="window_expired"):
            lease.issuance(session)
    with pytest.raises(ExecutionError, match="window_required"):
        lease.issuance(session)
    with pytest.raises(ExecutionError, match="window_required"):
        context.run(lease.issuance, session)


@pytest.mark.parametrize("change", ["worker", "application", "start", "profile", "operation"])
@pytest.mark.parametrize("schema_version", [lease.LEASE_V1, lease.LEASE_V2])
def test_new_job_schema_and_profile_refuse_mismatched_or_unbounded_leases(application, monkeypatch, change, schema_version):
    attempt = pin(application, monkeypatch, schema_version=schema_version)
    document = attempt.job
    profile = application[0].execution.profile
    if change == "worker":
        document["timeout_seconds"] = 601
    elif change == "application":
        document["application_deadline_utc"] = profile.review_application_lease.not_after_utc
    elif change == "start":
        document["application_started_utc"] = document["deadline_utc"]
    elif change == "profile":
        profile = replace(profile, review_application_lease=None)
    else:
        document["operation"] = "generate"
    with pytest.raises((ExecutionError, WorkflowContractError)):
        ExecutionAttempt.create(attempt_id="review", nonce="nonce", run_id="run", binding_id="binding",
            profile=profile, config_ref="profile-a", job=document, input_document=attempt.input_document)
    assert attempt.job["schema_version"] == (JOB_V5 if schema_version == lease.LEASE_V1 else JOB_V6)
    assert validate_document(attempt.job["schema_version"], attempt.job) == attempt.job
    restored = replace(attempt)
    assert restored.job_sha256 == attempt.job_sha256 and restored.job == attempt.job
    # Worker dispatch still expires at its original 600 second bound.
    with pytest.raises(WorkflowContractError):
        validate_job(attempt.job, now_utc=attempt.job["deadline_utc"])


def test_copied_live_window_loses_authority_on_scope_exit_before_expiry(monkeypatch):
    origin = datetime.now(timezone.utc)
    monkeypatch.setattr(native, "deadline", lambda _: origin + timedelta(hours=1))
    session = SimpleNamespace(execution=SimpleNamespace(profile=selected(origin)))
    with lease.review_application_window(session):
        copied = copy_context()
        assert copied.run(lease.issuance, session)["application_started_utc"]
    with pytest.raises(ExecutionError, match="window_required"):
        copied.run(lease.issuance, session)


@pytest.mark.parametrize("extended", [True, False])
@pytest.mark.parametrize("schema_version", [lease.LEASE_V1, lease.LEASE_V2])
def test_succeeded_pending_uses_only_original_selected_application_lease(application, monkeypatch, extended, schema_version):
    attempt = pin(application, monkeypatch, extended=extended, schema_version=schema_version)
    session, state, row, _, observed, records = application
    state.now += timedelta(seconds=790)
    session.policy.to_dict = lambda: {"human_review": False, "required_evaluators": [], "budgets": {"claims": 10, "sources": 10}}
    view = {"qualification": {"reason": "ready"}, "policy_compatible": True,
        "authority": {"terminal_grants_available": True}, "usage": {"claims": 1, "sources": 1},
        "execution": {"steps_used": 0, "pending": {"applied": False, "attempt_id": attempt.attempt_id,
            "state": "succeeded", "reservation_occupied": False, "deadline_utc": attempt.job["deadline_utc"]}}}
    monkeypatch.setattr(native, "execution_budget_overrun", lambda *_: False)
    monkeypatch.setattr(native, "_terminal_action", lambda *_: {"operation": "terminal"})
    action = native._owner_action(session, view, records)
    assert action["operation"] == ("admit" if extended else "terminal")


@pytest.mark.parametrize("route,case_id,cap", [("scoped_mcp", "independent-claim-text", 1800),
    ("direct", "other-case", 1800), ("direct", "independent-claim-text", 600)])
def test_observational_service_runtime_refuses_other_routes_or_worker_cap(route, case_id, cap):
    from tools._review_dependency_workload import drive_selective_services
    runtime = SimpleNamespace(execution=SimpleNamespace(profile=selected(datetime.now(timezone.utc))))
    with pytest.raises(ValueError, match="workload_observational_pilot_only"):
        drive_selective_services(None, runtime, route, "run", None, case_id=case_id, obligations=[], job_cap_seconds=cap)


@pytest.mark.parametrize("seconds", [1, 1800, 1801, 3600])
def test_explicit_extended_selection_roundtrips_without_changing_default(seconds):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expiry = (origin + timedelta(hours=2)).isoformat()
    selected_lease = lease.ReviewApplicationLease(seconds, expiry, schema_version=lease.LEASE_V2)
    assert selected_lease.document() == {"schema_version": lease.LEASE_V2,
        "seconds": seconds, "not_after_utc": expiry}
    profile = replace(selected(origin), review_application_lease=selected_lease)
    assert profile.document()["schema_version"] == "research-execution-profile/v17"
    assert _profile(profile.document()).document() == profile.document()
    assert lease.ReviewApplicationLease(1800, expiry).document() == {
        "schema_version": lease.LEASE_V1, "seconds": 1800, "not_after_utc": expiry}
    for wrong in ("research-execution-profile/v16", "research-execution-profile/v1"):
        with pytest.raises(ConfigError):
            _profile(profile.document() | {"schema_version": wrong})


@pytest.mark.parametrize("seconds", [0, -1, 3601, True, False, 1.0, 3600.0, "3600", None])
def test_extended_selection_refuses_noninteger_or_unbounded_duration(seconds):
    with pytest.raises(ExecutionError, match="bound_invalid"):
        lease.ReviewApplicationLease(seconds, "2026-01-01T00:00:00+00:00", schema_version=lease.LEASE_V2)


@pytest.mark.parametrize("version", ["research-review-application-lease/v3", None, True, []])
def test_application_selection_refuses_unknown_versions(version):
    with pytest.raises(ExecutionError, match="version_invalid"):
        lease.ReviewApplicationLease(1800, "2026-01-01T00:00:00+00:00", schema_version=version)


@pytest.mark.parametrize("schema_version", [lease.LEASE_V1, lease.LEASE_V2])
def test_application_job_version_must_match_explicit_profile(application, monkeypatch, schema_version):
    attempt = pin(application, monkeypatch, schema_version=schema_version)
    profile = application[0].execution.profile
    other = lease.LEASE_V2 if schema_version == lease.LEASE_V1 else lease.LEASE_V1
    mismatched = replace(profile, review_application_lease=lease.ReviewApplicationLease(
        1800, profile.review_application_lease.not_after_utc, schema_version=other))
    with pytest.raises(ExecutionError, match="binding_invalid"):
        mismatched.check_job_scope("profile-a", attempt.job, attempt.input_document)


def test_extended_worker_receipt_applies_within_frozen_hour_after_half_hour(application, monkeypatch):
    attempt = pin(application, monkeypatch, schema_version=lease.LEASE_V2)
    session, state, *_ = application
    state.now += timedelta(seconds=2400)
    native._apply_owned(session, attempt.attempt_id)
    assert state.effects == ["strict.register_review"]
    assert attempt.job["timeout_seconds"] == 600
    assert "application_deadline_utc" not in attempt.worker_document()["job"]


def test_extended_window_uses_original_authority_expiry_and_task_owner(monkeypatch):
    import asyncio

    origin = datetime.now(timezone.utc)
    monkeypatch.setattr(native, "deadline", lambda _: origin + timedelta(hours=2))
    profile = selected(origin, lease.LEASE_V2)
    profile = replace(profile, review_application_lease=lease.ReviewApplicationLease(
        3600, (origin + timedelta(seconds=2500)).isoformat(), schema_version=lease.LEASE_V2))
    session = SimpleNamespace(execution=SimpleNamespace(profile=profile))

    async def exercise():
        with lease.review_application_window(session):
            issued = lease.issuance(session)
            assert issued["application_deadline_utc"] == profile.review_application_lease.not_after_utc
            async def foreign_task():
                with pytest.raises(ExecutionError, match="window_required"):
                    lease.issuance(session)
            await asyncio.create_task(foreign_task())
            original = session.execution.profile
            session.execution.profile = replace(original, profile_ref="changed")
            with pytest.raises(ExecutionError, match="window_required"):
                lease.issuance(session)
            session.execution.profile = original
            assert lease.issuance(session) == issued
    asyncio.run(exercise())


@pytest.mark.parametrize("schema_version,maximum", [(lease.LEASE_V1, 1800), (lease.LEASE_V2, 3600)])
def test_wire_application_bounds_preserve_worker_ceiling(application, monkeypatch, schema_version, maximum):
    attempt = pin(application, monkeypatch, schema_version=schema_version)
    document = attempt.job
    start = datetime.fromisoformat(document["application_started_utc"])
    document["application_deadline_utc"] = (start + timedelta(seconds=maximum)).isoformat()
    assert validate_document(document["schema_version"], document) == document
    document["application_deadline_utc"] = (start + timedelta(seconds=maximum + 1)).isoformat()
    with pytest.raises(WorkflowContractError):
        validate_document(document["schema_version"], document)
    document = attempt.job
    document["timeout_seconds"] = 601
    with pytest.raises(WorkflowContractError):
        validate_document(document["schema_version"], document)
