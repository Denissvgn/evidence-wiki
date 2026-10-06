"""Controlled owner seams check lifetime and durable prefixes, not native qualification."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import readability_execution as execution
from evidence_wiki import readability_probes as probes
from evidence_wiki import research_execution
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import canonical, detached


@pytest.mark.parametrize("partial", [True, False, None])
def test_resolution_preserves_unsupported_siblings_and_requires_explicit_partial_policy(partial):
    original = {"questions": [{"slug": slug, "status": status} for slug, status in
        [("ready", "claimed"), ("empty", "claimed"), ("rejected", "claimed"), ("unknown", "claimed"), ("done", "answered")]]}
    strict = {"claims": [{"claim": {"question_slug": slug}, "accepted": accepted} for slug, accepted in
        [("ready", True), ("rejected", False), ("unknown", None), ("done", True)]]}
    before = detached(original)
    assert native._resolution_targets(original, strict, allow_partial=partial) == (
        ["ready"] if partial is True else ["ready", "empty", "rejected", "unknown"])
    assert original == before
    original["questions"][0]["status"] = "answered"
    assert native._resolution_targets(original, strict, allow_partial=True) == []


@pytest.mark.parametrize("checked", [None, "2026-10-01T11:59:59Z", "2026-10-01T12:00:20Z", "2026-10-01T12:02:00Z"])
def test_retained_application_clock_requires_actual_completed_job_and_probe_interval(checked):
    attempt = SimpleNamespace(job={"issued_utc": "2026-10-01T11:59:50Z", "deadline_utc": "2026-10-01T12:01:30Z"})
    probe = {"observed_utc": "2026-10-01T12:00:00Z", "expires_utc": "2026-10-01T12:00:20Z"}
    receipt = {"completed_utc": "2026-10-01T11:59:55Z"}
    assert execution._application_checked_at({"checked_utc": checked}, probe, attempt, receipt, "2026-10-01T12:01:00Z") is None
    assert execution._application_checked_at({"checked_utc": "2026-10-01T12:00:10Z"}, probe, attempt, receipt,
        "2026-10-01T12:01:00Z") == "2026-10-01T12:00:10Z"
    assert execution._application_checked_at({"checked_utc": "2026-10-01T12:00:10Z"}, probe, attempt,
        {"completed_utc": "2026-10-01T12:00:11Z"}, "2026-10-01T12:01:00Z") is None


@pytest.fixture
def witness(monkeypatch):
    records = []
    session = SimpleNamespace(execution=SimpleNamespace(profile=SimpleNamespace(sha256="profile"),
        qualifications=SimpleNamespace(sha256="registry")))
    now = datetime.now(timezone.utc)
    probe = {"observed_utc": now.isoformat(), "expires_utc": (now + timedelta(seconds=30)).isoformat()}
    monkeypatch.setattr(native, "retained", lambda *_: detached(records))
    owner = probes._ProbeObservation(session, "operation", "basis", {"semantic_basis_id": "domain"}, probe, records)
    token = probes._ACTIVE.set(owner)
    try:
        yield session, owner, records, probe
    finally:
        probes._ACTIVE.reset(token)


def test_owner_requires_exact_request_and_original_clock_bytes(witness):
    session, owner, _records, original = witness
    assert owner.retention(session)["probe"] == original
    copy = owner.retention(session)
    copy["probe"]["expires_utc"] = "2099-01-01T00:00:00Z"
    assert owner.retention(session)["probe"] == original
    with pytest.raises(UsageError) as wrong:
        owner.require_request(session, "other-operation")
    assert wrong.value.details["reason"] == "probe_observation_request_mismatch"


@pytest.mark.parametrize("boundary", ["wait", "idempotent_append"])
def test_even_no_result_change_wait_and_idempotent_append_invalidate_observation(witness, boundary):
    session, owner, records, _probe = witness
    if boundary == "wait":
        assert research_execution.bounded_host_call(lambda: None, (), 1) == (None, None)
    else:
        records.append({"request_id": "same", "kind": "same", "data": {}, "event_ref": "existing-event"})
        owner._records = detached(records)
        assert native.append(session, "same", "same", {}) == {"event_id": records[0].get("event_ref")}
    with pytest.raises(UsageError):
        owner.require(session)


@pytest.mark.parametrize("change", ["expired", "records", "profile", "registry"])
def test_witness_cannot_survive_expiry_or_current_owner_change(witness, monkeypatch, change):
    session, owner, records, probe = witness
    if change == "expired":
        future = datetime.fromisoformat(probe["expires_utc"])
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future
        monkeypatch.setattr(probes, "datetime", Clock)
    elif change == "records":
        records.append({"changed": True})
    else:
        selected = session.execution.profile if change == "profile" else session.execution.qualifications
        selected.sha256 = "changed"
    with pytest.raises(UsageError):
        owner.require(session)


@pytest.fixture
def preparation(monkeypatch):
    records, calls = [], {"callback": 0, "identity": 0}
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    attempt = {"job": {"id": "operation", "deadline_utc": deadline.isoformat()},
        "nonce": "fixed-nonce", "input": {"raw": "exact input"}}
    attempt_record = {"attempt": attempt, "purpose": "readability", "reservation": {"seconds": "30"},
        "readability_parent": {"order_seed": "fixed-seed", "binding": {"context_id": "fixed-context"}}}
    session = SimpleNamespace(execution=SimpleNamespace(profile=SimpleNamespace(total_steps=10),
        attempt=lambda document: SimpleNamespace(document=lambda: detached(document), attempt_id=document["job"]["id"])))
    view = {"basis_id": "basis", "execution": {"steps_used": 0}}
    @contextmanager
    def gate(*_a, **_k):
        yield
    class Witness:
        def require_request(self, current, request):
            assert current is session and request == "operation"
        def consume(self, current):
            assert current is session
            return {"probe": {"original_time": "unchanged"}, "domain_ref": "domain"}
    @contextmanager
    def prepared(current, request):
        calls["callback"] += 1
        yield Witness()
    @contextmanager
    def held(current):
        yield SimpleNamespace(native_current=lambda: detached(view), _native_observation=object())
    def build(*_a):
        calls["identity"] += 1
        return detached(attempt_record)
    monkeypatch.setattr(native, "retained", lambda *_: detached(records))
    monkeypatch.setattr(native, "gate", gate)
    monkeypatch.setattr(native, "_begin", lambda *_: None)
    monkeypatch.setattr(native, "deadline", lambda *_: deadline)
    monkeypatch.setattr(probes, "prepared", prepared)
    monkeypatch.setattr(execution, "_held_current", held)
    monkeypatch.setattr(execution, "_next_from_owned", lambda *_: {"operation": "evaluate", "purpose": "readability"})
    monkeypatch.setattr(execution, "_prepare_reservation", build)
    monkeypatch.setattr(execution, "_admit_input_dispatch", lambda *_: None)
    monkeypatch.setattr(execution, "_commit_current", lambda *_: None)
    # Prefix mechanics use a tiny owner seam; full typed/HMAC persistence has
    # separate tests with real ExecutionAttempt and ResearchExecution codecs.
    monkeypatch.setattr(execution, "stored_preparation", lambda value, _owner: detached(value))
    monkeypatch.setattr(execution, "restored_preparation", lambda value, _owner: detached(value))
    session.execution.seal_record = lambda _s, kind, identity, data, **_k: {
        "kind": kind, "request_id": identity, "data": detached(data)}
    return session, records, calls, attempt_record


@pytest.mark.parametrize("fault", [1, 2, 3])
@pytest.mark.parametrize("written", [False, True])
def test_interruption_after_each_append_never_regenerates_identity_or_reinvokes(preparation, monkeypatch, fault, written):
    session, records, calls, exact = preparation
    count = 0
    def append(_session, kind, request_id, data):
        nonlocal count
        count += 1
        if count == fault and not written:
            raise RuntimeError("Controlled before-write interruption")
        records.append({"kind": kind, "request_id": request_id, "data": detached(data)})
        if count == fault:
            raise RuntimeError("Controlled after-write interruption")
    monkeypatch.setattr(native, "append", append)
    with pytest.raises(RuntimeError):
        execution.prepare_step(session, "operation", "basis")
    if not records:
        assert calls == {"callback": 1, "identity": 1}
        return
    intent = records[0]["data"]
    assert canonical(intent["readability_preparation"]["attempt_record"]) == canonical(exact)
    before = dict(calls)
    if records[-1]["kind"] == "execution_attempt":
        attempt, action = execution.prepare_step(session, "operation", "basis")
        assert attempt.document() == exact["attempt"] and action["operation"] == "reconcile"
    else:
        with pytest.raises(UsageError) as refused:
            execution.prepare_step(session, "operation", "basis")
        assert refused.value.details["reason"] == "commit_prefix_requires_reconciliation"
    assert calls == before


def test_probe_only_and_unsupported_old_intent_refuse_before_callback_or_randomness(preparation):
    session, records, calls, _exact = preparation
    records.append({"kind": "execution_readability_probe", "request_id": native.child_id("operation", "readability_probe"), "data": {}})
    with pytest.raises(UsageError):
        execution.prepare_step(session, "operation", "basis")
    assert calls == {"callback": 0, "identity": 0}


@pytest.mark.parametrize("selection", ["terminal", "complete", "repair"])
def test_fresh_selection_loss_returns_finite_refusal_without_reservation(preparation, monkeypatch, selection):
    session, records, calls, _exact = preparation
    terminal = {"operation": "terminal", "reason": "budget_exhausted", "decision": {"outcome": "stop_budget"}}
    action = terminal if selection == "terminal" else None if selection == "complete" else {"operation": "repair"}
    monkeypatch.setattr(execution, "_next_from_owned", lambda *_: detached(action))
    monkeypatch.setattr(native, "_terminal_action", lambda *_: terminal)
    attempt, result = execution.prepare_step(session, "operation", "basis")
    assert attempt is None and result == terminal
    assert records == [] and calls == {"callback": 1, "identity": 0}


@pytest.mark.parametrize("loss", ["budget", "unknown_usage", "qualification", "unsupported"])
def test_current_selection_refusal_preserves_native_hard_policy_classification(monkeypatch, loss):
    from evidence_wiki.run_policy import RunPolicy
    from tests.test_run_policy import observation, policy_document

    policy = RunPolicy(policy_document())
    observed_policy = observation(policy)
    if loss == "budget":
        observed_policy["spent"]["seconds"] = policy.to_dict()["budgets"]["seconds"]
    elif loss == "unknown_usage":
        observed_policy["spent"]["model_calls"] = None
    fresh = {"qualification": {"state": "unknown" if loss == "qualification" else "qualified"}}
    session = SimpleNamespace(policy=policy, execution=SimpleNamespace(profile=SimpleNamespace(readability=object())))
    held = SimpleNamespace(require=lambda *_: None,
        document=lambda: {"global_blockers": ["current_state_unavailable"], "verdict": "abstain"},
        native_current=lambda: fresh)
    monkeypatch.setattr(native, "policy_observation", lambda *_: observed_policy)
    monkeypatch.setattr(native, "retained", lambda *_: [])
    monkeypatch.setattr(native, "execution_budget_overrun", lambda *_: False)
    monkeypatch.setattr(native, "_terminal_action", lambda _s, actual, outcome, reason: {
        "operation": "terminal", "outcome": outcome, "reason": reason, "same_current": actual is fresh})
    result = execution._next_from_owned(session, {}, [], held)
    expected = {"budget": ("stop_budget", "budget_exhausted"), "unknown_usage": ("stop_budget", "usage_unknown"),
        "qualification": ("blocked_external", "qualification_unknown"), "unsupported": ("abstained", "unsupported_claim")}
    assert (result["outcome"], result["reason"]) == expected[loss] and result["same_current"]


def test_extant_attempt_public_route_polls_without_any_probe_or_reservation(preparation, monkeypatch):
    session, records, calls, exact = preparation
    records.append({"kind": "execution_attempt", "data": exact})
    session.context = SimpleNamespace(require_grant=lambda *_: None)
    session.execution.profile.readability = object()
    session.execution.profile.calibration = None
    seen = []
    monkeypatch.setattr(native, "_complete_advance", lambda _s, _r, _b, action, attempt: seen.append(action["operation"]))
    native.advance(session, request_id="operation", expected_basis_id="basis")
    assert seen == ["reconcile"] and calls == {"callback": 0, "identity": 0}


def test_probe_application_without_marker_recovers_exact_history_without_current_approval(monkeypatch):
    from evidence_wiki.mutation_contracts import digest
    from evidence_wiki.readability_contracts import OUTPUT
    from tests.test_readability_context import context
    _brief, packet, binding = context()
    output = {"schema_version": OUTPUT, "context_id": packet["context_id"], "input_sha256": digest(packet),
        "state": "issues", "issues": []}
    parent = {"binding": binding, "plan_sha256": "plan"}
    attempt = SimpleNamespace(attempt_id="old", config_ref="readability", input_document={"packet": packet},
        worker_document=lambda: {"exact": "retained worker bytes"})
    result = {"state": "issues", "issues": [], "output_sha256": digest(output),
        "substance_sha256": binding["substance_sha256"], "packet_sha256": binding["packet_sha256"]}
    records = [{"kind": "execution_input_handoff", "data": {"attempt_id": "old",
        "boundary": "native_readability_stdin_complete", "worker_sha256": digest(attempt.worker_document()).removeprefix("sha256:")}},
        {"kind": "execution_application", "data": {"attempt_id": "old", "operation": "readability", "original_id": "original", "result": result}}]
    session = SimpleNamespace(execution=SimpleNamespace(profile=SimpleNamespace(readability=SimpleNamespace(sha256="plan", max_output_bytes=65536))))
    monkeypatch.setattr(native, "retained", lambda *_: detached(records))
    writes = []
    monkeypatch.setattr(native, "append", lambda _s, kind, _id, data: writes.append((kind, data)))
    monkeypatch.setattr(probes, "prepared", lambda *_: pytest.fail("Historical marker recovery called a provider"))
    observed = {"state": "succeeded", "final_accounting": True, "observed_usage": {"model_calls": 1},
        "reason": "completed", "output": output}
    assert execution._recover_applied_marker(session, {"purpose": "readability", "readability_parent": parent,
        "original_id": "original"}, attempt, observed)
    assert writes == [("execution_applied", {"attempt_id": "old", "operation": "readability", "original_id": "original", "profile_ref": "readability"})]
    records[1]["data"]["result"]["output_sha256"] = "substituted"
    with pytest.raises(UsageError):
        execution._recover_applied_marker(session, {"purpose": "readability", "readability_parent": parent,
            "original_id": "original"}, attempt, observed)
    assert len(writes) == 1


@pytest.mark.parametrize("crossing", ["probe_expiry", "journal_change"])
def test_closed_preparation_is_not_approval_during_fresh_commit(witness, monkeypatch, crossing):
    session, owner, records, probe = witness
    session._check = lambda *_a, **_k: None
    future = datetime.fromisoformat(probe["expires_utc"])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future
    def fresh_current(_session):
        if crossing == "probe_expiry":
            monkeypatch.setattr(probes, "datetime", Clock)
        else:
            records.append({"current": "changed"})
        return {}
    monkeypatch.setattr(native, "current", fresh_current)
    monkeypatch.setattr(native, "append", lambda *_a, **_k: pytest.fail("Commit wrote after current invalidation"))
    with pytest.raises(UsageError):
        execution._commit_current(session, owner, SimpleNamespace(finish=lambda: None), {})


def test_closed_preparation_source_change_is_refused_before_commit(witness, monkeypatch):
    session, owner, _records, _probe = witness
    session._check = lambda *_a, **_k: None
    monkeypatch.setattr(native, "current", lambda *_: {})
    monkeypatch.setattr(native, "append", lambda *_a, **_k: pytest.fail("Commit wrote after source change"))
    def changed():
        raise UsageError("CURRENT_SOURCE_CHANGED", "Controlled owner found changed bytes.")
    with pytest.raises(UsageError) as refused:
        execution._commit_current(session, owner, SimpleNamespace(finish=changed), {})
    assert refused.value.error_code == "CURRENT_SOURCE_CHANGED"


def test_new_application_job_expiry_during_full_closing_refuses_before_writes(preparation, monkeypatch):
    session, records, _calls, _exact = preparation
    end = datetime.now(timezone.utc) + timedelta(seconds=5)
    attempt = SimpleNamespace(attempt_id="attempt", config_ref="readability", job={"deadline_utc": end.isoformat()},
        input_document={"packet": {}}, worker_document=lambda: {"exact": "worker"})
    from evidence_wiki.mutation_contracts import digest
    records.append({"kind": "execution_input_handoff", "data": {"attempt_id": "attempt",
        "worker_sha256": digest(attempt.worker_document()).removeprefix("sha256:")}})
    row = {"purpose": "readability", "readability_parent": {"basis_id": "basis", "binding": {}, "order_seed": "00"},
        "original_id": "original"}
    session._check = lambda *_a, **_k: None
    session.execution.profile.readability = SimpleNamespace(config_ref="readability", max_input_bytes=524288, max_output_bytes=65536)
    monkeypatch.setattr(execution, "owned", lambda *_: (row, attempt, {"output": {}}))
    monkeypatch.setattr(execution, "_recover_applied_marker", lambda *_: False)
    monkeypatch.setattr(execution, "_model_observed", lambda *_: None)
    monkeypatch.setattr(execution, "_eligible", lambda *_: None)
    monkeypatch.setattr(execution, "qualification", lambda *_: {"state": "qualified"})
    monkeypatch.setattr(execution, "reduce", lambda *_a, **_k: {})
    monkeypatch.setattr(execution, "project", lambda *_: {})
    monkeypatch.setattr(native, "_document", lambda *_: {"state": {"current": "answering"}})
    monkeypatch.setattr(native, "owner", lambda *_: SimpleNamespace(TERMINAL_STATES={"complete"}))
    monkeypatch.setattr(native, "deadline", lambda *_: end + timedelta(seconds=60))
    @contextmanager
    def prepared(current, request):
        def require_request(owned_session, owned_request):
            assert owned_session is current and owned_request == request
        yield SimpleNamespace(require_request=require_request,
            consume=lambda *_: pytest.fail("Expired application consumed an observation"))
    monkeypatch.setattr(probes, "prepared", prepared)
    @contextmanager
    def held(_session):
        yield SimpleNamespace(_basis_id="basis", _native_observation=object(), _native_view={},
            bind_readability_eligibility=lambda: None, document=lambda: {})
    monkeypatch.setattr(execution, "_held_current", held)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return end
    monkeypatch.setattr(execution, "_commit_current", lambda *_: monkeypatch.setattr(execution, "datetime", Clock))
    monkeypatch.setattr(native, "append", lambda *_a, **_k: pytest.fail("Expired new application wrote a journal row"))
    with pytest.raises(UsageError) as expired:
        execution.apply(session, "attempt")
    assert expired.value.details["reason"] == "application_deadline_expired"
