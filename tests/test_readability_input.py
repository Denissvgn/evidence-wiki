"""Private input admission/budget mechanics keep public accounting unknown."""

import copy
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_accounting as accounting
from evidence_wiki import readability_execution as execution
from evidence_wiki import research_execution, resolution_evidence
from evidence_wiki import resolution_operations as resolution
from evidence_wiki.disagreement_configuration import DisagreementPlan
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import canonical, detached
from evidence_wiki.resolution_projection import _project
from evidence_wiki.resolution_rules import blockers
from evidence_wiki.run_policy import RunPolicy
from tests.test_disagreement import gates, reviews, selected_policy
from tests.test_execution_accounting import attempt as reservation
from tests.test_execution_accounting import opened, usage
from tests.test_readability_handoff import custody as custody
from tests.test_readability_profile import profile as profile
from tests.test_readability_qualification import issued as issued
from tests.test_resolution_projection import inputs
from tests.test_run_policy import observation, policy_document


@contextmanager
def fresh_lease(existing):
    from evidence_wiki.readability_handoff import ReadabilityHandoffLease
    admission = execution._InputDispatch(existing.session, existing.attempt.attempt_id, existing.attempt,
        execution.native.retained(existing.session))
    token = execution._INPUT_DISPATCH.set(admission)
    try:
        admission.transfer(existing.session, existing.attempt)
        value = ReadabilityHandoffLease(existing.session, existing.attempt, wait_seconds=30,
            started_monotonic=existing._deadline - 30, admission=admission)
        yield value
    finally:
        execution._INPUT_DISPATCH.reset(token)


@pytest.mark.parametrize("defect", ["copied", "request", "attempt", "session", "reused", "effect", "records", "thread"])
def test_identity_admission_rejects_copy_wrong_subject_replay_and_extra_effect(custody, monkeypatch, defect):
    lease, _payload, _clock, _probe = custody
    records = execution.native.retained(lease.session)
    value = execution._InputDispatch(lease.session, "guarded", lease.attempt, records)
    token = execution._INPUT_DISPATCH.set(value)
    try:
        with pytest.raises(UsageError):
            if defect == "copied":
                copy.copy(value)
            elif defect == "attempt":
                value.transfer(lease.session, replace(lease.attempt, nonce="foreign"))
            elif defect == "session":
                value.transfer(SimpleNamespace(), lease.attempt)
            elif defect == "thread":
                value.thread += 1
                value.transfer(lease.session, lease.attempt)
            elif defect == "records":
                monkeypatch.setattr(execution.native, "retained", lambda *_: records + [{"changed": True}])
                value.transfer(lease.session, lease.attempt)
            elif defect == "effect":
                execution.native.invalidate_current_facts()
                value.transfer(lease.session, lease.attempt)
            elif defect == "request":
                value.request_id = "foreign"
                # A foreign request cannot match the exact frozen job identity.
                value.transfer(lease.session, lease.attempt)
            else:
                value.transfer(lease.session, lease.attempt)
                value.transfer(lease.session, lease.attempt)
    finally:
        execution._INPUT_DISPATCH.reset(token)


@pytest.fixture
def input_budget(monkeypatch):
    document = policy_document()
    document["budgets"].update(model_calls=2, tool_calls=3, tokens=100, seconds=300, cost_limit="1")
    policy = RunPolicy(document)
    aid = "reserved-input"
    plan = accounting.reserve_attempt(opened(), reservation(aid))
    observed = observation(policy, selected_count=0)
    observed["spent"].update(model_calls=None, tool_calls=None, tokens=None, cost_limit=None)
    view = {"basis_id": observed["basis_id"], "usage": detached(observed["spent"]),
        "qualification": {"state": "qualified"}, "canonical_release": {"publication": {"questions": []}},
        "execution": {"pending": {"attempt_id": aid, "state": "dispatch_unknown", "reservation_occupied": True, "applied": False},
            "steps_used": 1, "steps_limit": 1}}
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    session = SimpleNamespace(policy=policy, execution=SimpleNamespace(ledger=lambda *_: detached(plan)))
    admission = SimpleNamespace(held=lambda current: None,
        attempt=SimpleNamespace(attempt_id=aid, job={"deadline_utc": deadline}))
    monkeypatch.setattr(execution.native, "policy_observation", lambda *_: detached(observed))
    monkeypatch.setattr(execution.native, "execution_budget_overrun", lambda *_: False)
    monkeypatch.setattr(execution.native, "deadline", lambda *_: datetime.fromisoformat(deadline.replace("Z", "+00:00")))
    monkeypatch.setattr(execution, "_remaining", lambda *_: {"overrun": []})
    return session, admission, view, plan, observed


def test_existing_reserved_cap_equality_allows_input_but_preserves_unknown_public_usage(input_budget):
    session, admission, view, plan, observed = input_budget
    before = canonical(plan), canonical(view), canonical(observed)
    assert execution.input_constraints(session, view, [], {}, admission) == []
    projection = accounting.projection(plan)
    assert projection["usage_unknown"] and projection["committed"] == usage(seconds="5", models=2, tools=3, tokens=100, cost="1")
    assert all(value is None for value in projection["usage"].values())
    assert session.policy.decide(observed)["reason"] == "usage_unknown"
    assert (canonical(plan), canonical(view), canonical(observed)) == before


@pytest.mark.parametrize("change", ["other_unknown", "cap_exceeded", "baseline_gap", "deadline", "pending_wrong", "host_unknown", "counter", "basis", "expected_basis", "qualification"])
def test_input_bound_rejects_other_unknown_missing_slots_overrun_and_owner_deadline(input_budget, change):
    session, admission, view, plan, observed = input_budget
    records = []
    if change == "other_unknown":
        plan["attempts"].append(reservation("other"))
    elif change == "cap_exceeded":
        plan["attempts"][0]["reservation"]["tokens"] += 1
    elif change == "baseline_gap":
        plan["slots"].append({"slot_id": "unobserved", "controller_ref": "host", "declared_at": "2026-10-03T00:00:01Z"})
    elif change == "deadline":
        admission.attempt.job["deadline_utc"] = "2026-01-01T00:00:00Z"
    elif change == "pending_wrong":
        view["execution"]["pending"]["attempt_id"] = "other"
    elif change == "host_unknown":
        records.append({"kind": "execution_host_unknown", "data": {"attempt_id": admission.attempt.attempt_id}})
    elif change in {"basis", "expected_basis"}:
        observed["basis_id" if change == "basis" else "expected_basis_id"] = "changed"
    elif change == "qualification":
        view["qualification"]["state"] = "unknown"
    else:
        view["usage"]["repair_cycles"] = session.policy.to_dict()["continuation_limits"]["repair_cycles"] + 1
    with pytest.raises(UsageError):
        execution.input_constraints(session, view, records, {}, admission)


def test_public_resolution_blockers_and_partial_original_accounting_stay_unchanged():
    value = inputs()
    frozen = copy.deepcopy(value)
    value["current"]["execution"]["pending"] = {"reservation_occupied": True}
    value["current"]["execution"]["accounting"] = {"usage_unknown": True}
    value["policy_observation"]["spent"]["model_calls"] = None
    public_blockers = blockers(value["current"], value["policy_document"], value["policy_observation"], value["derived_proofs"])
    assert public_blockers == ["current_policy_gate:usage_unknown"]
    public = _project(**{**value, "subset_blockers": public_blockers})
    assert not any(row["eligible_questions"] for row in public["original_outcomes"])
    selected = resolution._input_observation(value["current"], value["policy_observation"], value["derived_proofs"])
    assert selected["accepted_count"] == 0 and selected["selected_outcomes"] == []
    assert selected["spent"]["model_calls"] is None
    assert value["current"]["accounting"] == frozen["current"]["accounting"]


@pytest.mark.parametrize("state", ["already_begun", "wrong_running"])
def test_actual_executor_identity_refuses_prior_or_substituted_process(custody, state):
    lease, _payload, _clock, _probe = custody
    executor = lease.session.execution.executor
    fake = SimpleNamespace(attempt=lease.attempt, protected_delivery=True, handoff_deadline=lease._deadline)
    executor._attempts[lease.attempt.attempt_id] = fake
    if state == "wrong_running":
        lease._running = SimpleNamespace()
    with pytest.raises(UsageError):
        lease._require_started_identity()


def test_only_same_actual_started_process_remains_owned_through_input_custody(custody):
    lease, _payload, _clock, _probe = custody
    executor = lease.session.execution.executor
    running = SimpleNamespace(attempt=lease.attempt, protected_delivery=True, handoff_deadline=lease._deadline)
    with lease._admission.custody(lease):
        lease._thread = lease._admission.thread
        executor._attempts[lease.attempt.attempt_id] = running
        lease._started(executor, running)
        lease._admission.held(lease.session)
        running.handoff_deadline += 1
        with pytest.raises(UsageError):
            lease._admission.held(lease.session)


@pytest.mark.parametrize("drift", [None, "source", "proof", "rights"])
def test_private_owner_closes_fresh_inputs_and_never_exposes_public_resolution(custody, monkeypatch, drift):
    lease, _payload, _clock, _probe = custody
    session = lease.session
    session.run_id = "controlled"
    fixture = inputs()
    session.policy = RunPolicy(fixture["policy_document"])
    session.context.require_grant = lambda *_: None
    state = {"reads": 0}
    class Usage:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            if drift == "rights":
                raise UsageError("READABILITY_REFUSED", "controlled", details={"reason": "handoff_material_changed"})
        def revalidate(self, *_):
            pass
    monkeypatch.setattr(resolution, "ResearchSession", type(session))
    monkeypatch.setattr(resolution, "owner", lambda stem: SimpleNamespace(load_config=lambda *_: {}) if stem == "verify_quotes"
        else SimpleNamespace(configured=lambda *_: True, current_view=lambda *_: Usage()))
    @contextmanager
    def facts(*_):
        state["reads"] += 1
        changed = state["reads"] > 1 and drift == "source"
        yield SimpleNamespace(view=detached(fixture["current"]), records=resolution.native.retained(session),
            packets=[{"revision": "changed" if changed else "original"}],
            observed=SimpleNamespace(require=lambda *_: None))
    monkeypatch.setattr(resolution.native, "_current_facts", facts)
    monkeypatch.setattr(resolution, "_journal_proof", lambda *_: {})
    monkeypatch.setattr(resolution.native, "policy_observation", lambda *_: fixture["policy_observation"])
    monkeypatch.setattr(resolution.native, "execution_budget_overrun", lambda *_: False)
    monkeypatch.setattr(resolution, "_InputPass", lambda *_a, **_k: object())
    monkeypatch.setattr(resolution, "questions", lambda *_a, **_k: {} if state["reads"] > 1 and drift == "proof" else fixture["derived_proofs"])
    monkeypatch.setattr(execution, "input_constraints", lambda *_: [])
    # Independent admission identity remains checked; owner facts above are
    # controlled test observations and do not establish native qualification.
    with lease._admission.custody(lease):
        if drift is None:
            with resolution._held_readability_input(session, lease._admission) as actual:
                assert actual.input_brief()["verdict"] == "partial"
                with pytest.raises(UsageError):
                    actual.document()
        else:
            with pytest.raises(UsageError):
                with resolution._held_readability_input(session, lease._admission) as actual:
                    assert actual.input_brief()["original_outcomes"]
    assert lease.delivered_sha256 is None


@pytest.mark.parametrize("defect", [None, "unknown_vote", "controller", "mechanical", "authentication", "context", "adjudicated_material", "claim_limit", "counter_regressed"])
def test_private_input_requires_fresh_mandatory_material_proof_without_final_accounting_claim(monkeypatch, defect):
    from evidence_wiki import execution_continuations, execution_disagreement
    policy = selected_policy()
    proof = {"basis_id": "material", "reviews": reviews(basis="material", critic="pass"), "history": []}
    checks = gates(accounting="unknown")
    session = SimpleNamespace(policy=policy, execution=SimpleNamespace(profile=SimpleNamespace(disagreement=DisagreementPlan()),
        review_signer=SimpleNamespace(generator=SimpleNamespace(controller_ref="generator-controller"))))
    current = {"usage": {"repair_cycles": 0, "adjudication_cycles": 0, "acquisition_attempts": 0}}
    facts = SimpleNamespace(admission=SimpleNamespace(held=lambda *_: None), prepared=lambda *_: {}, packets=lambda: [])
    if defect == "unknown_vote":
        proof["reviews"][0]["verdicts"]["support"] = "unknown"
    elif defect == "controller":
        proof["reviews"][0]["controller_ref"] = proof["reviews"][1]["controller_ref"]
    elif defect in {"mechanical", "authentication"}:
        checks[defect] = "unknown"
    elif defect == "context":
        checks["context_complete"] = False
    elif defect == "adjudicated_material":
        proof["history"] = [{"action": "adjudicate", "basis_id": "material", "result_basis_id": "other"}]
        current["usage"]["adjudication_cycles"] = 1
    elif defect in {"claim_limit", "counter_regressed"}:
        count = 3 if defect == "claim_limit" else 1
        proof["history"] = [{"action": "repair", "basis_id": "past" + str(index), "result_basis_id": "later" + str(index)} for index in range(count)]
        current["usage"]["repair_cycles"] = 3 if defect == "claim_limit" else 0
    monkeypatch.setattr(execution_continuations, "claim_sources", lambda *_: [])
    monkeypatch.setattr(execution_continuations, "current_gates", lambda *_: checks)
    monkeypatch.setattr(execution_continuations, "authenticated_gates", lambda *_a, **_k: checks)
    monkeypatch.setattr(execution_disagreement, "project_claim", lambda *_a, **_k: proof)
    assert resolution_evidence._input_claim_ready(session, current, "claim", facts) is (defect is None)
    assert checks["accounting"] == "unknown"


@pytest.mark.parametrize("fault", [None, "echo_job", "echo_input", "extra_wait", "idempotent_append", "original_deadline"])
def test_actual_dispatch_configuration_binds_exact_echo_and_two_fixed_waits(custody, monkeypatch, fault):
    lease, _payload, _clock, _probe = custody
    executor = lease.session.execution.executor
    old_callback = executor.qualification_configuration
    called = []
    def callback(request):
        called.append(detached(request))
        response = old_callback(request)
        if fault in {"echo_job", "echo_input"}:
            response["request"]["job_sha256" if fault == "echo_job" else "input_sha256"] = "0" * 64
        return response
    executor.qualification_configuration = callback
    records = execution.native.retained(lease.session)
    records[0].update(request_id="same", event_ref="existing")
    monkeypatch.setattr(execution.native, "retained", lambda *_: detached(records))
    with fresh_lease(lease) as actual:
        original_deadline = actual._deadline
        if fault == "original_deadline":
            actual._deadline = 0
        if fault in {"echo_job", "echo_input", "original_deadline"}:
            with pytest.raises(UsageError):
                actual.observe_configuration()
            assert actual.delivered_sha256 is None
            return
        actual.observe_configuration()
        assert actual._deadline == original_deadline
        assert called[0]["schema_version"] == "research-readability-dispatch-request/v1"
        assert {key: called[0][key] for key in ("attempt_id", "job_sha256", "input_sha256", "binding_id")} == {
            "attempt_id": actual.attempt.attempt_id, "job_sha256": actual.attempt.job_sha256,
            "input_sha256": actual.attempt.input_sha256, "binding_id": actual.attempt.binding_id}
        actual.ready_guard()
        assert execution._INPUT_DISPATCH.get() is actual._admission
        if fault == "extra_wait":
            assert execution.native.bounded_host_call(lambda: None, (), 1) == (None, None)
        elif fault == "idempotent_append":
            assert execution.native.append(lease.session, "execution_attempt", "same", records[0]["data"]) == {"event_id": "existing"}
        if fault is not None:
            with pytest.raises(UsageError):
                execution.enter_input_host_call(executor.invoke_readability_guarded, (actual.attempt, actual))
        else:
            execution.enter_input_host_call(executor.invoke_readability_guarded, (actual.attempt, actual))
            assert execution._INPUT_DISPATCH.get() is None
            with actual._admission.custody(actual):
                actual._admission.held(actual.session)
        assert actual.delivered_sha256 is None


@pytest.mark.parametrize("own_present,expired,certified", [(True, False, True), (False, False, True), (True, True, True), (True, False, False)])
def test_report_qualification_uses_exact_application_probe_never_a_later_unrelated_one(issued, monkeypatch, own_present, expired, certified):
    from evidence_wiki.calibration_registry import CalibratedEvaluators
    from evidence_wiki.readability_reference import observe_domain
    from tests.test_qualification_calibration import NOW
    from tests.test_readability_context import context
    policy, profile, authority, record, config = issued
    registry = CalibratedEvaluators([record], authority)
    brief, _packet, _binding = context()
    domain = observe_domain(brief)
    now = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now + timedelta(seconds=30)
    session = SimpleNamespace(policy=policy, execution=SimpleNamespace(profile=profile, qualifications=registry))
    observed = SimpleNamespace(require=lambda *_: None, document=lambda: brief)
    probe = {"profile_sha256": profile.sha256, "registry_sha256": registry.sha256, "semantic_basis_id": domain["semantic_basis_id"],
        "observed_utc": NOW, "expires_utc": (now + timedelta(seconds=20)).isoformat(), "configuration": config, "reasons": []}
    own = detached(probe)
    if expired:
        own["expires_utc"] = NOW
    identity = execution.native.child_id(execution.native.child_id("owned-application", "readability_application"), "readability_probe")
    rows = ([{"kind": "execution_readability_probe", "request_id": identity, "data": {"probe": own}}] if own_present else [])
    rows.append({"kind": "execution_readability_probe", "request_id": "unrelated-newer", "data": {"probe": probe}})
    application = {"operation": "readability", "attempt_id": "owned-application"}
    if certified:
        application["checked_utc"] = NOW
    rows.append({"kind": "execution_application", "data": application})
    attempt = SimpleNamespace(job={"issued_utc": (now - timedelta(seconds=10)).isoformat(),
        "deadline_utc": (now + timedelta(seconds=60)).isoformat()})
    monkeypatch.setattr(execution, "owned", lambda *_: ({}, attempt, {"completed_utc": (now - timedelta(seconds=1)).isoformat()}))
    monkeypatch.setattr(execution.native, "retained", lambda *_: detached(rows))
    monkeypatch.setattr(execution, "datetime", Clock)
    monkeypatch.setattr(execution, "project", lambda value: value)
    monkeypatch.setattr(execution, "readability_credential", lambda *_: None)
    result = execution.qualification(session, observed, application_attempt_id="owned-application")
    assert result["state"] == ("qualified" if own_present and not expired and certified else "unknown")


@pytest.mark.parametrize("failure", ["capacity", "zero_wait", "thread_start", "late_guard"])
def test_real_bounded_guard_scheduling_failure_poisons_one_use_admission(custody, monkeypatch, failure):
    lease, payload, _clock, _probe = custody
    executor = lease.session.execution.executor
    with fresh_lease(lease) as actual:
        actual.observe_configuration()
        actual.ready_guard()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        if failure == "capacity":
            monkeypatch.setattr(research_execution, "_HOST_CALL_CAPACITY", threading.BoundedSemaphore(0))
        elif failure == "thread_start":
            monkeypatch.setattr(research_execution.threading.Thread, "start", lambda *_: (_ for _ in ()).throw(RuntimeError("controlled")))
        elif failure == "late_guard":
            original_thread = research_execution.threading.Thread
            class CompletionThread(original_thread):
                def run(self):
                    try:
                        super().run()
                    finally:
                        finished.set()
            monkeypatch.setattr(research_execution.threading, "Thread", CompletionThread)
            def invoke(_attempt, owned_lease):
                entered.set()
                release.wait(3)
                owned_lease.complete(payload)
            monkeypatch.setattr(executor, "_invoke", invoke)
        result, reason = research_execution.bounded_host_call(executor.invoke_readability_guarded,
            (actual.attempt, actual), 0 if failure == "zero_wait" else 1.5 if failure == "late_guard" else 1)
        assert result is None and reason is not None
        assert actual._admission.invalidated and actual._admission.state == "failed"
        if failure == "late_guard":
            assert entered.is_set(), actual.failure_diagnostic()
            release.set()
            assert finished.wait(1)
        assert actual.delivered_sha256 is None
        with pytest.raises(UsageError):
            executor.invoke_readability_guarded(actual.attempt, actual)


@pytest.mark.parametrize("wait", ["configuration", "guard"])
def test_repeated_fixed_wait_cannot_reuse_scheduled_or_finished_admission(custody, wait):
    lease, _payload, _clock, _probe = custody
    with fresh_lease(lease) as actual:
        actual.observe_configuration()
        if wait == "configuration":
            with pytest.raises(UsageError):
                actual.observe_configuration()
        else:
            actual.ready_guard()
            execution.enter_input_host_call(lease.session.execution.executor.invoke_readability_guarded, (actual.attempt, actual))
            with pytest.raises(UsageError):
                actual._admission.ready_guard(actual)


def test_late_configuration_callback_cannot_restore_failed_admission(custody, monkeypatch):
    lease, _payload, _clock, _probe = custody
    executor = lease.session.execution.executor
    original = executor.qualification_configuration
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def callback(request):
        entered.set()
        release.wait(1)
        try:
            return original(request)
        finally:
            finished.set()
    executor.qualification_configuration = callback
    with fresh_lease(lease) as actual:
        actual._bind_executor(executor)
        actual._admission.begin_configuration(actual)
        result, reason = research_execution.bounded_host_call(actual._configuration_callback, (), 0.05)
        assert result is None and reason == "host_wait_expired" and entered.is_set()
        release.set()
        assert finished.wait(1)
        assert actual._admission.invalidated
        with pytest.raises(UsageError):
            actual._admission.configuration_completed(actual)
        assert actual.delivered_sha256 is None


@pytest.mark.parametrize("change", ["request_job", "receipt_nonce", "extra_field", "interval"])
def test_closed_dispatch_receipt_rejects_substitution_before_transfer(custody, change):
    from evidence_wiki.readability_contracts import dispatch_observation_document
    lease, _payload, _clock, _probe = custody
    value = lease._configuration.document()
    if change == "request_job":
        value["request"]["job_sha256"] = "0" * 64
    elif change == "receipt_nonce":
        value["nonce"] = "0" * 32
    elif change == "extra_field":
        value["permission"] = True
    else:
        value["probe"]["expires_utc"] = value["probe"]["observed_utc"]
    with pytest.raises(UsageError):
        dispatch_observation_document(value, lease.attempt, lease.session.execution.profile, lease.session.execution.qualifications.sha256)
    assert lease.delivered_sha256 is None


def test_complete_native_envelope_bound_is_checked_before_guard_schedule(custody, monkeypatch):
    lease, _payload, _clock, _probe = custody
    original = lease.session.execution.seal_record
    def huge(*args):
        value = original(*args)
        value["data"]["bounded_test"] = "😀" * 30000
        return value
    monkeypatch.setattr(lease.session.execution, "seal_record", huge)
    with fresh_lease(lease) as actual:
        with pytest.raises(UsageError):
            actual.observe_configuration()
        assert actual._admission.state == "configuration_observed"
        assert actual.delivered_sha256 is None


@pytest.mark.parametrize("change,reason", [("domain", "input_domain_changed"), ("expired", "dispatch_probe_expired"), ("future", "dispatch_probe_not_yet_valid")])
def test_private_dispatch_probe_reports_specific_domain_and_clock_refusals(custody, change, reason):
    from tests.test_readability_context import context
    lease, _payload, clock, _probe = custody
    brief, _packet, _binding = context()
    observed = SimpleNamespace(require=lambda *_: None, input_brief=lambda: brief)
    if change == "domain":
        brief["limitations"].append("Changed field.")
    elif change == "expired":
        clock["now"] = datetime.fromisoformat(lease._configuration.document()["probe"]["expires_utc"].replace("Z", "+00:00"))
    else:
        clock["now"] -= timedelta(seconds=1)
    with lease._admission.custody(lease):
        with pytest.raises(UsageError) as error:
            lease.configuration_probe(observed)
        assert error.value.details["reason"] == reason
    assert lease.delivered_sha256 is None


@pytest.fixture
def lifecycle_scope(monkeypatch):
    """Isolate lifecycle routing from the separately tested native input owner."""
    from evidence_wiki import _review_dependency_inputs
    @contextmanager
    def scoped(_session):
        yield
    monkeypatch.setattr(_review_dependency_inputs, "bind_session", scoped)


@pytest.mark.parametrize("existing", [False, True])
def test_first_public_next_then_advance_initializes_normally_without_critic_preselection(monkeypatch, tmp_path, existing, lifecycle_scope):
    from evidence_wiki import review_packet_execution
    from evidence_wiki.research_session import ResearchSession
    session = object.__new__(ResearchSession)
    session._root = tmp_path
    session._execution = SimpleNamespace(profile=SimpleNamespace(readability=object(), calibration=None, review_services=None, total_steps=20))
    session._context = SimpleNamespace(require_grant=lambda *_: None)
    session._run_id = "entry"
    session._check = lambda *_a, **_k: None
    records = [{"kind": "execution_begin", "data": {}}] if existing else []
    state = {"begun": existing, "document_reads": 0}
    @contextmanager
    def gate(*_):
        yield
    def document(*_):
        state["document_reads"] += 1
        if not state["begun"]:
            raise AssertionError("Read canonical document before normal initialization")
        return {"state": {"current": "answering"}}
    def begin(*_):
        state["begun"] = True
        if not records:
            records.append({"kind": "execution_begin", "data": {}})
        return document()
    action = {"operation": "prepare", "original_id": "original", "reason": "original_question_preparation"}
    monkeypatch.setattr(execution.native, "gate", gate)
    monkeypatch.setattr(execution.native, "retained", lambda *_: detached(records))
    monkeypatch.setattr(execution.native, "current", lambda *_: {"basis_id": "basis", "execution": {"steps_used": 0, "total_steps": 20, "steps_limit": 20}})
    monkeypatch.setattr(execution.native, "checked", lambda _version, value: value)
    monkeypatch.setattr(execution.native, "_document", document)
    monkeypatch.setattr(execution.native, "_begin", begin)
    monkeypatch.setattr(execution.native, "owner", lambda *_: SimpleNamespace(TERMINAL_STATES={"complete"}))
    monkeypatch.setattr(execution.native, "_action", lambda *_a, **_k: detached(action))
    monkeypatch.setattr(execution, "prefix_attempt", lambda *_: None)
    monkeypatch.setattr(execution, "prepare_step", lambda *_: pytest.fail("Initial ordinary work selected a critic"))
    monkeypatch.setattr(review_packet_execution, "automatic", lambda *_a, **_k: None)
    monkeypatch.setattr(execution.native, "append", lambda *_: None)
    monkeypatch.setattr(execution.native, "_complete_advance", lambda *_: {"action": "prepare", "terminal": False})
    proposed = session.next()
    assert proposed["actions"][0]["operation"] == "prepare"
    result = session.advance(request_id="first", expected_basis_id="basis")
    assert result == {"action": "prepare", "terminal": False} and state["begun"]
    assert not any(row["kind"] == "execution_attempt" for row in records)


def test_stopped_initialized_run_keeps_normal_terminal_refusal(monkeypatch, tmp_path, lifecycle_scope):
    from evidence_wiki import review_packet_execution
    from evidence_wiki.research_session import ResearchSession
    session = object.__new__(ResearchSession)
    session._root = tmp_path
    session._execution = SimpleNamespace(profile=SimpleNamespace(readability=object(), calibration=None, review_services=None))
    session._context = SimpleNamespace(require_grant=lambda *_: None)
    session._check = lambda *_a, **_k: None
    @contextmanager
    def gate(*_):
        yield
    monkeypatch.setattr(execution.native, "gate", gate)
    monkeypatch.setattr(execution.native, "retained", lambda *_: [{"kind": "execution_begin", "data": {}}])
    monkeypatch.setattr(execution.native, "_document", lambda *_: {"state": {"current": "complete"}})
    monkeypatch.setattr(execution.native, "_begin", lambda *_: {"state": {"current": "complete"}})
    monkeypatch.setattr(execution.native, "owner", lambda *_: SimpleNamespace(TERMINAL_STATES={"complete"}))
    monkeypatch.setattr(execution.native, "_action", lambda *_a, **_k: pytest.fail("Stopped run selected work"))
    monkeypatch.setattr(execution, "prefix_attempt", lambda *_: None)
    monkeypatch.setattr(review_packet_execution, "automatic", lambda *_a, **_k: None)
    with pytest.raises(UsageError) as error:
        session.advance(request_id="first", expected_basis_id="basis")
    assert error.value.error_code == "RESEARCH_RUN_TERMINAL"
