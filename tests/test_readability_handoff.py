"""Actual lease diagnostics with controlled owners; no native timing proof."""

from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from evidence_wiki import readability_execution as execution
from evidence_wiki import readability_handoff as handoff
from evidence_wiki import research_session
from evidence_wiki.agent_execution import BoundedSubprocessExecutor, ExecutionAttempt
from evidence_wiki.calibration_registry import CalibratedEvaluators
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import canonical, digest
from evidence_wiki.pack_discovery import owner
from evidence_wiki.readability_context import create
from evidence_wiki.readability_contracts import stored_attempt
from evidence_wiki.research_execution import ResearchExecution
from tests.test_qualification_calibration import EXPIRY, NOW
from tests.test_readability_context import context
from tests.test_readability_profile import attempt
from tests.test_readability_profile import profile as profile
from tests.test_readability_qualification import issued as issued


@pytest.fixture
def custody(issued, profile, monkeypatch, tmp_path):
    from tests._usage_fixture import require_reference_executor
    require_reference_executor()
    policy, selected, authority, record, configuration = issued
    brief, _packet, _binding = context()
    packet, binding = create(brief, context_id="guarded", order_seed=b"b" * 32)
    supplied = {"purpose": "readability", "packet": packet}
    job = attempt(profile).job | {"id": "guarded", "context_id": "guarded", "packet_sha256": digest(supplied).removeprefix("sha256:"),
        "issued_utc": NOW, "deadline_utc": (datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(seconds=30)).isoformat()}
    current = ExecutionAttempt.create(attempt_id="guarded", nonce="fixed", run_id="controlled", binding_id="bound",
        profile=selected, config_ref="readability", job=job, input_document=supplied)
    clock = {"now": datetime.fromisoformat(NOW.replace("Z", "+00:00")), "fault": None}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]
    class Session:
        root = tmp_path
        binding = {"session": "controlled"}
        context = SimpleNamespace(activate=lambda: nullcontext(), require_grant=lambda *_: None)
        execution = SimpleNamespace(profile=selected)
        def _check(self, *_a, **_k):
            pass
    session = Session()
    executor = BoundedSubprocessExecutor(selected, commands={}, environment={}, cwd=tmp_path, meter=None, receipt_key=b"r" * 32)
    session.execution = SimpleNamespace(profile=selected, executor=executor)
    monkeypatch.setattr(research_session, "ResearchSession", Session)
    monkeypatch.setattr(handoff, "datetime", Clock)
    monkeypatch.setattr(handoff.native, "deadline", lambda *_: datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(seconds=60))
    row = {"attempt": stored_attempt(current, selected), "purpose": "readability", "readability_parent": {
        "basis_id": job["basis_id"], "substance_sha256": binding["substance_sha256"], "binding": binding, "order_seed": (b"b" * 32).hex(), "sources": []}}
    journal = object.__new__(ResearchExecution)
    journal._observer_key = b"o" * 32
    retained = journal.seal_record(session, "execution_attempt", "guarded.intent", row, records=[])
    monkeypatch.setattr(handoff.native, "retained", lambda *_: [retained])
    monkeypatch.setattr(handoff.native, "_document", lambda *_: {"state": {"current": "answering"}})
    native_owner = handoff.native.owner
    native_usage = native_owner("_evidence_usage")
    monkeypatch.setattr(handoff.native, "owner", lambda stem: SimpleNamespace(capture_workspace=lambda *_: SimpleNamespace(revision_id="fixed"))
        if stem == "_evidence_revision" else SimpleNamespace(TERMINAL_STATES=set()) if stem == "run_controller" else native_owner(stem))
    def rights(*_):
        if clock["fault"] == "rights":
            raise native_usage.EvidenceInvalid("host_trust_changed")
    observed = SimpleNamespace(_usage_view=SimpleNamespace(revalidate=rights), _config={}, _basis_id=job["basis_id"],
        bind_readability_eligibility=lambda: None, document=lambda: brief, source_packets=lambda: [], require=lambda *_: None)
    observed.input_brief = lambda: brief
    @contextmanager
    def held(*_):
        if clock["fault"] == "opening":
            raise UsageError("RESEARCH_RESOLUTION_REFUSED", "private opening", details={"reason": "resolution_current_inputs_changed"})
        yield observed
        if clock["fault"] == "closing":
            raise UsageError("RESEARCH_RESOLUTION_REFUSED", "private closing", details={"reason": "resolution_readability_currency_changed"})
        if clock["fault"] == "final_clock":
            clock["now"] = datetime.fromisoformat(job["deadline_utc"].replace("Z", "+00:00"))
    monkeypatch.setattr(handoff, "_held_readability_input", held)
    from evidence_wiki.readability_reference import observe_domain
    domain = observe_domain(brief)
    registry = CalibratedEvaluators([record], authority)
    session.execution.qualifications = registry
    session.execution.seal_record = lambda current_session, kind, identity, data: journal.seal_record(current_session, kind, identity, data,
        records=handoff.native.retained(current_session))
    executor.qualification_configuration = lambda request: {"request": request, "configuration": configuration}
    probe = {"profile_sha256": selected.sha256, "registry_sha256": registry.sha256,
        "semantic_basis_id": domain["semantic_basis_id"], "observed_utc": NOW,
        "expires_utc": (clock["now"] + timedelta(seconds=20)).isoformat(), "configuration": configuration, "reasons": []}
    monkeypatch.setattr(handoff, "qualification", lambda *_: registry.readability_observation(policy, selected,
        clock["now"].isoformat(), domain=domain, probe=probe))
    dispatch = execution._InputDispatch(session, "guarded", current, handoff.native.retained(session))
    token = execution._INPUT_DISPATCH.set(dispatch)
    try:
        dispatch.transfer(session, current)
    finally:
        execution._INPUT_DISPATCH.reset(token)
    lease = handoff.ReadabilityHandoffLease(session, current, wait_seconds=30, started_monotonic=handoff.time.monotonic(), admission=dispatch)
    lease.observe_configuration()
    lease.ready_guard()
    execution.enter_input_host_call(executor.invoke_readability_guarded, (current, lease))
    return lease, canonical(current.worker_document()), clock, probe


@pytest.mark.parametrize("fault,stage,reason", [
    ("opening", "native_opening", "resolution_current_inputs_changed"),
    ("probe_expiry", "purpose_qualification", "input_current_purpose_or_configuration_unknown"),
    ("signed_expiry", "purpose_qualification", "input_current_purpose_or_configuration_unknown"),
    ("transfer_expiry", "checkpoint_qualification", "handoff_qualification_expired"),
    ("rights", "checkpoint_rights", "native_usage_host_trust_changed"),
    ("closing", "native_closing", "resolution_readability_currency_changed"),
    ("final_clock", "final_preflight", "handoff_deadline_or_clock_invalid"),
])
def test_actual_lease_captures_only_owned_failure_through_complete_manager_exit(custody, fault, stage, reason):
    lease, payload, clock, probe = custody
    clock["fault"] = fault
    if fault == "probe_expiry":
        clock["now"] += timedelta(seconds=20)
    if fault == "signed_expiry":
        clock["now"] = datetime.fromisoformat(EXPIRY.replace("Z", "+00:00"))
        lease._wall_deadline = clock["now"] + timedelta(seconds=30)
        probe["observed_utc"] = (clock["now"] - timedelta(seconds=10)).isoformat()
        probe["expires_utc"] = (clock["now"] + timedelta(seconds=20)).isoformat()
    with pytest.raises((UsageError, ValueError)) as caught:
        with lease.custody(payload):
            if fault == "transfer_expiry":
                clock["now"] += timedelta(seconds=20)
                lease.checkpoint()
            lease.complete(payload)
    assert caught.value is not None
    assert lease.failure_diagnostic() == {"stage": stage, "owner_reason": reason}
    assert lease.delivered_sha256 is None
    diagnostic = lease.failure_diagnostic()
    diagnostic["owner_reason"] = "caller changed"
    assert lease.failure_diagnostic()["owner_reason"] == reason


@pytest.mark.parametrize("error", [ValueError("private /path key trace"),
    owner("_evidence_usage").EvidenceInvalid("private /path key trace"),
    UsageError("FOREIGN", "private", details={"reason": "handoff_qualification_expired"}),
    UsageError("READABILITY_REFUSED", "private", details={"reason": "private /path key trace"}),
    UsageError("READABILITY_REFUSED", "private", details={"reason": []})])
def test_private_prose_and_foreign_errors_are_rethrown_without_diagnostic(custody, error):
    lease, payload, _clock, _probe = custody
    with pytest.raises(type(error)) as caught:
        with lease.custody(payload):
            raise error
    assert caught.value is error
    assert lease.failure_diagnostic() == {}
    assert lease.delivered_sha256 is None


def test_external_failure_retains_actual_lease_diagnostic_without_changing_unknown_result(custody, monkeypatch, tmp_path):
    lease, _payload, clock, _probe = custody
    executor = lease.session.execution.executor
    admission = execution._InputDispatch(lease.session, "guarded", lease.attempt, handoff.native.retained(lease.session))
    token = execution._INPUT_DISPATCH.set(admission)
    created = []
    def initialize(value, *_a, **_k):
        value.__dict__.update(lease.__dict__)
        value._executor = None
        value._admission = _k["admission"]
        value._configuration = None
        created.append(value)
    monkeypatch.setattr(handoff.ReadabilityHandoffLease, "__init__", initialize)
    monkeypatch.setattr(handoff.native, "datetime", handoff.datetime)
    # The actual executor invokes custody; opening fails before worker dispatch.
    clock["fault"] = "opening"
    monkeypatch.setattr(executor, "_invoke", lambda *_: pytest.fail("Worker entered after opening refusal"))
    saved = []
    sealed = []
    journal = object.__new__(ResearchExecution)
    journal._observer_key = b"o" * 32
    @contextmanager
    def gate(*_):
        yield
    monkeypatch.setattr(handoff.native, "gate", gate)
    def append(session, kind, request_id, data):
        saved.append((kind, request_id, data))
        sealed.append(journal.seal_record(session, kind, request_id, data, records=sealed))
    monkeypatch.setattr(handoff.native, "append", append)
    try:
        result, reason = handoff.native._external(lease.session, "invoke", lease.attempt)
    finally:
        execution._INPUT_DISPATCH.reset(token)
    assert result is None and reason == "host_observation_failed"
    assert len(saved) == 1 and saved[0][0] == "execution_readability_handoff_failed"
    assert saved[0][2] == {"attempt_id": lease.attempt.attempt_id, "job_sha256": lease.attempt.job_sha256,
        "input_sha256": lease.attempt.input_sha256, "stage": "native_opening", "owner_reason": "resolution_current_inputs_changed"}
    assert lease.delivered_sha256 is None
    assert len(created) == 1 and created[0].failure_diagnostic()["stage"] == "native_opening"
    journal._verify(sealed[0])
    assert sealed[0]["data"] == saved[0][2] and sealed[0]["previous_record_ref"] is None
